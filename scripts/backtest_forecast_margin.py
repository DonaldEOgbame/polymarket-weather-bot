#!/usr/bin/env python3
"""
scripts/backtest_forecast_margin.py

Backtest of a NO-side temperature-margin strategy on Polymarket same-day-
settlement weather markets:

  STRATEGY:
    Buy NO on daily HIGH and LOW temperature bucket markets when:
    1. Entry is same-day (local calendar day matches target_date)
    2. Entry falls within a 4-hour window before the city's daily peak/trough
       lock-in hour (from REMAINING_RISE_TABLE: HIGH locks ~hour 15, LOW ~hour 21)
    3. The ACTUAL daily extreme (post-hoc, from Open-Meteo archive) was >=4.5°F
       clear of the bucket edge — "perfect forecast" proxy (see LIMITATIONS)
    4. The OBSERVED running extreme as of entry time (METAR, causal) is >=3.0°F
       clear of the bucket edge — zero look-ahead

  EXIT: Hold all positions to settlement (no stop-loss).
  SIZING: Flat $3.00 stake per trade (config.py default).

DATA SOURCES:
  1. Polymarket Gamma API: resolved event/market listing + resolved outcome
  2. Polymarket Data API /trades: real filled trade tape per token
  3. IEM ASOS archive (mesonet.agron.iastate.edu): historical METAR observations
     for observed clearance (same source the bot uses live)
  4. Open-Meteo Historical Archive API: actual hourly temperatures for the
     "perfect forecast" margin proxy

NO-LOOK-FORWARD-BIAS NOTES:
  - Gate #4 (forecast margin) uses the actual realized daily extreme, which is
    post-hoc information. This is NOT a causal filter — it is a "would a perfect
    forecast have passed this gate?" upper bound. A real forecast has error, so
    this OVERSTATES qualifying trades vs. what a live bot with a real forecast
    ensemble would have seen. Documented as the first limitation below.
  - Gate #3 (observed clearance) is strictly causal: only METAR observations
    with timestamps <= entry_ts are used, matching the v2 backtest design.
  - Entry selection iterates the trade tape in ascending timestamp order. No
    look-ahead, no sorting by price, no post-hoc selection of "best" entry.
  - The resolved outcome (outcomePrices from Gamma) is used ONLY for scoring.

FEES: TAKER_FEE_RATE * p * (1-p) per share, charged on entry only (no fee at
settlement, matching executor.py's settle_closed_trade formula).
"""

import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import csv as csv_module
import io
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date as _date, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy import transaction_cost  # noqa: E402
from config import TAKER_FEE_RATE, FIXED_POSITION_SIZE, REMAINING_RISE_TABLE  # noqa: E402
from metar import STATION_ICAO, MESONET_URL  # noqa: E402
from scanner import parse_bucket  # noqa: E402
from utils import safe_get  # noqa: E402
from weather import STATIONS  # noqa: E402

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

# --- Strategy parameters ---
FORECAST_MARGIN_F = 4.5          # >=4.5°F between actual extreme and bucket edge
OBSERVED_CLEARANCE_F = 3.0       # >=3.0°F between running observed extreme and bucket edge
LOCK_IN_WINDOW_HOURS = 4.0       # entry must be within 4h before lock-in hour
LOCK_IN_FRACTION_THRESHOLD = 0.02  # f_mean/g_mean below this = "locked in"

MAX_GAMMA_OFFSET = 2000
PAGE_LIMIT = 100
REQUEST_SLEEP_SEC = 0.08

CITY_ALLOWLIST = {
    "Dallas", "Toronto", "Buenos Aires", "Atlanta", "London",
    "Seattle", "Chicago", "Wellington", "Miami", "Beijing",
}

# Cache directories
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backtest_cache")
METAR_CACHE_DIR = os.path.join(CACHE_DIR, "metar")
ARCHIVE_CACHE_DIR = os.path.join(CACHE_DIR, "archive")

DAILY_TITLE_RE = re.compile(r'^(Highest|Lowest) temperature in (.+?) on ')

_metar_day_cache = {}
_archive_day_cache = {}


# ============================================================================
# Lock-in hour calculation (from REMAINING_RISE_TABLE)
# ============================================================================

def _lock_in_hour(is_high):
    """First local hour where REMAINING_RISE_TABLE's mean remaining fraction
    drops to/under LOCK_IN_FRACTION_THRESHOLD.
    HIGH markets use f_mean, LOW markets use g_mean."""
    key = "f_mean" if is_high else "g_mean"
    for hour in range(24):
        row = REMAINING_RISE_TABLE.get(hour)
        if row and row[key] <= LOCK_IN_FRACTION_THRESHOLD:
            return hour
    return None


def _within_lock_in_window(city, target_date, is_high, entry_ts):
    """True if entry_ts (unix seconds) falls within LOCK_IN_WINDOW_HOURS before
    the lock-in hour, in the station's own local time on target_date."""
    station = STATION_ICAO.get(city)
    if not station:
        return False
    _, tz_name = station
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return False
    entry_local = datetime.fromtimestamp(entry_ts, tz=tz)
    if entry_local.date().isoformat() != target_date:
        return False
    lock_hour = _lock_in_hour(is_high)
    if lock_hour is None:
        return False
    entry_hour = entry_local.hour + entry_local.minute / 60.0 + entry_local.second / 3600.0
    return (lock_hour - LOCK_IN_WINDOW_HOURS) <= entry_hour <= lock_hour


# ============================================================================
# METAR historical observations (for observed clearance — causal)
# ============================================================================

def _f_from_c(c):
    return c * 9.0 / 5.0 + 32.0


def _fetch_metar_day_series(icao, tz_name, date_str):
    """Every METAR temperature observation on the station's local calendar day,
    as (unix_ts, temp_f) pairs sorted by time. Disk-cached."""
    cache_key = (icao, date_str)
    if cache_key in _metar_day_cache:
        return _metar_day_cache[cache_key]

    os.makedirs(METAR_CACHE_DIR, exist_ok=True)
    disk_path = os.path.join(METAR_CACHE_DIR, f"{icao}_{date_str}.json")
    if os.path.exists(disk_path):
        with open(disk_path) as f:
            series = json.load(f)
        _metar_day_cache[cache_key] = series
        return series

    y, m, d = (int(x) for x in date_str.split("-"))
    nd = _date(y, m, d) + timedelta(days=1)
    params = {
        "station": icao, "data": "tmpc",
        "year1": y, "month1": m, "day1": d,
        "year2": nd.year, "month2": nd.month, "day2": nd.day,
        "tz": tz_name, "format": "onlycomma", "latlon": "no", "missing": "M",
    }
    series = []
    try:
        resp = safe_get(MESONET_URL, params=params, timeout=30)
        if resp.status_code == 200:
            tz = ZoneInfo(tz_name)
            for row in csv_module.DictReader(io.StringIO(resp.text)):
                valid = row.get("valid", "")
                if valid[:10] != date_str:
                    continue
                v = row.get("tmpc", "M")
                if v in ("M", ""):
                    continue
                try:
                    temp_c = float(v)
                    obs_local = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                    series.append((obs_local.timestamp(), _f_from_c(temp_c)))
                except (ValueError, TypeError):
                    continue
    except Exception as e:
        print(f"  [metar warning] METAR fetch failed for {icao} {date_str}: {e}", flush=True)

    series.sort(key=lambda x: x[0])
    tmp_path = f"{disk_path}.{os.getpid()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(series, f)
    os.replace(tmp_path, disk_path)
    _metar_day_cache[cache_key] = series
    return series


def _observed_extreme_as_of(city, target_date, is_high, entry_ts):
    """Running observed max/min using only METAR obs with timestamp <= entry_ts."""
    station = STATION_ICAO.get(city)
    if not station:
        return None
    icao, tz_name = station
    series = _fetch_metar_day_series(icao, tz_name, target_date)
    seen = [temp for ts, temp in series if ts <= entry_ts]
    if not seen:
        return None
    return max(seen) if is_high else min(seen)


def _observed_clearance_ok(market, entry_ts):
    """True if the running observed extreme is >=OBSERVED_CLEARANCE_F clear of
    the bucket edge in the NO direction. Open-ended buckets always pass."""
    bucket_low, bucket_high = market.get("bucket_low"), market.get("bucket_high")
    is_high = market["is_high"]

    obs = _observed_extreme_as_of(market["city"], market["target_date"], is_high, entry_ts)
    if obs is None:
        return False

    if is_high:
        # NO on HIGH: temp stays BELOW the bucket. Need observed max to be
        # well below the bucket's lower edge.
        if bucket_low is not None:
            return obs <= bucket_low - OBSERVED_CLEARANCE_F
        if bucket_high is not None:
            return obs <= bucket_high - OBSERVED_CLEARANCE_F
        return True  # open-ended
    else:
        # NO on LOW: temp stays ABOVE the bucket. Need observed min to be
        # well above the bucket's upper edge.
        if bucket_high is not None:
            return obs >= bucket_high + OBSERVED_CLEARANCE_F
        if bucket_low is not None:
            return obs >= bucket_low + OBSERVED_CLEARANCE_F
        return True  # open-ended


# ============================================================================
# Open-Meteo archive (for "perfect forecast" margin — post-hoc)
# ============================================================================

def _fetch_archive_extremes(city, target_date):
    """Fetch the actual daily max and min temperature (°F) for a city on
    target_date from the Open-Meteo Historical Archive API. Disk-cached.
    Returns (max_f, min_f) or (None, None) on failure."""
    cache_key = (city, target_date)
    if cache_key in _archive_day_cache:
        return _archive_day_cache[cache_key]

    os.makedirs(ARCHIVE_CACHE_DIR, exist_ok=True)
    disk_path = os.path.join(ARCHIVE_CACHE_DIR, f"{city}_{target_date}.json")
    if os.path.exists(disk_path):
        with open(disk_path) as f:
            data = json.load(f)
        result = (data.get("max_f"), data.get("min_f"))
        _archive_day_cache[cache_key] = result
        return result

    # Look up lat/lon from STATIONS
    station_info = STATIONS.get(city)
    if not station_info:
        _archive_day_cache[cache_key] = (None, None)
        return (None, None)

    lat, lon = station_info["lat"], station_info["lon"]

    # Get the station's timezone for proper local-day aggregation
    icao_info = STATION_ICAO.get(city)
    tz_name = icao_info[1] if icao_info else None

    url = (
        f"https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}"
        f"&start_date={target_date}&end_date={target_date}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit=fahrenheit"
    )
    if tz_name:
        url += f"&timezone={tz_name}"

    result = (None, None)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-bot-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            daily = data.get("daily", {})
            maxes = daily.get("temperature_2m_max", [])
            mins = daily.get("temperature_2m_min", [])
            if maxes and mins and maxes[0] is not None and mins[0] is not None:
                result = (float(maxes[0]), float(mins[0]))
            break  # success (even if data was empty)
        except Exception as e:
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            print(f"  [archive warning] Open-Meteo archive failed for {city} {target_date}: {e}", flush=True)

    # Only cache successes to disk (so failures can be retried on next run)
    if result[0] is not None:
        tmp_path = f"{disk_path}.{os.getpid()}.tmp"
        with open(tmp_path, "w") as f:
            json.dump({"max_f": result[0], "min_f": result[1]}, f)
        os.replace(tmp_path, disk_path)
    _archive_day_cache[cache_key] = result
    return result


def _forecast_margin_ok(market):
    """True if the actual realized daily extreme was >=FORECAST_MARGIN_F clear
    of the bucket edge in the NO direction. Post-hoc 'perfect forecast' proxy.
    Open-ended buckets always pass."""
    bucket_low, bucket_high = market.get("bucket_low"), market.get("bucket_high")
    is_high = market["is_high"]
    city = market["city"]
    target_date = market["target_date"]

    max_f, min_f = _fetch_archive_extremes(city, target_date)
    if max_f is None or min_f is None:
        return False

    if is_high:
        # NO on HIGH: the actual max must be well BELOW the bucket's lower edge
        actual = max_f
        if bucket_low is not None:
            return actual <= bucket_low - FORECAST_MARGIN_F
        if bucket_high is not None:
            return actual <= bucket_high - FORECAST_MARGIN_F
        return True
    else:
        # NO on LOW: the actual min must be well ABOVE the bucket's upper edge
        actual = min_f
        if bucket_high is not None:
            return actual >= bucket_high + FORECAST_MARGIN_F
        if bucket_low is not None:
            return actual >= bucket_low + FORECAST_MARGIN_F
        return True


# ============================================================================
# Gamma API: fetch resolved weather bucket markets
# ============================================================================

def _http_get_json(url, retries=4):
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-bot-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 422:
                raise
            last_exc = e
        except Exception as e:
            last_exc = e
        time.sleep(0.5 * (attempt + 1))
    raise last_exc


def fetch_resolved_weather_bucket_markets():
    """Page Gamma's closed 'weather' tag events, keep only daily temperature
    bucket events for CITY_ALLOWLIST, flatten to one row per sub-market with
    its resolved winner."""
    rows = []
    seen_condition_ids = set()
    offset = 0
    n_events_seen = 0
    n_daily_events = 0
    earliest_end = None
    latest_end = None

    while offset <= MAX_GAMMA_OFFSET:
        url = (f"{GAMMA_BASE}/events?tag_slug=weather&closed=true&limit={PAGE_LIMIT}"
               f"&order=endDate&ascending=false&offset={offset}")
        try:
            page = _http_get_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                break
            raise
        if not page:
            break
        n_events_seen += len(page)

        for ev in page:
            title = ev.get("title", "") or ""
            m = DAILY_TITLE_RE.match(title)
            if not m:
                continue
            n_daily_events += 1
            direction, city_raw = m.group(1), m.group(2)
            is_high = (direction == "Highest")
            city_name = city_raw.strip()
            if city_name not in CITY_ALLOWLIST:
                continue
            end_iso = ev.get("endDate")
            if end_iso:
                if latest_end is None or end_iso > latest_end:
                    latest_end = end_iso
                if earliest_end is None or end_iso < earliest_end:
                    earliest_end = end_iso

            date_match = re.search(r'on-([a-z]+)-(\d{1,2})-(\d{4})', ev.get("slug", "") or "")
            target_date = None
            if date_match:
                month_name, day, year = date_match.groups()
                try:
                    dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
                    target_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    target_date = None

            for sub in ev.get("markets", []) or []:
                cond_id = sub.get("conditionId")
                if not cond_id or cond_id in seen_condition_ids:
                    continue
                if sub.get("umaResolutionStatus") != "resolved" and not sub.get("closed"):
                    continue
                try:
                    token_ids = json.loads(sub.get("clobTokenIds") or "[]")
                    outcomes = json.loads(sub.get("outcomes") or "[]")
                    outcome_prices = json.loads(sub.get("outcomePrices") or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
                if len(token_ids) != 2 or len(outcomes) != 2 or len(outcome_prices) != 2:
                    continue

                try:
                    yes_idx = outcomes.index("Yes")
                except ValueError:
                    continue
                no_idx = 1 - yes_idx
                yes_price_resolved = float(outcome_prices[yes_idx])
                no_price_resolved = float(outcome_prices[no_idx])

                resolved_side = None
                if yes_price_resolved >= 0.99:
                    resolved_side = "YES"
                elif no_price_resolved >= 0.99:
                    resolved_side = "NO"

                question_text = sub.get("question") or ""
                try:
                    bucket_low, bucket_high = parse_bucket(question_text)
                except Exception:
                    bucket_low, bucket_high = None, None

                seen_condition_ids.add(cond_id)
                rows.append({
                    "city": city_name,
                    "is_high": is_high,
                    "target_date": target_date,
                    "question": question_text,
                    "condition_id": cond_id,
                    "yes_token_id": token_ids[yes_idx],
                    "no_token_id": token_ids[no_idx],
                    "resolved_side": resolved_side,
                    "bucket_label": sub.get("groupItemTitle"),
                    "bucket_low": bucket_low,
                    "bucket_high": bucket_high,
                    "event_end_iso": end_iso,
                })

        offset += PAGE_LIMIT
        print(f"  ... Gamma page fetched, offset now {offset}, daily events so far: {n_daily_events}", flush=True)
        time.sleep(REQUEST_SLEEP_SEC)

    meta = {
        "events_seen": n_events_seen,
        "daily_events_seen": n_daily_events,
        "earliest_event_end": earliest_end,
        "latest_event_end": latest_end,
        "gamma_offset_cap_hit": offset > MAX_GAMMA_OFFSET,
    }
    return rows, meta


# ============================================================================
# Trade tape fetching
# ============================================================================

def fetch_trade_tape(condition_id, cache):
    """Full trade tape from Data API, cached to disk."""
    if condition_id in cache:
        return cache[condition_id]

    cache_path = os.path.join(CACHE_DIR, f"{condition_id}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            trades = json.load(f)
        cache[condition_id] = trades
        return trades

    all_trades = []
    offset = 0
    limit = 500
    while True:
        url = f"{DATA_API_BASE}/trades?market={condition_id}&limit={limit}&offset={offset}"
        try:
            page = _http_get_json(url)
        except Exception:
            break
        if not page:
            break
        all_trades.extend(page)
        if len(page) < limit:
            break
        offset += limit
        time.sleep(REQUEST_SLEEP_SEC)

    all_trades.sort(key=lambda t: t.get("timestamp", 0))
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = f"{cache_path}.{os.getpid()}.{id(all_trades)}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(all_trades, f)
    os.replace(tmp_path, cache_path)
    cache[condition_id] = all_trades
    return all_trades


# ============================================================================
# Market simulation
# ============================================================================

def fee_rate_fee(price, fee_rate):
    """Polymarket taker fee per share."""
    return fee_rate * price * (1.0 - price)


def simulate_market(market, cache, stake_usd, fee_rate):
    """Walk the trade tape forward in time, find the first qualifying NO BUY
    entry that passes all filters, hold to settlement."""
    if market["resolved_side"] is None:
        return None
    if market["target_date"] is None:
        return None

    # Pre-check: does the forecast margin pass for this market?
    # (Post-hoc — entire day's actual extreme. Not causal, but the most honest
    # proxy we have for "would a >=4.5°F forecast margin have been satisfied?")
    if not _forecast_margin_ok(market):
        return None

    trades_by_token = {
        market["yes_token_id"]: ("YES",),
        market["no_token_id"]: ("NO",),
    }

    raw_trades = fetch_trade_tape(market["condition_id"], cache)
    if not raw_trades:
        return None

    tape = []
    for t in raw_trades:
        asset = t.get("asset")
        side_label = trades_by_token.get(asset)
        if not side_label:
            continue
        try:
            price = float(t["price"])
            ts = int(t["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        tape.append({
            "side_label": side_label[0],
            "trade_side": t.get("side"),
            "price": price,
            "ts": ts,
        })
    tape.sort(key=lambda x: x["ts"])
    if not tape:
        return None

    # --- ENTRY SCAN: first BUY print on NO side, passing all filters ---
    entry = None
    for tick in tape:
        if tick["trade_side"] != "BUY":
            continue
        if tick["side_label"] != "NO":
            continue
        # Same-day check (entry on target_date in local time)
        if not _within_lock_in_window(market["city"], market["target_date"],
                                      market["is_high"], tick["ts"]):
            continue
        # Observed clearance (causal — only METAR obs <= entry_ts)
        if not _observed_clearance_ok(market, tick["ts"]):
            continue
        entry = tick
        break

    if entry is None:
        return None

    entry_price = entry["price"]
    entry_ts = entry["ts"]
    shares = stake_usd / entry_price
    entry_fee = fee_rate_fee(entry_price, fee_rate) * shares

    # --- HOLD TO SETTLEMENT ---
    won = (market["resolved_side"] == "NO")
    settle_price = 1.0 if won else 0.0
    gross_pnl = (settle_price - entry_price) * shares
    net_pnl = gross_pnl - entry_fee

    return {
        "market": market,
        "entry_side": "NO",
        "entry_price": entry_price,
        "entry_ts": entry_ts,
        "exit_reason": "settlement",
        "exit_price": settle_price,
        "shares": shares,
        "stake_usd": stake_usd,
        "fees_usd": entry_fee,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "won": won,
    }


# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 100)
    print("BACKTEST: NO on High/Low markets | >=4.5°F forecast margin | >=3.0°F observed clearance")
    print("         Same-day settlement | 4-hour lock-in window | Hold to settlement (no stop-loss)")
    print("=" * 100)

    stake_usd = FIXED_POSITION_SIZE
    fee_rate = TAKER_FEE_RATE
    print(f"\nFlat stake per trade: ${stake_usd:.2f}")
    print(f"Fee model: TAKER_FEE_RATE={fee_rate:.3f} (fee/share = rate * p * (1-p)), entry only, no fee at settlement")
    print(f"Forecast margin threshold: >= {FORECAST_MARGIN_F}°F (post-hoc 'perfect forecast' proxy)")
    print(f"Observed clearance threshold: >= {OBSERVED_CLEARANCE_F}°F (causal METAR, no look-ahead)")
    print(f"Lock-in window: {LOCK_IN_WINDOW_HOURS}h before lock-in hour")

    # Print lock-in hours
    high_lock = _lock_in_hour(True)
    low_lock = _lock_in_hour(False)
    print(f"  HIGH markets: lock-in hour = {high_lock} (local), entry window = {high_lock - LOCK_IN_WINDOW_HOURS:.0f}:00 – {high_lock}:00")
    print(f"  LOW  markets: lock-in hour = {low_lock} (local), entry window = {low_lock - LOCK_IN_WINDOW_HOURS:.0f}:00 – {low_lock}:00")
    print(f"Cities: {', '.join(sorted(CITY_ALLOWLIST))}")

    print("\nFetching resolved daily temperature-bucket markets from Polymarket Gamma API...")
    markets, meta = fetch_resolved_weather_bucket_markets()
    print(f"  Gamma events scanned: {meta['events_seen']} (offset cap hit: {meta['gamma_offset_cap_hit']})")
    print(f"  Daily per-city temperature events matched: {meta['daily_events_seen']}")
    print(f"  Event end-date range covered: {meta['earliest_event_end']} .. {meta['latest_event_end']}")
    print(f"  Distinct resolved bucket sub-markets collected: {len(markets)}")

    resolved_markets = [m for m in markets if m["resolved_side"] is not None]
    unresolved_dropped = len(markets) - len(resolved_markets)
    print(f"  Cleanly resolved: {len(resolved_markets)}")
    print(f"  Dropped (unresolved): {unresolved_dropped}")

    # Split by high/low for reporting
    high_markets = [m for m in resolved_markets if m["is_high"]]
    low_markets = [m for m in resolved_markets if not m["is_high"]]
    print(f"  HIGH markets: {len(high_markets)}, LOW markets: {len(low_markets)}")

    # Pre-fetch archive extremes for all unique (city, target_date) pairs
    # to batch the Open-Meteo calls and show progress
    unique_city_dates = list({(m["city"], m["target_date"]) for m in resolved_markets if m["target_date"]})
    print(f"\nPre-fetching Open-Meteo archive extremes for {len(unique_city_dates)} unique (city, date) pairs...")
    archive_fetched = 0
    archive_failed = 0
    archive_cached = 0
    for i, (city, td) in enumerate(unique_city_dates):
        # Check disk cache first to skip network calls
        disk_path = os.path.join(ARCHIVE_CACHE_DIR, f"{city}_{td}.json")
        if os.path.exists(disk_path):
            archive_cached += 1
        else:
            time.sleep(0.25)  # rate limit Open-Meteo
        max_f, min_f = _fetch_archive_extremes(city, td)
        if max_f is not None:
            archive_fetched += 1
        else:
            archive_failed += 1
        if (i + 1) % 25 == 0:
            print(f"  ... {i+1}/{len(unique_city_dates)} ({archive_cached} cached, "
                  f"{archive_fetched} OK, {archive_failed} failed)", flush=True)
    print(f"  Done: {archive_fetched} fetched, {archive_cached} from cache, {archive_failed} failed")

    print(f"\nSimulating {len(resolved_markets)} markets (fetching trade tapes + METAR in parallel)...")
    cache = {}

    def _fetch_and_simulate(m):
        try:
            tape = fetch_trade_tape(m["condition_id"], cache)
        except Exception as e:
            return (m, None, f"fetch failed: {e}")
        try:
            r = simulate_market(m, cache, stake_usd, fee_rate)
        except Exception as e:
            return (m, None, f"simulate failed: {e}")
        return (m, r, None)

    results = []
    no_trade_tape = 0
    no_entry = 0
    forecast_margin_filtered = 0
    done_count = 0
    MAX_WORKERS = 24
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_fetch_and_simulate, m) for m in resolved_markets]
        for fut in as_completed(futures):
            m, r, err = fut.result()
            done_count += 1
            if done_count % 250 == 0:
                print(f"  ... processed {done_count}/{len(resolved_markets)} markets "
                      f"(qualifying trades so far: {len(results)})", flush=True)
            if err:
                continue
            if r is None:
                tape = cache.get(m["condition_id"])
                if not tape:
                    no_trade_tape += 1
                else:
                    no_entry += 1
                continue
            results.append(r)

    print(f"\nMarkets with no trade tape: {no_trade_tape}")
    print(f"Markets with tape but no qualifying entry: {no_entry}")
    print(f"QUALIFYING TRADES (entries taken): {len(results)}")

    if not results:
        print("\nNo trades met the entry criteria. Nothing further to report.")
        return

    # ---- Aggregate stats ----
    n = len(results)
    wins = [r for r in results if r["won"]]
    losses = [r for r in results if not r["won"]]

    total_staked = sum(r["stake_usd"] for r in results)
    total_net_pnl = sum(r["net_pnl"] for r in results)
    total_gross_pnl = sum(r["gross_pnl"] for r in results)
    total_fees = sum(r["fees_usd"] for r in results)
    win_rate = len(wins) / n * 100.0

    win_pnls = [r["net_pnl"] for r in wins]
    loss_pnls = [r["net_pnl"] for r in losses]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0

    # Profit factor
    gross_wins = sum(win_pnls) if win_pnls else 0.0
    gross_losses = abs(sum(loss_pnls)) if loss_pnls else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    # Running P&L / max drawdown
    results_sorted = sorted(results, key=lambda r: r["entry_ts"])
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve = []
    for r in results_sorted:
        running += r["net_pnl"]
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)
        equity_curve.append(running)

    roi_pct = (total_net_pnl / total_staked) * 100.0 if total_staked > 0 else 0.0

    print("\n" + "=" * 100)
    print("RESULTS SUMMARY")
    print("=" * 100)
    print(f"Total qualifying trades:        {n}")
    print(f"Win rate:                        {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Total staked:                    ${total_staked:,.2f}")
    print(f"Gross P&L (before fees):         ${total_gross_pnl:+,.2f}")
    print(f"Total fees:                      ${total_fees:,.2f}")
    print(f"Net P&L (after fees):            ${total_net_pnl:+,.2f}")
    print(f"Return on staked capital:        {roi_pct:+.2f}%")
    print(f"Average win (net $):             ${avg_win:+.3f}")
    print(f"Average loss (net $):            ${avg_loss:+.3f}")
    print(f"Profit factor:                   {profit_factor:.2f}")
    print(f"Max drawdown (running net $):    ${max_dd:,.2f}")

    print(f"\nSample-size verdict: {n} trades. Project convention: 'don't trust under ~50 trades.'")
    if n < 50:
        print(f"  --> {n} < 50: THIS SAMPLE IS TOO SMALL TO TRUST. Treat stats as preliminary/noisy.")
    else:
        print(f"  --> {n} >= 50: meets minimum bar for a trustworthy read.")

    # ---- By market type (HIGH vs LOW) ----
    print("\n" + "-" * 100)
    print("BY MARKET TYPE (HIGH vs LOW)")
    print("-" * 100)
    for mtype, label in [(True, "HIGH"), (False, "LOW")]:
        rs = [r for r in results if r["market"]["is_high"] == mtype]
        if not rs:
            print(f"  {label}: 0 trades")
            continue
        w = sum(1 for r in rs if r["won"])
        net = sum(r["net_pnl"] for r in rs)
        staked = sum(r["stake_usd"] for r in rs)
        avg_px = sum(r["entry_price"] for r in rs) / len(rs)
        print(f"  {label:4}: {len(rs):4} trades, {w}W/{len(rs)-w}L ({w/len(rs)*100:.1f}%), "
              f"Net ${net:+,.2f}, ROI {net/staked*100:+.1f}%, AvgEntryPx {avg_px:.3f}")

    # ---- By-city breakdown ----
    by_city = defaultdict(list)
    for r in results:
        by_city[r["market"]["city"]].append(r)

    print("\n" + "-" * 100)
    print(f"{'City':<18} {'Trades':<8} {'Wins':<6} {'Win%':<8} {'NetPnL':<12} {'AvgEntryPx':<11} {'High':<6} {'Low':<6}")
    print("-" * 100)
    for city in sorted(by_city.keys(), key=lambda c: -sum(r["net_pnl"] for r in by_city[c])):
        rs = by_city[city]
        w = sum(1 for r in rs if r["won"])
        net = sum(r["net_pnl"] for r in rs)
        avg_px = sum(r["entry_price"] for r in rs) / len(rs)
        n_high = sum(1 for r in rs if r["market"]["is_high"])
        n_low = len(rs) - n_high
        print(f"{city:<18} {len(rs):<8} {w:<6} {w/len(rs)*100:<7.1f}% ${net:<+10.3f} {avg_px:<11.3f} {n_high:<6} {n_low:<6}")

    # ---- Per-trade ledger ----
    print("\n" + "-" * 100)
    print("TRADE LEDGER (chronological by entry time)")
    print("-" * 100)
    header = (f"{'EntryTime(UTC)':<20} {'City':<14} {'H/L':<4} {'EntryPx':<8} "
              f"{'NetPnL':<9} {'Won':<5} {'Bucket':<15}")
    print(header)
    to_show = results_sorted if n <= 80 else results_sorted[:60] + results_sorted[-10:]
    for r in to_show:
        et = datetime.fromtimestamp(r["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mkt = r["market"]
        hl = "HIGH" if mkt["is_high"] else "LOW"
        bl = mkt.get("bucket_label", "") or ""
        print(f"{et:<20} {mkt['city']:<14} {hl:<4} {r['entry_price']:<8.3f} "
              f"{r['net_pnl']:<+9.3f} {str(r['won']):<5} {bl:<15}")
    if n > 80:
        print(f"... ({n - 70} trades omitted; full CSV has every trade) ...")

    # ---- CSV export ----
    csv_path = os.path.join(CACHE_DIR, "ledger_forecast_margin.csv")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_time_utc", "city", "target_date", "is_high", "side", "entry_price",
                     "exit_reason", "exit_price", "net_pnl", "won", "condition_id",
                     "bucket_low", "bucket_high", "bucket_label", "open_ended",
                     "forecast_margin_f", "observed_clearance_f"])
        for r in results_sorted:
            et = datetime.fromtimestamp(r["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            mkt = r["market"]
            b_lo, b_hi = mkt.get("bucket_low"), mkt.get("bucket_high")

            # Compute the actual margins for the CSV
            max_f, min_f = _fetch_archive_extremes(mkt["city"], mkt["target_date"])
            if mkt["is_high"]:
                edge = b_lo if b_lo is not None else b_hi
                fc_margin = (edge - max_f) if (edge is not None and max_f is not None) else None
            else:
                edge = b_hi if b_hi is not None else b_lo
                fc_margin = (min_f - edge) if (edge is not None and min_f is not None) else None

            obs = _observed_extreme_as_of(mkt["city"], mkt["target_date"], mkt["is_high"], r["entry_ts"])
            if mkt["is_high"]:
                obs_margin = (edge - obs) if (edge is not None and obs is not None) else None
            else:
                obs_margin = (obs - edge) if (edge is not None and obs is not None) else None

            w.writerow([et, mkt["city"], mkt.get("target_date", ""),
                        mkt.get("is_high", ""), r["entry_side"], r["entry_price"],
                        r["exit_reason"], r["exit_price"], r["net_pnl"], r["won"],
                        mkt.get("condition_id", ""), b_lo, b_hi, mkt.get("bucket_label", ""),
                        b_lo is None or b_hi is None,
                        f"{fc_margin:.1f}" if fc_margin is not None else "",
                        f"{obs_margin:.1f}" if obs_margin is not None else ""])
    print(f"\nFull ledger ({n} trades) written to: {csv_path}")

    # ---- Limitations ----
    print("\n" + "=" * 100)
    print("LIMITATIONS / CAVEATS")
    print("=" * 100)
    print("""
1. FORECAST MARGIN IS POST-HOC ("PERFECT FORECAST" PROXY).
   No historical forecast archive exists (the bot's replay_signals table is
   empty, and Open-Meteo only serves the current live forecast). This backtest
   uses the ACTUAL realized daily extreme from Open-Meteo's historical archive
   as a proxy for "would a >=4.5°F forecast margin have been satisfied?"
   This OVERSTATES qualifying trades: a real forecast has error, so some trades
   that passed here (because the ACTUAL extreme was >=4.5°F clear) would NOT
   have passed a real forecast margin check (the forecast might have been closer
   to the bucket edge). Think of this as an UPPER BOUND on what the strategy
   can do with a perfect weather model.

2. OBSERVED CLEARANCE IS CAUSAL AND HONEST.
   The >=3.0°F observed clearance filter uses the IEM ASOS METAR archive with
   a strict ts <= entry_ts cutoff. Only observations timestamped before or at
   the entry moment are used. This filter introduces zero look-ahead bias.

3. NO PRICE BAND RESTRICTION.
   Any BUY print on the NO token qualifies as an entry (no 0.90-0.92 band).
   This means entries can occur at any price, including very low prices where
   the NO side is not yet favored. The forecast margin + observed clearance
   gates are the primary filters, not the price.

4. TRADE TAPE PROXY (same as v2).
   No historical order-book API exists. BUY prints proxy for "an ask was
   crossable at this price." This is real transacted data, not synthetic, but
   assumes our order would have matched at the same price.

5. COVERAGE LIMITED BY GAMMA API.
   The Gamma /events endpoint 422s past offset=2000, capping history to roughly
   the most recent ~3 weeks of resolved weather markets.

6. FEES: TAKER_FEE_RATE * p * (1-p) per share, entry only. No fee at settlement.
   Flat $3.00 stake (config.py default).
""")


if __name__ == "__main__":
    main()
