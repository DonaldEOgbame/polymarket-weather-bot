#!/usr/bin/env python3
"""
scripts/backtest_causal_deployed.py

Backtest of the CURRENTLY DEPLOYED strategy (fly.toml @ 79ae2eb) over the full
history Polymarket's Gamma API exposes, using a CAUSAL forecast.

WHY THIS EXISTS
---------------
scripts/backtest_forecast_margin.py gates on the *actual realized* daily extreme
(its own docstring calls it a "perfect forecast proxy" and warns it OVERSTATES
qualifying trades). That produces a 100% win rate because the gate already knows
the answer. This script replaces that gate with the day-before forecast that was
actually available at entry time, so the gate can be — and sometimes is — wrong.

CAUSALITY
---------
  * Forecast margin uses Open-Meteo previous-runs `temperature_2m_previous_day1`
    = the forecast issued ~1 day before target_date. No look-ahead.
  * Entries iterate the real Polymarket trade tape in ascending timestamp order.
  * The realized extreme is used ONLY to score the outcome, never to gate.

DEPLOYED CONFIG REPRODUCED (from fly.toml)
  TRADE_HIGH_MARKETS=true / TRADE_LOW_MARKETS=false   -> highs only
  ENABLE_YES_ENTRIES=false                            -> NO side only
  MIN_ENTRY_PRICE=0.90 / MAX_ENTRY_PRICE=0.95
  FORECAST_MARGIN_F=5.0  (config margin excludes BUCKET_EDGE_PAD_F=0.5,
                          so the effective clearance test is 4.5F)
  REQUIRE_SAME_DAY=true / MAX_HOURS_TO_RESOLUTION=16
  EXCLUDED_CITIES (26)

Outputs per-window win rates (7/14/30/90/365d), per-city distribution, and the
full loss list.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TAKER_FEE_RATE, BUCKET_EDGE_PAD_F  # noqa: E402
from metar import STATION_ICAO  # noqa: E402
from scanner import parse_bucket  # noqa: E402
from weather import STATIONS  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_causal_cache")
TAPE_DIR = os.path.join(CACHE, "tape")
WX_DIR = os.path.join(CACHE, "wx")

# --- Deployed config ---------------------------------------------------------
EXCLUDED_CITIES = set("""Ankara,Atlanta,Beijing,Buenos Aires,Cape Town,Chengdu,Chongqing,
Denver,Guangzhou,Hong Kong,Houston,Lagos,Lucknow,Milan,Munich,NYC,New York,Panama,
San Francisco,Sao Paulo,Seoul,Shenzhen,Taipei,Tel Aviv,Wuhan""".replace("\n", "").split(","))
MIN_ENTRY_PRICE = 0.90
MAX_ENTRY_PRICE = 0.95
FORECAST_MARGIN_F = 5.0
EFFECTIVE_CLEARANCE_F = FORECAST_MARGIN_F - BUCKET_EDGE_PAD_F  # 4.5
MAX_HOURS_TO_RESOLUTION = 16.0
STAKE = 3.0

TITLE_RE = re.compile(r"^(Highest|Lowest) temperature in (.+?) on ", re.I)

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (backtest)"})


class RateLimited(Exception):
    """Open-Meteo hourly quota exhausted — a missing forecast here is NOT a
    genuine 'no data' answer, so callers must not treat it as one."""


# Serialises all threads behind a single quota-exhaustion sleep.
_quota_lock = __import__("threading").Lock()
_quota_until = [0.0]


def _get(url, params=None, timeout=30, retries=4, wait_on_quota=False):
    """wait_on_quota=True: on a persistent 429, sleep until the hourly quota
    resets rather than returning None. Open-Meteo resets on the wall-clock hour."""
    backoff = 1.0
    for attempt in range(retries):
        # Respect a quota pause another thread already started.
        while True:
            wait = _quota_until[0] - time.time()
            if wait <= 0:
                break
            time.sleep(min(wait, 30))
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                if wait_on_quota:
                    with _quota_lock:
                        if _quota_until[0] <= time.time():
                            now = time.time()
                            # next wall-clock hour + 30s of slack
                            reset = (int(now // 3600) + 1) * 3600 + 30
                            _quota_until[0] = reset
                            print(f"    [quota] Open-Meteo hourly limit hit; sleeping "
                                  f"{int(reset - now)}s until reset", flush=True)
                    continue
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 422):
                return None
        except Exception:
            pass
        time.sleep(backoff)
        backoff *= 2
    if wait_on_quota:
        raise RateLimited(url)
    return None


# ============================================================================
# Discovery — paginate by date range to bypass Gamma's ~2000 offset cap
# ============================================================================

def _discover_window(cur, wnd_end, lo_date=None, hi_date=None):
    """Discover one date window. Returns a dict keyed by condition_id.
    lo_date/hi_date bound target_date: Gamma's start_date_* filters bound the
    event's START, so windows otherwise leak markets far outside the range."""
    out = {}
    offset = 0
    while offset <= 1500:
        d = _get(f"{GAMMA}/events", params={
            "tag_slug": "weather", "closed": "true", "limit": 100, "offset": offset,
            "start_date_min": cur.strftime("%Y-%m-%dT00:00:00Z"),
            "start_date_max": (wnd_end + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z"),
        })
        if not d:
            break
        for ev in d:
                m = TITLE_RE.match(ev.get("title") or "")
                if not m:
                    continue
                city = m.group(2).strip()
                is_high = m.group(1).lower() == "highest"
                end_iso = ev.get("endDate") or ""
                target_date = end_iso[:10]
                if not target_date:
                    continue
                if lo_date and target_date < lo_date:
                    continue
                if hi_date and target_date > hi_date:
                    continue
                for mk in ev.get("markets", []) or []:
                    cid = mk.get("conditionId")
                    q = mk.get("question") or ""
                    if not cid or cid in out:
                        continue
                    prices = mk.get("outcomePrices")
                    if isinstance(prices, str):
                        try:
                            prices = json.loads(prices)
                        except Exception:
                            prices = None
                    if not prices or len(prices) < 2:
                        continue
                    try:
                        yes_final = float(prices[0])
                    except Exception:
                        continue
                    lo, hi = parse_bucket(q)
                    if lo is None and hi is None:
                        continue
                    toks = mk.get("clobTokenIds")
                    if isinstance(toks, str):
                        try:
                            toks = json.loads(toks)
                        except Exception:
                            toks = None
                    if not toks or len(toks) < 2:
                        continue
                    out[cid] = {
                        "condition_id": cid, "city": city, "is_high": is_high,
                        "target_date": target_date, "question": q,
                        "bucket_low": lo, "bucket_high": hi,
                        "yes_final": yes_final, "token_no": toks[1],
                        "end_iso": end_iso,
                    }
        if len(d) < 100:
            break
        offset += 100
    return out


def discover_markets(start_date, end_date, workers=8):
    """Return daily temperature bucket markets, discovered in parallel windows."""
    cur = datetime.strptime(start_date, "%Y-%m-%d")
    stop = datetime.strptime(end_date, "%Y-%m-%d")
    windows = []
    while cur <= stop:
        wnd_end = min(cur + timedelta(days=6), stop)
        windows.append((cur, wnd_end))
        cur = wnd_end + timedelta(days=1)

    out, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_discover_window, a, b, start_date, end_date): (a, b)
                for a, b in windows}
        for f in as_completed(futs):
            done += 1
            try:
                out.update(f.result())
            except Exception:
                pass
            if done % 10 == 0:
                print(f"    discovered {done}/{len(windows)} windows | markets {len(out)}", flush=True)
    return list(out.values())


# ============================================================================
# Weather — causal forecast (prev-day run) + realized extreme (scoring only)
# ============================================================================

def _wx(city, target_date):
    """Return dict(fc_max, fc_min, act_max, act_min) in °F.
    fc_* come from the forecast run issued ~1 day before target_date (CAUSAL).
    act_* are the realized extremes and are used ONLY for scoring."""
    os.makedirs(WX_DIR, exist_ok=True)
    path = os.path.join(WX_DIR, f"{city.replace(' ','_')}_{target_date}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass

    st = STATIONS.get(city)
    if not st:
        return None
    icao = STATION_ICAO.get(city)
    tz = icao[1] if icao else "UTC"

    res = {"fc_max": None, "fc_min": None, "act_max": None, "act_min": None}

    d = _get(PREV_RUNS, params={
        "latitude": st["lat"], "longitude": st["lon"],
        "start_date": target_date, "end_date": target_date,
        "hourly": "temperature_2m_previous_day1",
        "temperature_unit": "fahrenheit", "timezone": tz,
    }, wait_on_quota=True)
    if d and d.get("hourly"):
        vals = [v for v in d["hourly"].get("temperature_2m_previous_day1", []) if v is not None]
        if vals:
            res["fc_max"], res["fc_min"] = max(vals), min(vals)

    d = _get(ARCHIVE, params={
        "latitude": st["lat"], "longitude": st["lon"],
        "start_date": target_date, "end_date": target_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit", "timezone": tz,
    }, wait_on_quota=True)
    if d and d.get("daily"):
        mx = d["daily"].get("temperature_2m_max") or []
        mn = d["daily"].get("temperature_2m_min") or []
        if mx and mx[0] is not None:
            res["act_max"] = float(mx[0])
        if mn and mn[0] is not None:
            res["act_min"] = float(mn[0])

    if res["fc_max"] is not None and res["act_max"] is not None:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(res, f)
        os.replace(tmp, path)
    return res


def forecast_margin_ok(mkt, wx):
    """CAUSAL gate: does the day-before forecast clear the bucket by >=4.5F
    in the NO direction? Open-ended buckets pass."""
    lo, hi = mkt["bucket_low"], mkt["bucket_high"]
    if mkt["is_high"]:
        fc = wx.get("fc_max")
        if fc is None:
            return False, None
        edge = lo if lo is not None else hi
        if edge is None:
            return True, None
        return (fc <= edge - EFFECTIVE_CLEARANCE_F), round(edge - fc, 2)
    fc = wx.get("fc_min")
    if fc is None:
        return False, None
    edge = hi if hi is not None else lo
    if edge is None:
        return True, None
    return (fc >= edge + EFFECTIVE_CLEARANCE_F), round(fc - edge, 2)


def no_won(mkt, wx):
    """Score the NO side against the realized extreme."""
    lo, hi = mkt["bucket_low"], mkt["bucket_high"]
    act = wx.get("act_max") if mkt["is_high"] else wx.get("act_min")
    if act is None:
        return None
    inside = True
    if lo is not None and act < lo:
        inside = False
    if hi is not None and act > hi:
        inside = False
    return not inside


# ============================================================================
# Trade tape
# ============================================================================

def tape(cid, token):
    os.makedirs(TAPE_DIR, exist_ok=True)
    path = os.path.join(TAPE_DIR, f"{cid}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    rows, offset = [], 0
    while offset <= 2000:
        d = _get(f"{DATA_API}/trades", params={"market": cid, "limit": 500, "offset": offset})
        if not d:
            break
        rows.extend(d)
        if len(d) < 500:
            break
        offset += 500
        time.sleep(0.05)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    os.replace(tmp, path)
    return rows


def simulate(mkt, stats=None):
    """First in-band NO print on the real tape, subject to the deployed gates.
    Records why each market was dropped so data gaps stay visible."""
    def _drop(reason):
        if stats is not None:
            stats[reason] += 1
        return None

    wx = _wx(mkt["city"], mkt["target_date"])
    if not wx:
        return _drop("no_station")
    if wx.get("fc_max") is None and wx.get("fc_min") is None:
        return _drop("no_forecast_data")
    ok, margin = forecast_margin_ok(mkt, wx)
    if not ok:
        return _drop("margin_gate")
    won = no_won(mkt, wx)
    if won is None:
        return _drop("no_actual_data")

    icao = STATION_ICAO.get(mkt["city"])
    tz = ZoneInfo(icao[1]) if icao else timezone.utc
    try:
        end_dt = datetime.strptime(mkt["end_iso"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None

    for t in sorted(tape(mkt["condition_id"], mkt["token_no"]), key=lambda x: x.get("timestamp", 0)):
        ts = t.get("timestamp")
        if not ts:
            continue
        try:
            ts = int(ts)
        except Exception:
            continue
        # Price is denominated in the row's own outcome. Prefer the explicit
        # `outcome` field; fall back to matching the NO token id.
        outcome = str(t.get("outcome") or "").strip().lower()
        try:
            raw = float(t.get("price", 0))
        except Exception:
            continue
        if outcome == "no" or str(t.get("asset")) == str(mkt["token_no"]):
            p = raw
        elif outcome == "yes":
            p = 1.0 - raw
        else:
            continue
        if not (MIN_ENTRY_PRICE <= p <= MAX_ENTRY_PRICE):
            continue
        entry = datetime.fromtimestamp(ts, timezone.utc)
        hours_left = (end_dt - entry).total_seconds() / 3600.0
        if hours_left <= 0 or hours_left > MAX_HOURS_TO_RESOLUTION:
            continue
        if entry.astimezone(tz).date().isoformat() != mkt["target_date"]:
            continue  # REQUIRE_SAME_DAY

        shares = STAKE / p
        fee = TAKER_FEE_RATE * p * (1 - p) * shares
        pnl = (shares * 1.0 - STAKE - fee) if won else (-STAKE - fee)
        return {
            "target_date": mkt["target_date"], "city": mkt["city"],
            "is_high": mkt["is_high"], "entry_price": round(p, 4),
            "entry_utc": entry.isoformat(), "hours_left": round(hours_left, 2),
            "forecast_margin_f": margin, "won": won, "net_pnl": round(pnl, 4),
            "bucket_low": mkt["bucket_low"], "bucket_high": mkt["bucket_high"],
            "question": mkt["question"], "condition_id": mkt["condition_id"],
            "fc": wx.get("fc_max") if mkt["is_high"] else wx.get("fc_min"),
            "actual": wx.get("act_max") if mkt["is_high"] else wx.get("act_min"),
        }
    return _drop("no_in_band_print")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-09-15")
    ap.add_argument("--end", default="2026-09-14")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", default="scripts/_causal_cache/ledger_causal.csv")
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    print(f"Discovering markets {args.start} -> {args.end} ...", flush=True)
    mkts = discover_markets(args.start, args.end, workers=args.workers)
    print(f"  daily temperature markets found: {len(mkts)}", flush=True)

    # Gamma's start_date_* filters bound the event's START, not its resolution
    # date, so windows leak markets far outside the requested range. Bound
    # target_date explicitly.
    mkts = [m for m in mkts if args.start <= m["target_date"] <= args.end]
    print(f"  within target_date range: {len(mkts)}", flush=True)

    elig = [m for m in mkts if m["city"] not in EXCLUDED_CITIES and m["is_high"]
            and m["city"] in STATIONS and m["city"] in STATION_ICAO]
    print(f"  after allowed-cities + highs-only: {len(elig)}", flush=True)

    rows, done = [], 0
    stats = Counter()
    rate_limited = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(simulate, m, stats): m for m in elig}
        for f in as_completed(futs):
            done += 1
            if done % 250 == 0:
                print(f"    simulated {done}/{len(elig)} | trades so far {len(rows)}", flush=True)
            try:
                r = f.result()
            except RateLimited:
                rate_limited += 1
                r = None
            except Exception:
                stats["error"] += 1
                r = None
            if r:
                rows.append(r)

    print("\n=== MARKET DISPOSITION (why markets did not become trades) ===")
    for k, v in stats.most_common():
        print(f"  {k:<22} {v}")
    if rate_limited:
        print(f"  !! RATE-LIMITED (data gap, NOT a real miss): {rate_limited}")
        print("  Results below are INCOMPLETE — re-run to fill the gap.")

    rows.sort(key=lambda r: (r["target_date"], r["city"]))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if rows:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nWrote {len(rows)} trades -> {args.out}")

    if not rows:
        print("No qualifying trades.")
        return

    last = datetime.strptime(max(r["target_date"] for r in rows), "%Y-%m-%d")
    print(f"\nCoverage: {min(r['target_date'] for r in rows)} -> {max(r['target_date'] for r in rows)}")

    print("\n=== WIN RATE BY WINDOW (deployed config, causal forecast) ===")
    print(f"{'window':>8} {'n':>6} {'wins':>6} {'losses':>7} {'win%':>8} {'P&L':>10} {'ROI':>8}")
    for label, days in [("7d", 7), ("14d", 14), ("30d", 30), ("90d", 90), ("365d", 365)]:
        cut = (last - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        sub = [r for r in rows if r["target_date"] >= cut]
        if not sub:
            print(f"{label:>8} {0:>6} {'-':>6} {'-':>7} {'n/a':>8} {'-':>10} {'-':>8}")
            continue
        w = sum(1 for r in sub if r["won"])
        pnl = sum(r["net_pnl"] for r in sub)
        print(f"{label:>8} {len(sub):>6} {w:>6} {len(sub)-w:>7} {w/len(sub)*100:>7.2f}% "
              f"{pnl:>9.2f} {pnl/(len(sub)*STAKE)*100:>7.2f}%")

    print("\n=== CITY DISTRIBUTION (full period) ===")
    print(f"{'city':<16} {'n':>5} {'wins':>5} {'losses':>7} {'win%':>8} {'P&L':>9}")
    by = defaultdict(list)
    for r in rows:
        by[r["city"]].append(r)
    for city, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        w = sum(1 for r in rs if r["won"])
        print(f"{city:<16} {len(rs):>5} {w:>5} {len(rs)-w:>7} {w/len(rs)*100:>7.1f}% "
              f"{sum(r['net_pnl'] for r in rs):>8.2f}")

    losses = [r for r in rows if not r["won"]]
    print(f"\n=== LOSSES ({len(losses)}) ===")
    for r in losses:
        b = f"[{r['bucket_low']},{r['bucket_high']}]"
        print(f"  {r['target_date']}  {r['city']:<14} p={r['entry_price']:<6} "
              f"bucket={b:<16} fc={r['fc']}  actual={r['actual']}  "
              f"margin={r['forecast_margin_f']}F  pnl={r['net_pnl']}")

    tot_w = sum(1 for r in rows if r["won"])
    tot_pnl = sum(r["net_pnl"] for r in rows)
    print(f"\n=== TOTAL === n={len(rows)} wins={tot_w} losses={len(rows)-tot_w} "
          f"win%={tot_w/len(rows)*100:.2f}% P&L=${tot_pnl:.2f} "
          f"ROI={tot_pnl/(len(rows)*STAKE)*100:.2f}%")


if __name__ == "__main__":
    main()
