"""Which values a station can actually report, and therefore which buckets can pay.

Markets settle on a QUANTISED reading, and a bucket containing no reachable
value is a bucket that cannot pay YES no matter what the weather does. Finding
those requires knowing each station's quantisation, and the assumption baked
into metar.py — whole °C everywhere — turns out to be wrong for a third of the
traded cities.

Measured 2026-08-05 over a week of observations per station:

    KORD  12.3% of readings on a whole °C, 9 distinct fractional parts
    KLGA  17.9%                             9
    EGLC  100%                              1
    ZGGG  100%                              1
    RJTT  100%                              1

The US values are not noisy °C. They are whole °F: 27.78°C is exactly 82°F,
27.22°C is 81°F, 26.67°C is 80°F. US ASOS reports in whole °F and IEM converts
to °C for the tmpc column, so the underlying lattice is °F. Everywhere else the
lattice really is whole °C.

That matches how the markets are quoted — US markets ask about °F buckets
("between 74-75°F"), the rest about °C buckets ("11°C") — which is why no
impossible bucket has ever been observed: in both regions the market's unit is
the station's unit, so every bucket contains at least one reachable value.

It also exposes a live settlement bug, which is the real product of this audit.
metar.resolved_extreme_f rounds every city's reading to a whole °C before
converting to °F. For a US station that is a spurious round trip through a
coarser grid than the observation: 27.78°C (exactly 82°F) rounds to 28°C and
comes back as 82.4°F. On the 2°F-wide buckets US markets use, an error of up to
0.9°F decides outcomes.

    python audit_station_lattice.py
"""
import argparse
import collections
import csv
import io
import json
import logging
import os
import sys
import time
from datetime import date, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from metar import STATION_ICAO
from weather import STATIONS

MESONET_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# A reading is "on the °F lattice" if converting it to °F lands on a whole
# number. Tolerance covers IEM's 2-decimal rounding of the converted value:
# 82°F is stored as 27.78°C, which converts back to 82.004°F.
_F_TOL = 0.02
_C_TOL = 0.01


def classify(values):
    """('F' | 'C' | 'UNKNOWN', diagnostics) for a station's readings."""
    if len(values) < 30:
        return "UNKNOWN", {"n": len(values), "why": "too few observations"}
    on_c = sum(1 for v in values if abs(v - round(v)) < _C_TOL)
    on_f = sum(1 for v in values if abs((v * 9 / 5 + 32) - round(v * 9 / 5 + 32)) < _F_TOL)
    n = len(values)
    diag = {"n": n, "pct_on_c": round(100 * on_c / n, 1),
            "pct_on_f": round(100 * on_f / n, 1),
            "distinct_fractions": len({round(abs(v) % 1, 2) for v in values})}
    # Deliberately strict. A station that is not overwhelmingly on one lattice
    # is UNKNOWN, and an UNKNOWN lattice must not be guessed: the whole point is
    # to stop rounding a reading onto a grid it does not live on.
    if diag["pct_on_c"] >= 95.0:
        return "C", diag
    if diag["pct_on_f"] >= 95.0:
        return "F", diag
    return "UNKNOWN", diag


def fetch(icao, tz, days=7, max_retries=5):
    end = date.today()
    start = end - timedelta(days=days)
    p = {"station": icao, "data": "tmpc",
         "year1": start.year, "month1": start.month, "day1": start.day,
         "year2": end.year, "month2": end.month, "day2": end.day,
         "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "M"}
    backoff = 3.0
    for _ in range(max_retries):
        try:
            r = requests.get(MESONET_URL, params=p, timeout=90)
            if r.status_code == 200:
                break
            if r.status_code != 429:
                return []
        except requests.RequestException:
            pass
        time.sleep(backoff)
        backoff *= 1.8
    else:
        return []
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        v = row.get("tmpc", "M")
        if v in ("M", "", None):
            continue
        try:
            out.append(float(v))
        except ValueError:
            continue
    return out


def audit(days=7, pause=1.0):
    results = {}
    # One entry per ICAO, not per city: NYC and New York are the same station.
    by_icao = {}
    for city in STATIONS:
        icao, tz = STATION_ICAO.get(city, (None, None))
        if icao:
            by_icao.setdefault(icao, (tz, []))[1].append(city)

    for icao, (tz, cities) in sorted(by_icao.items()):
        vals = fetch(icao, tz, days)
        lattice, diag = classify(vals)
        results[icao] = {"cities": sorted(cities), "tz": tz,
                         "lattice": lattice, **diag}
        logging.info(f"{icao:6s} {lattice:8s} {diag}")
        time.sleep(pause)
    return results


def render(results):
    by_lattice = collections.Counter(r["lattice"] for r in results.values())
    lines = [
        "# Station reporting lattice",
        "",
        f"{len(results)} stations. " + ", ".join(
            f"{n} `{k}`" for k, n in sorted(by_lattice.items())),
        "",
        "## Why this matters",
        "",
        "`metar.resolved_extreme_f` rounded EVERY city's reading to a whole °C "
        "before converting to °F. For a station that reports in whole °F that "
        "is a round trip through a coarser grid than the observation itself: "
        "27.78°C is exactly 82°F, rounds to 28°C, and comes back as 82.4°F. US "
        "markets use 2°F-wide buckets, so an error up to 0.9°F decides "
        "outcomes.",
        "",
        "The lattice also determines which buckets are REACHABLE. No impossible "
        "bucket has ever been observed, and this explains why rather than "
        "leaving it to luck: US markets quote °F against °F-reporting stations, "
        "and the rest quote °C against °C-reporting stations, so in both cases "
        "the market's unit is the station's unit and every bucket contains at "
        "least one reachable value.",
        "",
        "| station | cities | lattice | % on °C | % on °F | distinct fractions | n |",
        "|---|---|---|---|---|---|---|",
    ]
    for icao, r in sorted(results.items()):
        lat = r["lattice"] if r["lattice"] != "UNKNOWN" else "**UNKNOWN**"
        lines.append(
            f"| {icao} | {', '.join(r['cities'])} | {lat} | "
            f"{r.get('pct_on_c', '—')} | {r.get('pct_on_f', '—')} | "
            f"{r.get('distinct_fractions', '—')} | {r.get('n', 0)} |")
    unknown = [i for i, r in results.items() if r["lattice"] == "UNKNOWN"]
    if unknown:
        lines += ["", "## UNKNOWN lattice", "",
                  "Not guessed. Rounding a reading onto a grid it does not live "
                  "on is the bug this audit found; doing it on a hunch would be "
                  "the same bug with less evidence. These fall back to the raw "
                  "reading, unrounded.", ""]
        lines += [f"- {i} ({', '.join(results[i]['cities'])}): "
                  f"{results[i].get('why', 'mixed lattice')}" for i in sorted(unknown)]
    lines.append("")
    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="reports/station-lattice.md")
    ap.add_argument("--json-out", default="reports/station-lattice.json")
    args = ap.parse_args()

    results = audit(args.days)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write(render(results))
    with open(args.json_out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
