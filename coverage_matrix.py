"""Which Open-Meteo models actually return data, per model, per city, per lead.

Ground truth for the ensemble expansion. Everything in Phases 2.3-2.5 is a
hypothesis until this table says a model returns non-null at a coordinate.

Queried ONE MODEL AT A TIME, which is the whole point. A multi-model request
containing a single invalid ID returns HTTP 400 for the ENTIRE request, so a
batched probe reports "no data" for every model in the batch and the failure is
attributed to the wrong thing. That is the origin of the "GFS unavailable in the
Southern Hemisphere" belief now embedded in weather.STATIONS, where Cape Town,
Buenos Aires and Sao Paulo were moved to a GFS-less blend. Availability really
is per-model per-coordinate — gfs_graphcast025 and bom_access_global genuinely
return null at Cape Town — but that is a fact to be measured, not inferred from
a 400.

    python coverage_matrix.py                  # all models x all cities
    python coverage_matrix.py --models a,b     # subset
"""
import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OPEN_METEO_URL
from weather import STATIONS

# Every model that could plausibly serve a traded city. Deliberately a superset:
# the API is the gate, and an ID that returns nothing everywhere is a cheap
# negative result worth having written down.
CANDIDATE_MODELS = [
    # --- currently in use ---
    "ecmwf_ifs025", "gfs_global", "icon_global", "gem_global", "jma_gsm",
    # --- missing globals (item 13) ---
    "ukmo_global_deterministic_10km", "meteofrance_arpege_world",
    "bom_access_global", "cma_grapes_global",
    # --- AI members (item 14) ---
    "ecmwf_aifs025_single", "gfs_graphcast025", "ncep_aigfs025",
    # --- North America regional ---
    "ncep_nbm_conus", "gfs_hrrr", "ncep_nam_conus",
    "gem_hrdps_continental", "gem_regional",
    # --- Europe regional ---
    "icon_eu", "icon_d2", "ukmo_uk_deterministic_2km",
    "meteofrance_arpege_europe", "meteofrance_arome_france_hd",
    "knmi_harmonie_arome_europe", "knmi_harmonie_arome_netherlands",
    "dmi_harmonie_arome_europe", "metno_seamless",
    "italia_meteo_arpae_icon_2i", "arpae_cosmo_2i",
    # --- Asia-Pacific regional ---
    "jma_msm", "kma_ldps", "kma_gdps",
]

LEADS = (24, 48, 72)


def probe(model, lat, lon, timeout=25):
    """Non-null coverage for one model at one coordinate, by lead hour.

    Returns {"ok": bool, "status": int|str, "leads": {24: bool, 48: bool, 72: bool},
             "horizon_h": int|None}. `horizon_h` is the furthest lead that
    returned a value — the practical statement of a limited-area model's reach
    (icon_d2 simply stops past 48h)."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "models": model,
        "timezone": "UTC",
        "temperature_unit": "fahrenheit",
        "forecast_days": 4,
    }
    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "status": type(e).__name__, "leads": {}, "horizon_h": None}

    if r.status_code != 200:
        # 400 here means the model ID is invalid FOR THIS REQUEST, which with a
        # single model is unambiguous — the reason this probe is one-at-a-time.
        detail = ""
        try:
            detail = r.json().get("reason", "")[:120]
        except Exception:
            pass
        return {"ok": False, "status": r.status_code, "reason": detail,
                "leads": {}, "horizon_h": None}

    series = r.json().get("hourly", {}).get(f"temperature_2m_{model}") \
        or r.json().get("hourly", {}).get("temperature_2m")
    if not series:
        return {"ok": False, "status": "no_series", "leads": {}, "horizon_h": None}

    leads = {ld: (ld < len(series) and series[ld] is not None) for ld in LEADS}
    horizon = None
    for i, v in enumerate(series):
        if v is not None:
            horizon = i
    return {"ok": any(leads.values()), "status": 200, "leads": leads,
            "horizon_h": horizon}


def run(models, cities, pause=0.15):
    """One row per (model, city). Serial: Open-Meteo's free tier rate-limits
    parallel probes, and a rate-limited probe reports a false negative — which
    in this script would become a permanent belief about model availability."""
    results = defaultdict(dict)
    total = len(models) * len(cities)
    done = 0
    for model in models:
        for city in cities:
            st = STATIONS[city]
            results[model][city] = probe(model, st["lat"], st["lon"])
            done += 1
            if done % 50 == 0:
                logging.info(f"  {done}/{total}")
            time.sleep(pause)
        n_ok = sum(1 for c in cities if results[model][c]["ok"])
        logging.info(f"{model:34s} {n_ok:3d}/{len(cities)} cities")
    return results


def render(results, cities):
    lines = ["# Open-Meteo coverage matrix", "",
             f"{len(results)} candidate models x {len(cities)} cities, probed "
             f"ONE MODEL AT A TIME.", "",
             "A multi-model request containing one invalid ID returns HTTP 400 "
             "for the whole request, so a batched probe blames every model in "
             "the batch. That is where the \"GFS unavailable in the Southern "
             "Hemisphere\" belief came from.", "",
             "| model | cities with data | 24h | 48h | 72h | notes |",
             "|---|---|---|---|---|---|"]
    for model, per_city in results.items():
        ok = [c for c in cities if per_city[c]["ok"]]
        at = {ld: sum(1 for c in cities if per_city[c]["leads"].get(ld)) for ld in LEADS}
        statuses = {str(per_city[c].get("status")) for c in cities if not per_city[c]["ok"]}
        note = ""
        if not ok:
            note = f"NO DATA ANYWHERE (statuses: {', '.join(sorted(statuses))})"
        elif len(ok) < len(cities):
            note = f"limited domain — {len(cities) - len(ok)} cities null"
        if ok and at[72] < at[24]:
            note += ("; " if note else "") + f"horizon-limited (72h at {at[72]} vs 24h at {at[24]})"
        lines.append(f"| `{model}` | {len(ok)}/{len(cities)} | {at[24]} | {at[48]} | "
                     f"{at[72]} | {note} |")

    lines += ["", "## Per-city availability", "",
              "| city | models with data |", "|---|---|"]
    for city in cities:
        avail = [m for m in results if results[m][city]["ok"]]
        lines.append(f"| {city} | {len(avail)}: {', '.join(sorted(avail))} |")
    lines.append("")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--out", default="reports/coverage-matrix.md")
    ap.add_argument("--json-out", default="reports/coverage-matrix.json")
    args = ap.parse_args()

    models = args.models.split(",") if args.models else CANDIDATE_MODELS
    # One entry per distinct coordinate: NYC and New York are the same station.
    seen, cities = set(), []
    for c, st in STATIONS.items():
        key = (st["lat"], st["lon"])
        if key not in seen:
            seen.add(key)
            cities.append(c)

    results = run(models, cities)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.json_out, "w") as fh:
        json.dump(results, fh, indent=2)
    with open(args.out, "w") as fh:
        fh.write(render(results, cities))
    print(render(results, cities))
    return 0


if __name__ == "__main__":
    sys.exit(main())
