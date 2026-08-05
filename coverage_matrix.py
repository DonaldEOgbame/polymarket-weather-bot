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


# Coordinates per request. 51 in one request times out on the slower models
# (measured: jma_gsm, cma_grapes_global, icon_eu all exceeded 60s), and one
# timeout loses the whole model rather than one city. Chunking bounds the blast
# radius of a slow response and keeps each request inside a sane deadline.
CHUNK = 12


def _request(model, cities, timeout, max_retries=3):
    """One request for one model over up to CHUNK coordinates."""
    lats = ",".join(str(STATIONS[c]["lat"]) for c in cities)
    lons = ",".join(str(STATIONS[c]["lon"]) for c in cities)
    params = {
        "latitude": lats, "longitude": lons,
        "hourly": "temperature_2m",
        "models": model,
        "timezone": "UTC",
        "temperature_unit": "fahrenheit",
        "forecast_days": 4,
    }
    backoff = 4.0
    last = "unknown"
    for _ in range(max_retries):
        try:
            r = requests.get(OPEN_METEO_URL, params=params, timeout=timeout)
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(backoff)
            backoff *= 1.8
            continue
        if r.status_code == 429:
            last = 429
            time.sleep(backoff)
            backoff *= 1.8
            continue
        if r.status_code != 200:
            # With a single model this is unambiguous: the ID is invalid for
            # these coordinates, full stop. Not JSON-decoded blindly — an error
            # page is not JSON, and calling .json() on it crashed the first run
            # partway through and lost every result before it.
            detail = r.text[:150]
            try:
                detail = r.json().get("reason", detail)[:150]
            except ValueError:
                pass
            return None, r.status_code, detail
        try:
            return r.json(), 200, ""
        except ValueError:
            last = "bad_json"
            time.sleep(backoff)
            backoff *= 1.8
    return None, last, "exhausted retries"


def _read_location(loc, model):
    """Coverage verdict for one location object."""
    hourly = loc.get("hourly", {}) or {}
    series = hourly.get(f"temperature_2m_{model}") or hourly.get("temperature_2m")
    if not series:
        return {"ok": False, "status": "no_series", "leads": {}, "horizon_h": None}
    leads = {ld: (ld < len(series) and series[ld] is not None) for ld in LEADS}
    horizon = None
    for j, v in enumerate(series):
        if v is not None:
            horizon = j
    return {"ok": any(leads.values()), "status": 200, "leads": leads,
            "horizon_h": horizon}


def probe_model(model, cities, timeout=90):
    """Non-null coverage for ONE model across ALL cities.

    Open-Meteo accepts comma-separated latitude/longitude and returns a LIST of
    location objects in the order asked, so this costs a handful of requests per
    model rather than one per city. The sequential per-coordinate version was
    measured at roughly six hours, most of it spent waiting out timeouts on
    model/coordinate pairs that were never going to return anything.

    Still ONE MODEL AT A TIME, which is the part that matters. A request naming
    several models returns HTTP 400 for ALL of them if any single ID is invalid,
    so a batched-by-model probe reports "no data" for every model in the batch
    and attributes the failure to the wrong thing. That is the origin of the
    "GFS unavailable in the Southern Hemisphere" belief. Batching by COORDINATE
    has no such failure mode: an out-of-domain coordinate returns nulls for that
    location only, which is exactly the signal being measured.
    """
    out = {}
    for i in range(0, len(cities), CHUNK):
        chunk = cities[i:i + CHUNK]
        payload, status, reason = _request(model, chunk, timeout)
        if payload is None:
            # A 400 saying "No data is available for this location" is
            # OUT-OF-DOMAIN, not an invalid model ID — verified 2026-08-05:
            # gfs_hrrr, ncep_nbm_conus and ncep_nam_conus all return 200 at
            # Chicago and 400 at Tokyo. So one out-of-domain coordinate 400s
            # the whole chunk, which is the SAME failure this script exists to
            # avoid, just on the coordinate axis instead of the model axis:
            # batching would report "no data anywhere" for every limited-area
            # model and attribute it to the model rather than to the
            # coordinate. Fall back to probing the chunk one coordinate at a
            # time so a limited-area model gets a correct domain map.
            if status == 400:
                for c in chunk:
                    one, st1, rs1 = _request(model, [c], timeout)
                    if one is None:
                        out[c] = {"ok": False, "status": st1, "reason": rs1,
                                  "leads": {}, "horizon_h": None}
                    else:
                        loc = one[0] if isinstance(one, list) else one
                        out[c] = _read_location(loc, model)
                    time.sleep(0.2)
                continue
            for c in chunk:
                out[c] = {"ok": False, "status": status, "reason": reason,
                          "leads": {}, "horizon_h": None}
            continue
        locs = payload if isinstance(payload, list) else [payload]
        for city, loc in zip(chunk, locs):
            out[city] = _read_location(loc, model)
        for city in chunk[len(locs):]:
            out[city] = {"ok": False, "status": "missing_location", "leads": {},
                         "horizon_h": None}
        time.sleep(0.3)
    return out


def run(models, cities, pause=0.5, partial_path=None):
    """One row per (model, city). Writes partial results as it goes.

    The partial write is not decoration: the first run crashed on model 20 of 32
    and lost every result before it, having already spent an hour."""
    results = {}
    for model in models:
        try:
            results[model] = probe_model(model, cities)
        except Exception as e:
            logging.error(f"{model}: {type(e).__name__}: {e}")
            results[model] = {c: {"ok": False, "status": type(e).__name__,
                                  "leads": {}, "horizon_h": None} for c in cities}
        n_ok = sum(1 for c in cities if results[model][c]["ok"])
        statuses = {str(results[model][c].get("status"))
                    for c in cities if not results[model][c]["ok"]}
        note = f"  ({', '.join(sorted(statuses))})" if statuses else ""
        logging.info(f"{model:34s} {n_ok:3d}/{len(cities)}{note}")
        if partial_path:
            with open(partial_path, "w") as fh:
                json.dump(results, fh)
        time.sleep(pause)
    return results


def _lead(leads, ld):
    """leads[ld], tolerating the string keys a JSON round-trip produces."""
    if not leads:
        return False
    return bool(leads.get(ld, leads.get(str(ld), False)))


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
        # A JSON round-trip turns the integer lead keys into strings, so
        # leads.get(24) silently returns None and every horizon column reads 0
        # — which looked exactly like "this model has no data at any lead" for
        # models that plainly did. Accept both.
        at = {ld: sum(1 for c in cities
                      if _lead(per_city[c]["leads"], ld)) for ld in LEADS}
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

    results = run(models, cities, partial_path=args.json_out)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.json_out, "w") as fh:
        json.dump(results, fh, indent=2)
    with open(args.out, "w") as fh:
        fh.write(render(results, cities))
    print(render(results, cities))
    return 0


if __name__ == "__main__":
    sys.exit(main())
