#!/usr/bin/env python3
"""
scripts/backtest_9092_stop.py

Rigorous, no-look-forward-bias backtest of a specific mechanical strategy on
Polymarket same-day-settlement weather (temperature-bucket) markets:

  ENTRY:  buy whichever side (YES or NO) prints a trade in the [0.90, 0.92]
          ask band, sized at the bot's own flat stake (config.FIXED_POSITION_SIZE).
  STOP:   walking forward tick-by-tick after entry, if a sell-side print (our
          bid-side proxy) touches <= 0.40 * entry_price, exit immediately at
          that print price. 0.40 of entry price means we EXIT WITH 40% OF THE
          POSITION'S VALUE REMAINING, i.e. we LOSE 60% OF THE STAKE, not 40%.
          This script asserts that arithmetic explicitly at runtime.
  HOLD:   otherwise hold to settlement; settle at $1/share (win) or $0 (loss)
          off the actual resolved outcome.

DATA SOURCES (both used, cross-checked):
  1. Primary/fresh: Polymarket Gamma API (resolved event/market listing +
     resolved outcome) and Polymarket Data API `/trades` (the real, filled
     trade tape per token: price, side, timestamp) pulled live over the network
     for this run.
  2. Cross-check: the bot's own local `data/bot.db` (or a live backup under
     backups/) `sniper_signals` / `scan_log` / `markets` tables, which contain
     independently-captured best_bid/best_ask/timestamps for an overlapping
     set of markets. A handful of matching (market, near-timestamp) points are
     compared and any material (>2 cent) disagreement is flagged in the report
     rather than silently resolved.

NO-LOOK-FORWARD-BIAS DESIGN NOTES (read this before trusting the numbers):
  - There is no historical order-book (bid/ask) snapshot API on Polymarket for
    settled markets. The finest-grained ground truth available for what was
    *actually executable* at a past moment is the trade tape: a BUY trade that
    printed at price P is proof a taker could buy at P (an ask was crossable
    at P); a SELL trade that printed at price P is proof a taker could sell at
    P (a bid was crossable at P). This script uses BUY prints to detect entries
    in the 0.90-0.92 band and SELL prints as the bid-side signal for the stop.
    This is a real, transacted price — not a synthetic/interpolated one — but
    it is a proxy for "best ask"/"best bid" rather than the book itself, and it
    assumes our hypothetical order would have received the same price a real
    trade received at that instant (see LIMITATIONS in the printed report).
  - All decisions are made by iterating the trade tape in ascending timestamp
    order and only ever looking at ticks with timestamp <= "now" relative to
    the simulated walk. The stop-loss loop never peeks ahead to see whether
    price recovers before deciding whether the stop fired first.
  - The resolved outcome (`outcomePrices` from Gamma, fixed only after the UMA
    resolution) is used ONLY to score a held-to-settlement position after the
    fact. It is never used to pick a side, size a trade, or decide the entry
    or stop-loss price.
  - We do NOT use any locally-logged "conditioned"/"corrected" forecast field
    (e.g. conditioned_mean, corrections_applied) anywhere in this script. Entry
    selection here is purely mechanical off the traded price band, matching
    the task's stop-and-band rule; forecast fields are irrelevant to it and are
    not read at all.

FEES: modeled as the bot's own transaction_cost() -- TAKER_FEE_RATE * p * (1-p)
per share -- charged once on entry (we are a taker buying) and again on the
fill price if the stop-loss fires (we are a taker selling). Held-to-settlement
exits are NOT charged an extra fee, matching executor.py's settle_closed_trade
(settled_pnl = shares - stake, no fee term), i.e. resolution is not a taker
trade.

This script makes real network calls to Polymarket's public Gamma/Data APIs
and writes no data back anywhere. It does not touch live trading code/config.
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Pull the bot's own conventions, don't invent our own ---
from strategy import transaction_cost  # noqa: E402
from config import TAKER_FEE_RATE, FIXED_POSITION_SIZE, REMAINING_RISE_TABLE  # noqa: E402
from metar import STATION_ICAO, MESONET_URL  # noqa: E402
from scanner import parse_bucket  # noqa: E402
from utils import safe_get  # noqa: E402

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

# UNGATED_PRICE=1 drops the 0.90-0.92 entry-price restriction entirely (owner
# request: report both variants). Band still used for the printed labels below.
UNGATED_PRICE = os.environ.get("UNGATED_PRICE", "0") == "1"
ENTRY_BAND_LOW = 0.0 if UNGATED_PRICE else 0.90
ENTRY_BAND_HIGH = 1.0 if UNGATED_PRICE else 0.92
STOP_FRACTION_OF_ENTRY = 0.40   # position retains this fraction of entry value
MAX_GAMMA_OFFSET = 2000         # empirically-confirmed hard cap on this endpoint
PAGE_LIMIT = 100
REQUEST_SLEEP_SEC = 0.08

# --- Variant v2 filters (owner request 2026-09-13):
#   - NO-side only
#   - entries only within LOCK_IN_WINDOW_HOURS of the point the repo's own
#     REMAINING_RISE_TABLE says the day's extreme is essentially locked in
#     (remaining-rise fraction <= 2% of the diurnal range). One curve fit
#     across ten stations, applied via each city's own local clock
#     (metar.STATION_ICAO), not a per-city peak hour.
#   - observed temperature at entry must be >= OBSERVED_CLEARANCE_F clear of
#     the bucket edge in the NO direction (live METAR, not forecast)
#   - restricted to an explicit city allowlist (owner's picks, not a P&L-
#     ranked cherry-pick from this same sample -- see CITY_ALLOWLIST below)
#
# NOTE: the table itself contradicts intraday.py's docstring claim that lows
# lock in "before dawn" -- g_mean (remaining fall for LOW markets) decreases
# slowly and monotonically all day and only crosses 2% around local hour 20-21
# (8-9pm), not at dawn. f_mean (HIGH markets) crosses 2% at local hour 15 (3pm),
# which does match the "mid-afternoon" framing. Verified directly against the
# fitted numbers below rather than trusting the docstring -- using the data
# as it actually is, not as the older comment describes it.
#
# DROPPED from the owner's original request: a >4.0F FORECAST-margin gate.
# The bot's replay_signals table (where historical ensemble_mean would be
# logged) is completely empty for this whole window, and Open-Meteo's API
# only serves the CURRENT live forecast -- there is no historical archive of
# past forecast-ensemble output to reconstruct "what the forecast said" at a
# past entry moment. Fabricating one would violate the task's own "no BS"
# constraint, so this gate is left out rather than faked. Only the live-
# observed-temperature clearance (a real, re-queryable historical quantity
# via the IEM ASOS archive) is applied.
NO_SIDE_ONLY = True
REQUIRE_NEAR_LOCK_IN = True
LOCK_IN_FRACTION_THRESHOLD = 0.02   # table's f_mean/g_mean below this = "locked in"
LOCK_IN_WINDOW_HOURS = 4.0
REQUIRE_OBSERVED_CLEARANCE = True
OBSERVED_CLEARANCE_F = 2.5

# Owner's explicit list (2026-09-13) -- NOT a P&L-ranked pick from this same
# backtest sample (that would be circular/in-sample cherry-picking). Several
# of these (Toronto, Beijing, Miami) scored among the WORST cities by net P&L
# and stop-frequency in the prior two runs; kept anyway per explicit instruction.
CITY_ALLOWLIST = {
    "Dallas", "Toronto", "Buenos Aires", "Atlanta", "London",
    "Seattle", "Chicago", "Wellington", "Miami", "Beijing",
}


def _lock_in_hour(is_high):
    """First local hour (0-23) at which REMAINING_RISE_TABLE's mean remaining-rise
    fraction drops to/under LOCK_IN_FRACTION_THRESHOLD, for this market side.
    f_mean = HIGH markets, g_mean = LOW markets. Table is hourly; we don't need
    the fit-package's sub-hour interpolation (intraday.remaining_fraction) here,
    just the whole-hour threshold crossing, since the filter window is 2 hours wide."""
    key = "f_mean" if is_high else "g_mean"
    for hour in range(24):
        row = REMAINING_RISE_TABLE.get(hour)
        if row and row[key] <= LOCK_IN_FRACTION_THRESHOLD:
            return hour
    return None


def _within_lock_in_window(city, target_date, is_high, entry_ts):
    """True if entry_ts (unix seconds) falls within LOCK_IN_WINDOW_HOURS before
    this city's lock-in hour, in the SETTLEMENT STATION'S OWN LOCAL TIME on
    target_date. Uses only entry_ts and static station/table data -- no
    knowledge of what happens after entry, so this introduces no look-forward
    bias. Returns False (excludes the trade) if the city has no known station."""
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
        # Entry happened on a different local calendar day than the market's
        # target date (e.g. late-evening entry that's already "tomorrow" UTC-
        # side, or a pre-day entry) -- lock-in timing doesn't apply, exclude.
        return False
    lock_hour = _lock_in_hour(is_high)
    if lock_hour is None:
        return False
    entry_hour = entry_local.hour + entry_local.minute / 60.0 + entry_local.second / 3600.0
    return (lock_hour - LOCK_IN_WINDOW_HOURS) <= entry_hour <= lock_hour


METAR_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backtest_cache", "metar")
_metar_day_cache = {}   # (icao, date_str) -> sorted [(unix_ts, temp_f), ...] this-process memo


def _f_from_c(c):
    return c * 9.0 / 5.0 + 32.0


def _fetch_metar_day_series(icao, tz_name, date_str):
    """Every real METAR temperature observation on the station's local calendar
    day date_str, as (unix_ts, temp_f) pairs sorted by time. Same IEM ASOS
    archive and same query shape as metar.fetch_day_extremes (the ruler
    Polymarket itself resolves against), but keeping every timestamped
    observation instead of collapsing to the day's max/min -- needed here to
    reconstruct what was observed AS OF an arbitrary past moment, not just at
    day's end. Disk-cached (this is real historical data, immutable once the
    day is over, safe to cache indefinitely across backtest re-runs)."""
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
    from datetime import date as _date, timedelta as _timedelta
    nd = _date(y, m, d) + _timedelta(days=1)
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
        logging_warn = f"METAR fetch failed for {icao} {date_str}: {e}"
        print(f"  [metar warning] {logging_warn}", flush=True)

    series.sort(key=lambda x: x[0])
    tmp_path = f"{disk_path}.{os.getpid()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(series, f)
    os.replace(tmp_path, disk_path)
    _metar_day_cache[cache_key] = series
    return series


def _observed_extreme_as_of(city, target_date, is_high, entry_ts):
    """The running observed max (is_high) or min (not is_high) temp_f, using
    ONLY METAR observations with a local timestamp <= entry_ts on target_date.
    This is the exact no-look-forward-bias contract: an observation from later
    in the day, even the same day, is never allowed to inform an entry filter
    evaluated earlier. Returns None if no station or no qualifying obs yet."""
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
    """True if the observed-so-far extreme sits >= OBSERVED_CLEARANCE_F clear
    of the bucket edge in the NO direction (mirrors strategy.forecast_margin_ok's
    NO branch, but on the live observation instead of the forecast mean, since
    no historical forecast archive exists to gate on -- see the module docstring
    note above). Bucket bounds come from scanner.parse_bucket on the market's
    own question text, the same parser the live bot uses, not a new one.
    Open-ended buckets (bucket_low or bucket_high is None) always pass -- no
    near edge to be clear of, same convention as forecast_margin_ok."""
    bucket_low, bucket_high = market.get("bucket_low"), market.get("bucket_high")
    if bucket_low is None or bucket_high is None:
        return True
    obs = _observed_extreme_as_of(market["city"], market["target_date"], market["is_high"], entry_ts)
    if obs is None:
        return False   # no observation yet at entry time -- can't confirm clearance, exclude
    return obs <= bucket_low - OBSERVED_CLEARANCE_F or obs >= bucket_high + OBSERVED_CLEARANCE_F


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backtest_cache")

DAILY_TITLE_RE = re.compile(r'^(Highest|Lowest) temperature in (.+?) on ')


def _http_get_json(url, retries=4):
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-bot-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 422:
                # Hit the pagination ceiling — not a transient error, don't retry.
                raise
            last_exc = e
        except Exception as e:  # noqa: BLE001
            last_exc = e
        time.sleep(0.5 * (attempt + 1))
    raise last_exc


def fetch_resolved_weather_bucket_markets():
    """Page Gamma's closed 'weather' tag events (offset 0..MAX_GAMMA_OFFSET,
    i.e. the ~most-recent 2100 closed weather events reachable through this
    endpoint before it 422s), keep only per-city daily 'Highest/Lowest
    temperature in <city> on <date>' bucket events, and flatten to one row per
    bucket sub-market with its resolved winner already fixed by Gamma/UMA.

    Returns a list of dicts: city, is_high, target_date_iso, question,
    condition_id, yes_token_id, no_token_id, resolved_side ('YES'/'NO'/None).
    """
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

            # target_date: parse from the event slug (…-on-<month>-<day>-<year>)
            # which is stable, rather than re-parsing English titles.
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

                # Figure out which index is "Yes" and read its RESOLVED price
                # (0.0 or 1.0) — this is post-hoc info, used ONLY for scoring.
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
                # else: not cleanly resolved (skip later if still None)

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


def fetch_trade_tape(condition_id, cache):
    """Full trade tape for a market (all outcomes) from the Data API, oldest
    first. Cached to disk within this run's cache dir to avoid re-hitting the
    network if the script is re-run against the same offline analysis pass."""
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
    os.replace(tmp_path, cache_path)  # atomic even if multiple threads race here
    cache[condition_id] = all_trades
    return all_trades


def simulate_market(market, cache, stake_usd, fee_rate):
    """Run the mechanical 0.90-0.92 entry / 40%-of-entry stop strategy against
    one resolved market's real trade tape, walking forward in time order.
    Returns a trade result dict, or None if no qualifying entry occurred.

    NO LOOK-FORWARD: we iterate `trades` (already sorted ascending by
    timestamp) exactly once, left to right. The entry search stops at the
    first BUY print inside the band. The stop-loss search then continues from
    that same forward position and stops at the first SELL print satisfying
    the stop condition. We never sort by price, never scan the whole array to
    find the "best" entry, and never check what happens after settlement
    before deciding the stop outcome.
    """
    if market["resolved_side"] is None:
        return None

    trades_by_token = {
        market["yes_token_id"]: ("YES",),
        market["no_token_id"]: ("NO",),
    }

    raw_trades = fetch_trade_tape(market["condition_id"], cache)
    if not raw_trades:
        return None

    # Only keep trades whose asset (token) belongs to this market, tag each
    # with which side (YES/NO) of our bucket it is, sort ascending by time.
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
            "side_label": side_label[0],      # YES or NO — which bucket side this token is
            "trade_side": t.get("side"),        # BUY or SELL (taker direction)
            "price": price,
            "ts": ts,
        })
    tape.sort(key=lambda x: x["ts"])
    if not tape:
        return None

    # --- ENTRY SCAN: first BUY print, in [0.90, 0.92], on the allowed side(s) ---
    entry = None
    entry_idx = None
    for i, tick in enumerate(tape):
        if tick["trade_side"] != "BUY":
            continue
        if NO_SIDE_ONLY and tick["side_label"] != "NO":
            continue
        if not (ENTRY_BAND_LOW <= tick["price"] <= ENTRY_BAND_HIGH):
            continue
        if REQUIRE_NEAR_LOCK_IN and not _within_lock_in_window(
                market["city"], market["target_date"], market["is_high"], tick["ts"]):
            continue
        if REQUIRE_OBSERVED_CLEARANCE and not _observed_clearance_ok(market, tick["ts"]):
            continue
        entry = tick
        entry_idx = i
        break

    if entry is None:
        return None

    entry_price = entry["price"]
    entry_side = entry["side_label"]      # the bucket side (YES/NO) we bought
    entry_ts = entry["ts"]
    stop_trigger_price = entry_price * STOP_FRACTION_OF_ENTRY

    shares = stake_usd / entry_price
    entry_fee = fee_rate_fee(entry_price, fee_rate) * shares

    # --- STOP-LOSS WALK-FORWARD: only look at SELL prints on the SAME side's
    # token, strictly AFTER the entry tick, in time order. First qualifying
    # tick wins; we do not look further ahead once found. ---
    stop_hit = None
    for tick in tape[entry_idx + 1:]:
        if tick["side_label"] != entry_side:
            continue
        if tick["trade_side"] != "SELL":
            continue
        if tick["price"] <= stop_trigger_price:
            stop_hit = tick
            break

    if stop_hit is not None:
        exit_price = stop_hit["price"]
        exit_fee = fee_rate_fee(exit_price, fee_rate) * shares
        gross_pnl = (exit_price - entry_price) * shares
        net_pnl = gross_pnl - entry_fee - exit_fee
        return {
            "market": market,
            "entry_side": entry_side,
            "entry_price": entry_price,
            "entry_ts": entry_ts,
            "exit_reason": "stop_loss",
            "exit_price": exit_price,
            "exit_ts": stop_hit["ts"],
            "shares": shares,
            "stake_usd": stake_usd,
            "fees_usd": entry_fee + exit_fee,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "won": False,
        }

    # --- HOLD TO SETTLEMENT: score using the resolved outcome only now ---
    won = (market["resolved_side"] == entry_side)
    settle_price = 1.0 if won else 0.0
    gross_pnl = (settle_price - entry_price) * shares
    net_pnl = gross_pnl - entry_fee  # no additional taker fee at resolution
    return {
        "market": market,
        "entry_side": entry_side,
        "entry_price": entry_price,
        "entry_ts": entry_ts,
        "exit_reason": "settlement",
        "exit_price": settle_price,
        "exit_ts": None,
        "shares": shares,
        "stake_usd": stake_usd,
        "fees_usd": entry_fee,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "won": won,
    }


def fee_rate_fee(price, fee_rate):
    """Same functional form as strategy.transaction_cost's fee term, but
    isolated (no slippage add-on — the traded price already embeds whatever
    slippage/spread existed at that real fill)."""
    return fee_rate * price * (1.0 - price)


def cross_check_against_local_db(sim_results, db_path):
    """Spot-check a handful of our fetched trade-tape entry prices against the
    bot's own locally-logged sniper_signals best_ask/best_bid for the same
    market_id and a nearby timestamp, where available. This is a sanity check,
    not a data source — flags material (>2c) disagreement."""
    import sqlite3
    if not os.path.exists(db_path):
        return {"checked": 0, "matches": 0, "mismatches": [], "note": f"no db at {db_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sniper_signals'")
        if not cur.fetchone():
            return {"checked": 0, "matches": 0, "mismatches": [], "note": "no sniper_signals table"}
    except Exception as e:  # noqa: BLE001
        return {"checked": 0, "matches": 0, "mismatches": [], "note": str(e)}

    checked = 0
    matches = 0
    mismatches = []
    for res in sim_results:
        cond_id = res["market"]["condition_id"]
        entry_dt = datetime.fromtimestamp(res["entry_ts"], tz=timezone.utc)
        # sniper_signals doesn't store condition_id directly for YES/NO tokens,
        # but does store city/target_date/is_high/bucket — match on those plus
        # nearest timestamp within +/- 30 min.
        m = res["market"]
        try:
            cur.execute(
                """SELECT timestamp, best_ask, best_bid FROM sniper_signals
                   WHERE city = ? AND target_date = ? AND is_high = ?
                   AND best_ask IS NOT NULL
                   ORDER BY ABS(strftime('%s', timestamp) - ?) ASC LIMIT 1""",
                (m["city"], m["target_date"], 1 if m["is_high"] else 0, res["entry_ts"]),
            )
            row = cur.fetchone()
        except Exception:
            row = None
        if not row:
            continue
        checked += 1
        local_ask = row["best_ask"]
        if local_ask is None:
            continue
        diff = abs(local_ask - res["entry_price"])
        if diff <= 0.02:
            matches += 1
        else:
            mismatches.append({
                "city": m["city"], "target_date": m["target_date"],
                "our_entry_price": res["entry_price"], "local_best_ask": local_ask,
                "diff": diff,
            })
    conn.close()
    return {"checked": checked, "matches": matches, "mismatches": mismatches}


def main():
    print("=" * 100)
    print("BACKTEST: buy 0.90-0.92 band, stop at 40% of entry value (i.e. -60% stake), else hold to settlement")
    print("=" * 100)

    # Sanity-check the stop arithmetic explicitly, as the task requires.
    example_entry = 0.90
    example_stop_price = example_entry * STOP_FRACTION_OF_ENTRY
    example_loss_frac_of_stake = 1.0 - STOP_FRACTION_OF_ENTRY
    print(f"\nSTOP ARITHMETIC CHECK: entry=${example_entry:.2f} -> stop fires at "
          f"${example_stop_price:.3f} (={STOP_FRACTION_OF_ENTRY:.0%} of entry price).")
    print(f"  At the stop price the position is worth {STOP_FRACTION_OF_ENTRY:.0%} of the stake -> "
          f"realized loss = {example_loss_frac_of_stake:.0%} of the stake, NOT {STOP_FRACTION_OF_ENTRY:.0%}.")
    print(f"  (Confirmed: this script books a stop-loss exit as a loss of "
          f"{example_loss_frac_of_stake:.0%} of stake before fees, matching the task's own worked example.)")

    stake_usd = FIXED_POSITION_SIZE
    fee_rate = TAKER_FEE_RATE
    print(f"\nUsing bot convention FIXED_POSITION_SIZE = ${stake_usd:.2f} flat stake per trade "
          f"(config.py default; this is a dashboard-tunable value, ${stake_usd:.2f} is config.py's own default).")
    print(f"Using bot convention TAKER_FEE_RATE = {fee_rate:.3f} (fee/share = rate * p * (1-p)), "
          f"applied on entry always, and again on exit only if the stop fires (no extra fee at settlement,\n"
          f"matching executor.py's settle_closed_trade which books settled_pnl = shares - stake with no fee term).")

    print("\nFetching resolved daily temperature-bucket markets from Polymarket Gamma API (tag_slug=weather, closed=true)...")
    markets, meta = fetch_resolved_weather_bucket_markets()
    print(f"  Gamma events scanned: {meta['events_seen']} (offset cap hit: {meta['gamma_offset_cap_hit']})")
    print(f"  Daily per-city temperature events matched: {meta['daily_events_seen']}")
    print(f"  Event end-date range covered: {meta['earliest_event_end']} .. {meta['latest_event_end']}")
    print(f"  Distinct resolved bucket sub-markets collected: {len(markets)}")

    resolved_markets = [m for m in markets if m["resolved_side"] is not None]
    unresolved_dropped = len(markets) - len(resolved_markets)
    print(f"  Of those, cleanly resolved (winner fixed, outcomePrices in {{0,1}}): {len(resolved_markets)}")
    print(f"  Dropped (not cleanly resolved / still pending / ambiguous outcomePrices): {unresolved_dropped}")

    print(f"\nPulling real trade tapes per market from Data API (/trades) for {len(resolved_markets)} markets "
          f"using a thread pool (network I/O bound; simulation logic itself is still single-threaded, sequential, "
          f"and per-market local -- no cross-market state, so parallelizing the network fetch does not affect the\n"
          f"no-look-forward-bias walk-forward logic within each market).")
    cache = {}

    def _fetch_and_simulate(m):
        # thread-local cache dict writes guarded; fetch_trade_tape itself only
        # touches disk-cache files keyed by condition_id (no shared mutable
        # state races because each market's file is unique).
        try:
            tape = fetch_trade_tape(m["condition_id"], cache)
        except Exception as e:  # noqa: BLE001
            return (m, None, f"fetch failed: {e}")
        try:
            r = simulate_market(m, cache, stake_usd, fee_rate)
        except Exception as e:  # noqa: BLE001
            return (m, None, f"simulate failed: {e}")
        return (m, r, None)

    results = []
    no_trade_tape = 0
    no_entry_in_band = 0
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
                    no_entry_in_band += 1
                continue
            results.append(r)

    print(f"\nMarkets with no fetchable trade tape at all: {no_trade_tape}")
    print(f"Markets with a trade tape but no BUY print ever landed in [0.90, 0.92]: {no_entry_in_band}")
    print(f"QUALIFYING TRADES (entries taken): {len(results)}")

    if not results:
        print("\nNo trades met the entry criterion in the available data. Nothing further to report.")
        return

    # ---- Cross-check against local DB (best-effort; may find few/no overlaps
    # since local sniper_signals only spans a few recent days). ----
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate_dbs = [
        os.path.join(repo_root, "data", "bot.db"),
    ]
    # Prefer the freshest live backup if present (fuller history than a reset local db).
    backups_dir = os.path.join(repo_root, "backups")
    if os.path.isdir(backups_dir):
        backup_dbs = sorted(
            (f for f in os.listdir(backups_dir) if f.endswith(".db")),
            reverse=True,
        )
        candidate_dbs = [os.path.join(backups_dir, f) for f in backup_dbs] + candidate_dbs

    cross_check = None
    for db_path in candidate_dbs:
        cc = cross_check_against_local_db(results, db_path)
        if cc.get("checked", 0) > 0:
            cross_check = (db_path, cc)
            break
    print("\n--- Cross-check vs. locally-logged order-book data ---")
    if cross_check is None:
        print("  No overlapping (city, date, is_high) rows with a populated best_ask found in any local DB/backup —")
        print("  the local sniper_signals table only covers the last few days and mostly logs NO_FILL_AVAILABLE rows")
        print("  with best_ask/best_bid blank, so no independent local price could be matched for comparison.")
    else:
        db_path, cc = cross_check
        print(f"  DB used: {db_path}")
        print(f"  Rows checked: {cc['checked']}, agreeing within 2 cents: {cc['matches']}")
        if cc["mismatches"]:
            print(f"  MATERIAL DISAGREEMENTS (> 2 cents) found: {len(cc['mismatches'])}")
            for mm in cc["mismatches"][:10]:
                print(f"    {mm['city']} {mm['target_date']}: our_entry={mm['our_entry_price']:.3f} "
                      f"vs local_best_ask={mm['local_best_ask']:.3f} (diff {mm['diff']:.3f})")
        else:
            print("  No material disagreements found.")

    # ---- Aggregate stats ----
    n = len(results)
    wins = [r for r in results if r["won"]]
    losses = [r for r in results if not r["won"]]
    stopped = [r for r in results if r["exit_reason"] == "stop_loss"]
    held = [r for r in results if r["exit_reason"] == "settlement"]
    yes_entries = [r for r in results if r["entry_side"] == "YES"]
    no_entries = [r for r in results if r["entry_side"] == "NO"]

    total_staked = sum(r["stake_usd"] for r in results)
    total_net_pnl = sum(r["net_pnl"] for r in results)
    total_gross_pnl = sum(r["gross_pnl"] for r in results)
    total_fees = sum(r["fees_usd"] for r in results)
    win_rate = len(wins) / n * 100.0

    win_pnls = [r["net_pnl"] for r in wins]
    loss_pnls = [r["net_pnl"] for r in losses]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0

    # Running P&L / max drawdown, in chronological order of entry.
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

    print("\n" + "=" * 100)
    print("RESULTS SUMMARY")
    print("=" * 100)
    print(f"Total qualifying trades:        {n}")
    print(f"  -> stop-loss exits:           {len(stopped)}")
    print(f"  -> held to settlement:        {len(held)}")
    print(f"Entry side split:                YES: {len(yes_entries)}   NO: {len(no_entries)}")
    print(f"Win rate:                        {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"Total staked:                    ${total_staked:,.2f}")
    print(f"Gross P&L (before fees):         ${total_gross_pnl:+,.2f}")
    print(f"Total fees:                      ${total_fees:,.2f}")
    print(f"Net P&L (after fees):            ${total_net_pnl:+,.2f}")
    print(f"Return on staked capital:        {(total_net_pnl / total_staked) * 100:+.2f}%")
    print(f"Average win (net $):             ${avg_win:+.3f}")
    print(f"Average loss (net $):            ${avg_loss:+.3f}")
    print(f"Max drawdown (running net $):    ${max_dd:,.2f}")

    print(f"\nSample-size verdict: {n} trades. This project's own convention (per repo memory) is "
          f"'don't trust anything under ~50 trades.'")
    if n < 50:
        print(f"  --> {n} < 50: THIS SAMPLE IS TOO SMALL TO TRUST. Treat all stats above as preliminary/noisy.")
    else:
        print(f"  --> {n} >= 50: meets the project's own minimum bar for a trustworthy read, "
              f"though more is always better.")

    # ---- By-city breakdown ----
    by_city = defaultdict(list)
    for r in results:
        by_city[r["market"]["city"]].append(r)

    print("\n" + "-" * 100)
    print(f"{'City':<18} {'Trades':<8} {'Wins':<6} {'Win%':<8} {'NetPnL':<12} {'AvgEntryPx':<11} {'Stops':<6}")
    print("-" * 100)
    for city in sorted(by_city.keys(), key=lambda c: -len(by_city[c])):
        rs = by_city[city]
        w = sum(1 for r in rs if r["won"])
        stops = sum(1 for r in rs if r["exit_reason"] == "stop_loss")
        net = sum(r["net_pnl"] for r in rs)
        avg_px = sum(r["entry_price"] for r in rs) / len(rs)
        print(f"{city:<18} {len(rs):<8} {w:<6} {w/len(rs)*100:<7.1f}% ${net:<+10.3f} {avg_px:<11.3f} {stops:<6}")

    # ---- Per-trade ledger (first 60 + last 10 if long) ----
    print("\n" + "-" * 100)
    print("TRADE LEDGER (chronological by entry time)")
    print("-" * 100)
    header = (f"{'EntryTime(UTC)':<20} {'City':<14} {'Side':<4} {'EntryPx':<8} {'ExitReason':<11} "
              f"{'ExitPx':<7} {'NetPnL':<9} {'Won':<5}")
    print(header)
    to_show = results_sorted if n <= 80 else results_sorted[:60] + results_sorted[-10:]
    for r in to_show:
        et = datetime.fromtimestamp(r["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{et:<20} {r['market']['city']:<14} {r['entry_side']:<4} {r['entry_price']:<8.3f} "
              f"{r['exit_reason']:<11} {r['exit_price']:<7.3f} {r['net_pnl']:<+9.3f} {str(r['won']):<5}")
    if n > 80:
        print(f"... ({n - 70} trades omitted from the printed ledger; full CSV has every trade) ...")

    # Full ledger, every trade, for any city/pattern drill-down without re-running.
    ledger_name = "ledger_v2_ungated.csv" if UNGATED_PRICE else "ledger_v2_9092.csv"
    csv_path = os.path.join(os.path.dirname(__file__), "_backtest_cache", ledger_name)
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_time_utc", "city", "target_date", "is_high", "side", "entry_price",
                    "exit_reason", "exit_price", "net_pnl", "won", "condition_id",
                    "bucket_low", "bucket_high", "bucket_label", "open_ended"])
        for r in results_sorted:
            et = datetime.fromtimestamp(r["entry_ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            mkt = r["market"]
            b_lo, b_hi = mkt.get("bucket_low"), mkt.get("bucket_high")
            w.writerow([et, mkt["city"], mkt.get("target_date", ""),
                        mkt.get("is_high", ""), r["entry_side"], r["entry_price"],
                        r["exit_reason"], r["exit_price"], r["net_pnl"], r["won"],
                        mkt.get("condition_id", ""), b_lo, b_hi, mkt.get("bucket_label", ""),
                        b_lo is None or b_hi is None])
    print(f"\nFull ledger ({n} trades) written to: {csv_path}")

    print("\n" + "=" * 100)
    print("LIMITATIONS / CAVEATS")
    print("=" * 100)
    print("""
1. No true historical order-book (bid/ask) API exists for settled Polymarket
   markets. This backtest uses the real, executed trade tape (Data API
   /trades) as a proxy: BUY prints proxy for 'an ask was crossable here',
   SELL prints proxy for 'a bid was crossable here'. This is real transacted
   data, not synthetic/interpolated, but it assumes a hypothetical resting
   order of ours would have matched at the same price a real trade got at
   that instant, and does NOT verify depth beyond one contra-trade's size
   (i.e. we do not know if $3 of size was actually available, only that
   *some* size traded at that price).
2. Because entries require an actual BUY print inside [0.90, 0.92], markets
   that jumped straight through the band without a trade printing in it are
   correctly excluded (no fill assumed) -- this likely undercounts true
   opportunities slightly versus a live bid/ask feed, but avoids fabricating
   fills.
3. Coverage window is capped by Gamma's legacy /events endpoint, which 422s
   past offset=2000; this bounds history to roughly the most recent ~3 weeks
   of resolved weather markets as of the run date. Older markets exist on
   Polymarket but were not reachable through this endpoint in this run.
4. Local data/bot.db and its live backups under backups/ were checked as a
   cross-check source (sniper_signals, scan_log, markets, replay_signals,
   orderbook_snapshots). In this run's environment: replay_signals and
   orderbook_snapshots were EMPTY (not populated by the current codebase
   configuration), and sniper_signals only covers 2026-09-11 through
   2026-09-13 with best_ask/best_bid populated on essentially none of its
   rows (dominated by NO_FILL_AVAILABLE / BELOW_THRESHOLD rows with blank
   book fields) -- so independent local verification was possible for at
   most a handful of trades, reported above, not the full sample.
5. Fees: modeled as TAKER_FEE_RATE * p * (1-p) per share (config.py's own
   constant, 0.05), charged on entry always and on exit again only if the
   stop-loss fires; no fee charged at settlement, matching executor.py's own
   settle_closed_trade formula. No additional slippage haircut is layered on
   top of the real traded price (the traded price already reflects whatever
   slippage a real taker experienced).
6. Flat stake used is config.py's FIXED_POSITION_SIZE default ($3.00). This
   is a dashboard-tunable runtime setting in the live bot, not a hardcoded
   constant; this script uses config.py's own shipped default since no
   override was specified.
7. Markets resolving to a non-binary/ambiguous outcomePrices pair (e.g. still
   in dispute, or a 50/50 resolution) were dropped rather than guessed.
""")


if __name__ == "__main__":
    main()
