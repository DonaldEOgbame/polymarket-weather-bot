#!/usr/bin/env python3
"""
scripts/prefetch_wx_batch.py

Bulk-fill the causal backtest's weather cache using Open-Meteo's multi-location
+ date-range batching: ONE request covers N cities x the whole window, instead
of one request per city-day.

Why: the per-city-day loop in backtest_causal_deployed.py costs 2 hourly-weighted
calls each (~1500 for a 30d x 25-city run) and Open-Meteo throttles that to
~42 city-days/hour. Batched, the same job is ~10 requests.

Timezone note: batched requests cannot carry a per-city `timezone`, so we fetch
UTC hourly series and aggregate to LOCAL calendar days ourselves using each
station's own tz -- matching what the per-city (timezone=...) calls returned.
Verified to reproduce cached values exactly.

Writes the same {fc_max,fc_min,act_max,act_min} JSON files that _wx() reads, so
the backtest just finds a warm cache.
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from weather import STATIONS          # noqa: E402
from metar import STATION_ICAO        # noqa: E402

WX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_causal_cache", "wx")
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
CHUNK = 5  # cities per request

EXCLUDED = set("""Ankara,Atlanta,Beijing,Buenos Aires,Cape Town,Chengdu,Chongqing,
Denver,Guangzhou,Hong Kong,Houston,Lagos,Lucknow,Milan,Munich,NYC,New York,Panama,
San Francisco,Sao Paulo,Seoul,Shenzhen,Taipei,Tel Aviv,Wuhan""".replace("\n", "").split(","))

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (prefetch)"})


def _get(url, params, tries=5):
    backoff = 5.0
    for _ in range(tries):
        try:
            r = _s.get(url, params=params, timeout=120)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                now = time.time()
                reset = (int(now // 3600) + 1) * 3600 + 30
                wait = max(30, reset - now)
                print(f"    [quota] sleeping {int(wait)}s", flush=True)
                time.sleep(wait)
                continue
            print(f"    [http {r.status_code}] {r.text[:120]}", flush=True)
        except Exception as e:
            print(f"    [err] {e}", flush=True)
        time.sleep(backoff)
        backoff *= 2
    return None


def _local_day_extremes(times, vals, tz_name):
    """Aggregate a UTC hourly series into local-calendar-day (max,min)."""
    tz = ZoneInfo(tz_name)
    by_day = defaultdict(list)
    for t, v in zip(times, vals):
        if v is None:
            continue
        # Open-Meteo returns naive ISO in the requested tz; we request UTC.
        dt = datetime.fromisoformat(t).replace(tzinfo=ZoneInfo("UTC"))
        by_day[dt.astimezone(tz).date().isoformat()].append(v)
    return {d: (max(v), min(v)) for d, v in by_day.items() if v}


def main():
    start, end = sys.argv[1], sys.argv[2]
    cities = [c for c in STATIONS
              if c not in EXCLUDED and c in STATION_ICAO]
    print(f"cities={len(cities)}  window={start}..{end}", flush=True)

    # pad one day each side so local-day aggregation has full coverage
    s_pad = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    e_pad = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(WX_DIR, exist_ok=True)
    data = defaultdict(dict)  # city -> date -> partial dict

    for i in range(0, len(cities), CHUNK):
        grp = cities[i:i + CHUNK]
        lats = ",".join(str(STATIONS[c]["lat"]) for c in grp)
        lons = ",".join(str(STATIONS[c]["lon"]) for c in grp)

        fc = _get(PREV_RUNS, {"latitude": lats, "longitude": lons,
                              "start_date": s_pad, "end_date": e_pad,
                              "hourly": "temperature_2m_previous_day1",
                              "temperature_unit": "fahrenheit", "timezone": "UTC"})
        ac = _get(ARCHIVE, {"latitude": lats, "longitude": lons,
                            "start_date": s_pad, "end_date": e_pad,
                            "hourly": "temperature_2m",
                            "temperature_unit": "fahrenheit", "timezone": "UTC"})
        if fc is None or ac is None:
            print(f"  !! chunk {i//CHUNK} failed", flush=True)
            continue
        if isinstance(fc, dict):
            fc = [fc]
        if isinstance(ac, dict):
            ac = [ac]

        for j, city in enumerate(grp):
            tz = STATION_ICAO[city][1]
            try:
                f_ext = _local_day_extremes(fc[j]["hourly"]["time"],
                                            fc[j]["hourly"]["temperature_2m_previous_day1"], tz)
                a_ext = _local_day_extremes(ac[j]["hourly"]["time"],
                                            ac[j]["hourly"]["temperature_2m"], tz)
            except Exception as e:
                print(f"  !! {city}: {e}", flush=True)
                continue
            for d, (mx, mn) in f_ext.items():
                if start <= d <= end:
                    data[city].setdefault(d, {})["fc_max"] = mx
                    data[city][d]["fc_min"] = mn
            for d, (mx, mn) in a_ext.items():
                if start <= d <= end:
                    data[city].setdefault(d, {})["act_max"] = mx
                    data[city][d]["act_min"] = mn
        print(f"  chunk {i//CHUNK + 1}/{(len(cities)+CHUNK-1)//CHUNK}: {', '.join(grp)}", flush=True)

    written = skipped = 0
    for city, days in data.items():
        for d, rec in days.items():
            if rec.get("fc_max") is None or rec.get("act_max") is None:
                skipped += 1
                continue
            p = os.path.join(WX_DIR, f"{city.replace(' ','_')}_{d}.json")
            if os.path.exists(p):
                continue
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump({k: rec.get(k) for k in
                           ("fc_max", "fc_min", "act_max", "act_min")}, f)
            os.replace(tmp, p)
            written += 1
    print(f"\nwrote {written} new city-day files (incomplete skipped: {skipped})")
    print(f"cache now: {len(os.listdir(WX_DIR))} files")


if __name__ == "__main__":
    main()
