#!/usr/bin/env python3
"""Auditability-first investigation of forecast-revision exits.

This deliberately refuses to manufacture historical forecast snapshots or bids.
It joins the existing causal entry/settlement ledger to timestamped station
observations and emits a per-trade audit showing exactly which exit inputs are
available.  It is the safe scaffold for a prospective shadow replay.
"""
import argparse, csv, json, os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from metar import STATION_ICAO


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="scripts/_causal_cache/ledger_365d_resettled.csv")
    ap.add_argument("--paths", default="scripts/_causal_cache/paths")
    ap.add_argument("--out", default="reports/forecast-revision-exit-audit.csv")
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.ledger)))
    out = []
    for r in rows:
        p = os.path.join(args.paths, r["city"].replace(" ", "_") + "_" + r["target_date"] + ".json")
        obs = json.load(open(p)) if os.path.exists(p) else []
        entry = ts(r["entry_utc"])
        lo = float(r["bucket_low"]) if r["bucket_low"] else None
        hi = float(r["bucket_high"]) if r["bucket_high"] else None
        # Station paths are Celsius; ledger buckets/actuals are Fahrenheit.
        tz_name = STATION_ICAO.get(r["city"], (None, "UTC"))[1]
        tz = ZoneInfo(tz_name)
        post = []
        for t, c in obs:
            local = datetime.fromisoformat(t).replace(tzinfo=tz)
            if local.timestamp() >= entry:
                post.append((t, float(c) * 9 / 5 + 32))
        running = None
        first_inside = first_above = None
        for t, f in post:
            running = f if running is None else max(running, f)
            if lo is not None and hi is not None:
                if first_inside is None and lo <= running <= hi:
                    first_inside = t
                if first_above is None and running > hi:
                    first_above = t
        out.append({
            "condition_id": r["condition_id"], "city": r["city"], "target_date": r["target_date"],
            "entry_utc": r["entry_utc"], "shares": "UNRECORDED", "bucket_low": r["bucket_low"],
            "bucket_high": r["bucket_high"], "settlement_source": "station path (audit only)",
            "forecast_at_entry": r["fc"], "forecast_revision_snapshots": "MISSING",
            "post_entry_observations": len(post), "first_observed_inside_or_path": first_inside or "",
            "first_observed_above": first_above or "", "historical_bid_depth": "MISSING",
            "decision": "UNTESTABLE_NO_CAUSAL_FORECAST_OR_BOOK",
            "outcome": "winner" if r["won"] == "True" else "loser",
        })
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    print(f"trades={len(out)} observation_paths={sum(bool(x['post_entry_observations']) for x in out)} "
          f"missing_paths={sum(not bool(x['post_entry_observations']) for x in out)}")
    print(f"audit={args.out}")


if __name__ == "__main__":
    main()
