#!/usr/bin/env python3
"""Fetch timestamped METAR observation series (the SETTLEMENT ruler) for the
city-days that matter to the exit question: all losses, all overshoot winners,
and all inside/above-bucket cases.

Unlike metar.fetch_day_extremes (which returns only max/min), this keeps every
observation with its local timestamp so intraday paths can be reconstructed:
first bucket entry, first upper-bound exceedance, time between, and what the
running maximum was at any decision time.

Sparse-sampling honesty: METAR is typically hourly (sometimes 20/30-min SPECI).
We therefore record OBSERVED crossings only, and flag whether consecutive
observations jumped across a boundary (unconfirmed path) vs. were observed
inside it (confirmed).
"""
import csv, io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metar import STATION_ICAO, MESONET_URL, _iem_station  # noqa: E402
from utils import safe_get  # noqa: E402

OUT = "scripts/_causal_cache/paths"
os.makedirs(OUT, exist_ok=True)


def fetch_series(city, date_str):
    """[(local_iso, temp_c)] for the station's local calendar day."""
    p = os.path.join(OUT, f"{city.replace(' ','_')}_{date_str}.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    info = STATION_ICAO.get(city)
    if not info:
        return None
    icao, tz = info
    y, m, d = (int(x) for x in date_str.split("-"))
    nd = _date(y, m, d) + timedelta(days=1)
    params = {"station": _iem_station(icao), "data": "tmpc",
              "year1": y, "month1": m, "day1": d,
              "year2": nd.year, "month2": nd.month, "day2": nd.day,
              "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "M"}
    out = []
    try:
        resp = safe_get(MESONET_URL, params=params, timeout=45)
        if resp.status_code == 200:
            for row in csv.DictReader(io.StringIO(resp.text)):
                ts = (row.get("valid") or "").strip()
                if not ts.startswith(date_str):
                    continue
                raw = (row.get("tmpc") or "").strip()
                if raw in ("", "M", "null"):
                    continue
                try:
                    out.append([ts, float(raw)])
                except ValueError:
                    continue
    except Exception as e:
        print(f"  [err] {city} {date_str}: {e}", flush=True)
        return None
    out.sort(key=lambda x: x[0])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, p)
    return out


def main():
    rows = list(csv.DictReader(open("scripts/_causal_cache/ledger_365d.csv")))

    def F(r, k):
        try:
            return float(r[k])
        except Exception:
            return None

    def interesting(r):
        lo, hi, act = F(r, "bucket_low"), F(r, "bucket_high"), F(r, "actual")
        if r["won"] != "True":
            return True                      # every loss
        if act is None or lo is None or hi is None:
            return False
        return act >= lo                     # winners that reached or passed the bucket

    keys = sorted({(r["city"], r["target_date"]) for r in rows if interesting(r)})
    print(f"city-days to fetch: {len(keys)}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch_series, c, d): (c, d) for c, d in keys}
        for f in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  fetched {done}/{len(keys)}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
