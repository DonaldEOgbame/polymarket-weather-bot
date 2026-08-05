"""Fit the remaining-rise climatology used by intraday observation conditioning.

At 15:00 local with the station already at 91°F, P(daily max < 91) is zero, and
the day's final max is 91 plus however much rise is left. That "however much" is
what this fits.

The naive parameterisation — expected remaining rise in °F by hour and month —
does not travel. August is high summer in Chicago and midwinter in Wellington,
and a °F table fitted on one is nonsense for the other. So the quantity fitted
here is DIMENSIONLESS:

    f(h) = (max_final - max_so_far_at_h) / (max_final - min_final)

the fraction of the day's diurnal range still to come at local hour h. That
factors the climate out: a big-range desert day and a small-range maritime day
have similar f(h) curves and wildly different °F. At runtime it is multiplied by
the diurnal range the ENSEMBLE forecasts for that day — which is available for
free since Phase 1.2 made one request return both max and min.

The mirror quantity for minima:

    g(h) = (min_so_far_at_h - min_final) / (max_final - min_final)

Stations are chosen to span the climate zones actually traded, so pooling is a
measured decision rather than an assumption: the report prints per-station
curves alongside the pooled one, and if they disagree the pooling is wrong and
the table needs a per-climate key.

    python fit_remaining_rise.py --months 12 --out reports/remaining-rise.md
"""
import argparse
import csv
import io
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MESONET_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Ten traded stations spanning the climate zones in STATIONS. Not a random
# sample — deliberately chosen so that if the pooled curve is wrong for some
# climate, one of these shows it.
SAMPLE_STATIONS = [
    ("KORD", "America/Chicago",     "continental"),
    ("KMIA", "America/New_York",    "tropical maritime"),
    ("KLAX", "America/Los_Angeles", "Mediterranean coastal"),
    ("KBKF", "America/Denver",      "high plains"),
    ("EGLC", "Europe/London",       "maritime temperate"),
    ("RJTT", "Asia/Tokyo",          "humid subtropical monsoon"),
    ("ZGGG", "Asia/Shanghai",       "subtropical monsoon"),
    ("WSSS", "Asia/Singapore",      "equatorial"),
    ("OEJN", "Asia/Riyadh",         "desert"),
    ("NZWN", "Pacific/Auckland",    "southern maritime"),
]

# A day is usable only if it is well observed. A day with a six-hour gap has a
# "max so far" that is missing exactly the hours the curve is about.
MIN_OBS_PER_DAY = 18
# And its diurnal range must be big enough that dividing by it is meaningful.
# Below ~2°C the ratio is dominated by observation noise, not by the diurnal
# cycle, and those days would inject enormous variance into f(h).
MIN_RANGE_C = 2.0


def _fetch_month(station, tz, y, m, max_retries=5):
    """One station-month of observations, with backoff on IEM's rate limiter.

    Fetched serially and retried rather than fanned out: six parallel workers
    got HTTP 429 on roughly 80% of requests, and the fit silently proceeded on
    the ~20% that survived — 62 "usable days" out of an expected ~365, which is
    a table fitted on a sixth of the data it claims. A rate limiter that
    degrades the SAMPLE rather than failing the RUN is the more dangerous
    failure, so this raises rather than returning empty when retries run out."""
    start = date(y, m, 1)
    end = (start + timedelta(days=32)).replace(day=1)
    p = {"station": station, "data": "tmpc",
         "year1": start.year, "month1": start.month, "day1": start.day,
         "year2": end.year, "month2": end.month, "day2": end.day,
         "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "M"}
    backoff = 3.0
    for attempt in range(max_retries):
        try:
            r = requests.get(MESONET_URL, params=p, timeout=120)
            if r.status_code == 200:
                break
            if r.status_code != 429:
                raise RuntimeError(f"{station} {y}-{m:02d}: HTTP {r.status_code}")
            logging.info(f"  {station} {y}-{m:02d}: 429, waiting {backoff:.0f}s")
        except requests.RequestException as e:
            logging.info(f"  {station} {y}-{m:02d}: {e}, retrying")
        time.sleep(backoff)
        backoff *= 1.8
    else:
        raise RuntimeError(f"{station} {y}-{m:02d}: rate limited after "
                           f"{max_retries} attempts — refusing to fit on a "
                           f"partial sample")
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        v = row.get("tmpc", "M")
        if v in ("M", "", None):
            continue
        try:
            out.append((row["valid"], float(v)))
        except ValueError:
            continue
    return out


def fetch_station(station, tz, months, pause=1.0):
    """All observations for `months` months back, as {local_date: [(hour, °C)]}.

    Serial, with a courtesy pause. IEM is a free public archive and the whole
    fit is a one-off."""
    today = date.today().replace(day=1)
    periods = []
    for i in range(1, months + 1):
        d = today
        for _ in range(i):
            d = (d - timedelta(days=1)).replace(day=1)
        periods.append((d.year, d.month))

    rows = []
    for y, m in periods:
        rows.extend(_fetch_month(station, tz, y, m))
        time.sleep(pause)

    by_day = defaultdict(list)
    for valid, temp in rows:
        # "YYYY-MM-DD HH:MM" in station-local time (tz= was passed).
        by_day[valid[:10]].append((int(valid[11:13]), temp))
    return by_day


def curves_for_day(obs):
    """(hour -> f, hour -> g, range_c) for one day, or None if unusable.

    f(h) is the fraction of the diurnal range still to RISE after hour h;
    g(h) the fraction still to FALL. Both are computed against the day's own
    final extremes, so they are dimensionless by construction."""
    if len(obs) < MIN_OBS_PER_DAY:
        return None
    temps = [t for _, t in obs]
    mx, mn = max(temps), min(temps)
    rng = mx - mn
    if rng < MIN_RANGE_C:
        return None

    f, g = {}, {}
    for h in range(24):
        so_far = [t for hh, t in obs if hh <= h]
        if not so_far:
            continue
        f[h] = (mx - max(so_far)) / rng
        g[h] = (min(so_far) - mn) / rng
    return f, g, rng


def _mean_sd(values):
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0)
    mu = sum(values) / n
    if n == 1:
        return (mu, 0.0, 1)
    var = sum((v - mu) ** 2 for v in values) / (n - 1)
    return (mu, math.sqrt(var), n)


def fit(months=12, stations=None):
    stations = stations or SAMPLE_STATIONS
    pooled_f, pooled_g = defaultdict(list), defaultdict(list)
    per_station = {}

    for icao, tz, zone in stations:
        by_day = fetch_station(icao, tz, months)
        sf, sg = defaultdict(list), defaultdict(list)
        used = 0
        for day, obs in by_day.items():
            res = curves_for_day(obs)
            if not res:
                continue
            used += 1
            f, g, _ = res
            for h, v in f.items():
                sf[h].append(v)
                pooled_f[h].append(v)
            for h, v in g.items():
                sg[h].append(v)
                pooled_g[h].append(v)
        per_station[icao] = {
            "zone": zone, "days": used,
            "f": {h: _mean_sd(v)[0] for h, v in sorted(sf.items())},
            "g": {h: _mean_sd(v)[0] for h, v in sorted(sg.items())},
        }
        logging.info(f"{icao} ({zone}): {used} usable days")

    table = {}
    for h in range(24):
        fm, fs, fn = _mean_sd(pooled_f.get(h, []))
        gm, gs, gn = _mean_sd(pooled_g.get(h, []))
        table[h] = {"f_mean": round(fm, 4), "f_sd": round(fs, 4),
                    "g_mean": round(gm, 4), "g_sd": round(gs, 4), "n": fn}
    return table, per_station


def render(table, per_station, months):
    lines = [
        "# Remaining-rise climatology",
        "",
        f"Fitted on {months} months of METAR observations from "
        f"{len(per_station)} stations spanning the traded climate zones.",
        "",
        "`f(h)` = fraction of the day's diurnal range still to RISE after local "
        "hour h — used to condition a daily MAX on observations so far.",
        "`g(h)` = fraction still to FALL — used for daily MIN.",
        "",
        "Dimensionless on purpose: a °F table fitted on August in Chicago is "
        "nonsense for August in Wellington. At runtime these are multiplied by "
        "the diurnal range the ensemble forecasts for the day.",
        "",
        "| local hour | f mean | f sd | g mean | g sd | n days |",
        "|---|---|---|---|---|---|",
    ]
    for h in range(24):
        r = table[h]
        lines.append(f"| {h:02d} | {r['f_mean']:.3f} | {r['f_sd']:.3f} | "
                     f"{r['g_mean']:.3f} | {r['g_sd']:.3f} | {r['n']:,} |")

    lines += ["", "## Per-station f(h) — is pooling defensible?", "",
              "If these disagree materially the pooled table is wrong and the "
              "climatology needs a per-climate key.", "",
              "| station | zone | days | " +
              " | ".join(f"{h:02d}" for h in (0, 6, 9, 12, 15, 18, 21)) + " |",
              "|---|---|---|" + "---|" * 7]
    for icao, s in per_station.items():
        cells = " | ".join(f"{s['f'].get(h, float('nan')):.2f}"
                           for h in (0, 6, 9, 12, 15, 18, 21))
        lines.append(f"| {icao} | {s['zone']} | {s['days']} | {cells} |")
    lines.append("")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--out", default="reports/remaining-rise.md")
    ap.add_argument("--json-out", default="reports/remaining-rise.json")
    args = ap.parse_args()

    table, per_station = fit(args.months)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(render(table, per_station, args.months))
    with open(args.json_out, "w") as fh:
        json.dump({"table": table, "per_station": per_station}, fh, indent=2)
    print(render(table, per_station, args.months))
    print(f"\nwritten to {args.out} and {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
