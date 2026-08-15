import requests
import re
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from weather import (get_station_coords, STATIONS, is_tradeable_window,
                     settlement_window)
from metar import STATION_ICAO
from lattice import star_tag
from db import execute_query, fetch_query, flag_impossible_bucket
from config import (
    MIN_VOLUME, MAX_HOURS_TO_RESOLUTION, GAMMA_EVENTS_URL, GAMMA_API_URL, CLOB_BASE_URL,
    DATA_API_URL,
    DEBUG_MARKET_SCAN, DEBUG_MARKET_SCAN_VERBOSE, DEBUG_WEATHER_DISCOVERY,
    MARKET_DISCOVERY_LIMIT, MARKET_DISCOVERY_MAX_PAGES,
    MARKET_DISCOVERY_STOP_AFTER_WEATHER, MAX_CLOB_CANDIDATES,
    MAX_BUCKETS_PER_CITY_DATE, TRADE_HIGH_MARKETS, TRADE_LOW_MARKETS,
)
from utils import (get_session, parse_utc_datetime, safe_get, get_cached_price,
                   set_cached_price, get_cached_depth, get_cached_top_size)

@dataclass
class MarketOpportunity:
    market_id: str
    token_id_yes: str
    token_id_no: str
    city: str
    date: str
    bucket_low: float
    bucket_high: float
    yes_price: float
    no_price: float
    volume: float
    hours_to_resolution: float
    question: str
    is_high: bool


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# Bump this any time parse_bucket's bucket-math logic changes (not just cosmetic
# edits). Written to signals.parser_version and markets.parser_version so a bug
# like the 2026-06 Celsius zero-width bucket issue (exact "33°C" parsed as a
# zero-width (91.4, 91.4) bucket instead of the correct rounding-tolerant
# (91.0, 91.8)) is instantly detectable from the DB, instead of requiring a
# multi-hour forensic timestamp-correlation audit to even notice it happened.
PARSER_VERSION = 3  # v3: strict vs inclusive threshold phrasing split (±1 whole degree)


def parse_market_direction(question: str) -> tuple[bool, bool]:
    """Return (is_low, is_high) for a market question using whole-word matching."""
    q_lower = question.lower()
    low_keywords = ("low", "min", "lowest", "minimum", "cold", "coolest")
    high_keywords = ("high", "max", "highest", "maximum", "warm", "hottest")

    def _word_pos(w):
        m = re.search(r'\b' + re.escape(w) + r'\b', q_lower)
        return m.start() if m else None

    low_hits = [p for p in (_word_pos(w) for w in low_keywords) if p is not None]
    high_hits = [p for p in (_word_pos(w) for w in high_keywords) if p is not None]
    is_low, is_high = bool(low_hits), bool(high_hits)
    if is_high and is_low:
        is_high = min(high_hits) <= min(low_hits)
        is_low = not is_high
    elif not is_high and not is_low:
        is_high = True
        is_low = False
    return is_low, is_high


def parse_bucket(question: str):
    """Parse a temperature bucket from a market question.

    Handles both °F (US/UK markets) and °C (international markets).
    All returned values are in °F to match Open-Meteo's fahrenheit output.
    """
    q_original = question.lower()

    # Detect unit: prefer explicit marker; default to °F if absent
    is_celsius = bool(re.search(r'(?:°\s*c\b|\b\d+\s*c\b|\bcelsius\b)', q_original))

    # Only match numbers that are directly adjacent to a degree symbol (°F or °C)
    # This avoids matching date numbers like "June 5" or "May 20".
    degree_pattern = r'(-?\d+(?:\.\d+)?)\s*°\s*[cfCF]'
    degree_matches = re.findall(degree_pattern, question, re.IGNORECASE)

    # Fallback: numbers after explicit temperature keywords (no degree symbol present)
    keyword_pattern = r'(?:above|below|exceed|at\s+least|or\s+more|between|under|over)\s+(-?\d+(?:\.\d+)?)'
    keyword_matches = re.findall(keyword_pattern, q_original)

    # Use degree-symbol matches as primary; keyword matches as fallback
    temp_matches = degree_matches if degree_matches else keyword_matches

    # Range pattern: "58-59°F" or "between 12-14°C"
    range_pattern = r'(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*°\s*[cfCF]'
    range_match = re.search(range_pattern, question, re.IGNORECASE)
    if not range_match:
        # Also handle "70 to 75°F"
        range_pattern2 = r'(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)\s*°\s*[cfCF]'
        range_match = re.search(range_pattern2, question, re.IGNORECASE)
    if not range_match:
        # Also handle "between 12°C and 14°C" — without this, such questions fell
        # through to the exact-bucket branch and silently parsed as a bucket at 12.
        range_pattern3 = (r'between\s+(-?\d+(?:\.\d+)?)\s*(?:°\s*[cfCF])?\s+and\s+'
                          r'(-?\d+(?:\.\d+)?)\s*°\s*[cfCF]')
        range_match = re.search(range_pattern3, question, re.IGNORECASE)

    if is_celsius:
        # For Celsius weather markets:
        # Since the resolution source resolves using whole degrees Celsius, the
        # range is extended by +/- 0.5°C in Celsius. To correct for the fact that
        # get_bucket_probability() adds/subtracts 0.5 in Fahrenheit, we convert the
        # bounds with a correction factor of +/- 0.5 in the Fahrenheit input.
        # Inclusive vs strict phrasing resolve one whole degree apart:
        # "X or below" pays YES when the rounded reading ≤ X (raw < X+0.5), while
        # strict "below X" needs rounded < X (raw < X-0.5) — and symmetrically on the
        # high side. Check the inclusive phrases first ("or below" contains "below").
        if ("or below" in q_original or "or under" in q_original
                or "or lower" in q_original or "or less" in q_original):
            if temp_matches:
                val = float(temp_matches[-1])
                return (None, _c_to_f(val + 0.5) - 0.5)
        elif "below" in q_original or "under" in q_original:
            if temp_matches:
                val = float(temp_matches[-1])
                return (None, _c_to_f(val - 0.5) - 0.5)
        elif ("or more" in q_original or "at least" in q_original
              or "or higher" in q_original or "or above" in q_original):
            if temp_matches:
                val = float(temp_matches[0])
                return (_c_to_f(val - 0.5) + 0.5, None)
        elif "above" in q_original or "exceed" in q_original:
            if temp_matches:
                val = float(temp_matches[0])
                return (_c_to_f(val + 0.5) + 0.5, None)
        elif range_match:
            low, high = float(range_match.group(1)), float(range_match.group(2))
            if low < high:
                return (_c_to_f(low - 0.5) + 0.5, _c_to_f(high + 0.5) - 0.5)
        elif temp_matches:
            val = float(temp_matches[0])
            if -100 <= val <= 200:
                return (_c_to_f(val - 0.5) + 0.5, _c_to_f(val + 0.5) - 0.5)
    else:
        # Standard Fahrenheit logic
        # Same inclusive/strict split as the Celsius branch, in whole °F: the
        # downstream ±0.5°F pad makes a bound at X inclusive of X, so strict
        # phrasing must move the bound one whole degree.
        if ("or below" in q_original or "or under" in q_original
                or "or lower" in q_original or "or less" in q_original):
            if temp_matches:
                return (None, float(temp_matches[-1]))
        elif "below" in q_original or "under" in q_original:
            if temp_matches:
                return (None, float(temp_matches[-1]) - 1.0)
        elif ("or more" in q_original or "at least" in q_original
              or "or higher" in q_original or "or above" in q_original):
            if temp_matches:
                return (float(temp_matches[0]), None)
        elif "above" in q_original or "exceed" in q_original:
            if temp_matches:
                return (float(temp_matches[0]) + 1.0, None)
        elif range_match:
            low, high = float(range_match.group(1)), float(range_match.group(2))
            if low < high:
                return (low, high)
        elif temp_matches:
            val = float(temp_matches[0])
            if -100 <= val <= 200:
                return (val, val)

    return (None, None)


# Fixed fixtures for parse_bucket, checked at process startup (verify_parser_fixtures)
# and pinned in tests/test_scanner.py::TestParseBucketExactCelsius. Keep both in
# sync — this is intentionally a small, hand-picked subset (the real historical
# questions that triggered the 2026-06 bug), not a full copy of the test suite.
_PARSER_FIXTURES = [
    ("Will the highest temperature in Hong Kong be 33°C on July 1?", (91.0, 91.8)),
    ("Will the highest temperature in Wellington be 12°C on July 1?", (53.2, 54.0)),
    ("Will the highest temperature in Ankara be 32°C on July 1?", (89.2, 90.0)),
]


def verify_parser_fixtures():
    """Fail fast at startup if parse_bucket's output drifts from pinned known-good
    values. Catches a regression like the 2026-06 Celsius zero-width bucket bug
    before the scanner ever writes a bad bucket to the DB, rather than relying
    solely on the test suite (which may not run before every deploy)."""
    for question, expected in _PARSER_FIXTURES:
        lb, ub = parse_bucket(question)
        exp_lb, exp_ub = expected
        if lb is None or ub is None or abs(lb - exp_lb) > 0.01 or abs(ub - exp_ub) > 0.01:
            raise RuntimeError(
                f"parse_bucket() regression detected at startup: "
                f"question={question!r} expected={expected} got=({lb}, {ub}). "
                f"Refusing to start — this is exactly the failure mode that caused "
                f"the 2026-06 Celsius bucket bug."
            )


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_target_date(description: str, end_date, question: str = ""):
    """Return the temperature measurement date (YYYY-MM-DD) a market resolves on.

    Polymarket resolves on a named calendar day at a specific station ("...recorded
    at the X Station in degrees Celsius on 1 Jul '26"). That "on <DATE>" phrase is the
    unambiguous source of truth. Deriving the date from the endDate timestamp instead
    is fragile: the endDate convention has drifted (older markets close 00:00Z, newer
    ones 12:00Z on the named day), and for far-east/-west stations the UTC calendar
    date of a T00:00Z close can fall on the wrong local day — which mis-dated the
    forecast for cities like Wellington (UTC+12) and flipped real trades.

    Prefers matching "on <DATE>" in description or question; falls back to the UTC date of end_date.
    """
    for text in (description, question):
        if not text:
            continue
        m = re.search(r"\bon\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:\s+'?(\d{2,4}))?\b", text, re.IGNORECASE)
        if not m:
            m = re.search(r"\bon\s+(\d{1,2})\s+([A-Za-z]{3,9})(?:\s+'?(\d{2,4}))?\b", text, re.IGNORECASE)
        if m:
            groups = m.groups()
            if groups[0].isdigit():
                day = int(groups[0])
                mon_str = groups[1]
            else:
                mon_str = groups[0]
                day = int(groups[1])
            mon = _MONTHS.get(mon_str[:3].lower())
            yr_str = groups[2]
            if yr_str:
                yr = int(yr_str)
                if yr < 100:
                    yr += 2000
            elif end_date:
                yr = end_date.year
            else:
                yr = datetime.now(timezone.utc).year
            if mon and 1 <= day <= 31:
                try:
                    return datetime(yr, mon, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
                except ValueError:
                    pass
    # Fallback: UTC calendar date of the close timestamp.
    if end_date is not None:
        return end_date.strftime("%Y-%m-%d")
    return None


def get_target_day_end_utc(target_date_str: str, city_name: str = None) -> datetime:
    """Return the UTC datetime representing the end of the target day for a city.

    For a given target_date (e.g. '2026-08-10'), the civil measurement day ends at
    23:59:59 local time in the station's IANA timezone. Converted to UTC, this gives
    the exact end of the target observation day.
    """
    try:
        yr, mon, day = map(int, target_date_str.split("-"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=24)

    tz_name = None
    if city_name and city_name in STATION_ICAO:
        tz_name = STATION_ICAO[city_name][1]

    if tz_name:
        try:
            local_end = datetime(yr, mon, day, 23, 59, 59, tzinfo=ZoneInfo(tz_name))
            return local_end.astimezone(timezone.utc)
        except Exception:
            pass

    base_utc = datetime(yr, mon, day, 23, 59, 59, tzinfo=timezone.utc)
    return base_utc + timedelta(hours=6)


def get_or_store_bucket(market_id: str, question: str, city: str, target_date: str):
    """Return (bucket_low, bucket_high) for market_id, immutably.

    First call for a market_id parses the question and persists the result to
    the `markets` table. Every subsequent call — even after parse_bucket's logic
    changes — returns the ORIGINALLY stored bucket, not a fresh re-parse. This is
    what prevents a parser fix (or future bug) from silently changing the bucket
    definition of a market that's still being actively scanned/traded, which is
    exactly the failure mode that let the 2026-06 Celsius zero-width bucket bug
    change bucket bounds mid-trade for ~26% of markets with no audit trail.
    """
    existing = fetch_query(
        "SELECT bucket_low, bucket_high FROM markets WHERE market_id=?", (market_id,)
    )
    if existing:
        return existing[0]["bucket_low"], existing[0]["bucket_high"]

    lb, ub = parse_bucket(question)
    execute_query(
        "INSERT OR IGNORE INTO markets "
        "(market_id, question, city, target_date, bucket_low, bucket_high, parser_version, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (market_id, question, city, target_date, lb, ub, PARSER_VERSION,
         datetime.now(timezone.utc).isoformat())
    )
    # Re-read rather than trust the just-computed values, in case a concurrent
    # scan cycle won the INSERT race first — INSERT OR IGNORE silently no-ops
    # in that case, so the values we hold locally could be stale for THIS market_id
    # if two scans discovered it in the same instant.
    row = fetch_query(
        "SELECT bucket_low, bucket_high FROM markets WHERE market_id=?", (market_id,)
    )
    if row:
        return row[0]["bucket_low"], row[0]["bucket_high"]
    return lb, ub


def _best_ask_bid_from_book(data):
    """Extract (best_ask, best_bid) from a CLOB /book response WITHOUT trusting
    array order.

    The Polymarket CLOB /book endpoint does not return price levels best-first.
    Verified live 2026-07-04 across 6 markets: `asks` come back sorted DESCENDING
    (worst/highest price at index 0) and `bids` ASCENDING (worst/lowest at index
    0). Reading asks[0]/bids[0] as "best" therefore inverts the book — it returns
    the WORST prices, producing a fake ~98%% spread (which the entry spread gate
    then rejects) and a mid that only coincidentally lands near the true mid by
    symmetry. The best executable ask is the MINIMUM ask; the best executable bid
    is the MAXIMUM bid. Never rely on the returned index.

    Returns (0.0, 0.0) for a missing side. Malformed level entries are skipped.
    """
    def _prices(levels):
        out = []
        for lvl in levels or []:
            try:
                out.append(float(lvl["price"]))
            except (TypeError, ValueError, KeyError):
                continue
        return out

    ask_prices = _prices(data.get("asks", []))
    bid_prices = _prices(data.get("bids", []))
    best_ask = min(ask_prices) if ask_prices else 0.0
    best_bid = max(bid_prices) if bid_prices else 0.0
    return best_ask, best_bid


def _book_depth_usd(data):
    """Total resting $ depth on each side of a CLOB /book response — how many
    dollars could actually be bought (asks) or sold into (bids) right now, not
    just the best price. Recorded at entry time so post-hoc "could a $50/$100
    position have filled without walking the book" questions can be answered
    from what was really there, instead of guessed from the current (unrelated)
    live book of a market that's since moved on or resolved."""
    def _depth(levels):
        total = 0.0
        for lvl in levels or []:
            try:
                total += float(lvl["price"]) * float(lvl["size"])
            except (TypeError, ValueError, KeyError):
                continue
        return total

    return _depth(data.get("asks", [])), _depth(data.get("bids", []))


def _usable_ask_depth_usd(data, max_price):
    """Resting $ depth on the ask side AT OR BELOW `max_price`.

    Total book depth is the wrong number for an entry decision. Depth resting at
    0.95 is not depth you can use when your cap is 0.80 — it is exactly the
    depth a market order walks into after exhausting everything cheaper, which
    is how a $6 order against a $26.49 book filled at 0.9818 on a 0.64 quote.

    `max_price=None` means no cap and returns the whole side, matching
    _book_depth_usd."""
    total = 0.0
    for lvl in data.get("asks", []) or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (TypeError, ValueError, KeyError):
            continue
        if max_price is None or p <= max_price + 1e-9:
            total += p * s
    return total


def _walk_asks(data, usd_amount, max_price=None):
    """What buying `usd_amount` would ACTUALLY cost, by consuming the real book.

    Returns (vwap, filled_usd, exhausted_book). Levels are taken cheapest-first,
    which is how a taker fills. `vwap` is None when nothing is fillable.

    This is the quantity SLIPPAGE_FRACTION was always meant to approximate and
    never did: `spread_fraction * price` describes CROSSING THE SPREAD, a
    one-level move, while a size larger than the top level WALKS the book and
    pays every level it eats. On the Austin book those differed by 4x — modelled
    0.085, actual 0.34."""
    levels = []
    for lvl in data.get("asks", []) or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (TypeError, ValueError, KeyError):
            continue
        if p > 0 and s > 0 and (max_price is None or p <= max_price + 1e-9):
            levels.append((p, s))
    levels.sort()

    spent = shares = 0.0
    for price, size in levels:
        if spent >= usd_amount - 1e-9:
            break
        can_spend = min(price * size, usd_amount - spent)
        spent += can_spend
        shares += can_spend / price
    if shares <= 0:
        return None, 0.0, True
    return spent / shares, spent, spent < usd_amount - 1e-9


def _top_of_book_size(data):
    """Shares resting AT the best price on each side: (ask_top_size, bid_top_size).

    Not the same question as _book_depth_usd. Total depth says whether a size
    could fill at all; top-of-book says whether the quoted best price is real or
    is one small order defining the entire spread. On a book where
    MAX_ENTRY_SPREAD_FRACTION allows up to 15%, a bid quoted by a single 2-share
    order and a bid backed by 200 shares mean very different things about a
    drawdown measured against that bid — which is exactly the Chongqing -56.2%
    question the position trail exists to settle.

    Sizes are SUMMED across levels at the best price: the CLOB returns levels
    unaggregated, so two orders at the same price arrive as two entries. Uses
    the same min-ask/max-bid rule as _best_ask_bid_from_book — never index 0,
    which is the worst price, not the best.
    """
    def _top(levels, best_fn):
        parsed = []
        for lvl in levels or []:
            try:
                parsed.append((float(lvl["price"]), float(lvl["size"])))
            except (TypeError, ValueError, KeyError):
                continue
        if not parsed:
            return 0.0
        best = best_fn(p for p, _ in parsed)
        return sum(s for p, s in parsed if p == best)

    return _top(data.get("asks", []), min), _top(data.get("bids", []), max)


def get_realtime_price_status(token_id):
    """Fetch best ask/bid for a token. Returns (ask, bid, reachable)."""
    cached = get_cached_price(token_id)
    if cached is not None:
        return cached[0], cached[1], cached[2]

    try:
        resp = safe_get(f"{CLOB_BASE_URL}/book?token_id={token_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            best_ask, best_bid = _best_ask_bid_from_book(data)
            ask_depth, bid_depth = _book_depth_usd(data)
            ask_top, bid_top = _top_of_book_size(data)
            set_cached_price(token_id, best_ask, best_bid, True, ask_depth, bid_depth,
                             ask_top, bid_top)
            return best_ask, best_bid, True
        logging.warning(f"Orderbook for {token_id}: HTTP {resp.status_code}")
    except Exception as e:
        logging.error(f"Error fetching orderbook for {token_id}: {e}")

    set_cached_price(token_id, 0.0, 0.0, False)
    return 0.0, 0.0, False


# Raw books, cached briefly. The price cache stores only the AGGREGATE depth
# numbers, which cannot answer "how much is resting at or below 0.80" or "what
# would $6 actually fill at" — both need the levels themselves.
_BOOK_CACHE: dict = {}
_BOOK_TTL_SECONDS = 30


def get_book(token_id, force=False):
    """The raw CLOB book for `token_id`, or None if it cannot be read.

    None is load-bearing and must not be collapsed to an empty book: "the book
    is unreadable" and "the book is empty" are different facts, and the entry
    gate refuses on the first rather than treating unknown depth as zero or as
    infinite."""
    import time as _t
    hit = _BOOK_CACHE.get(token_id)
    if hit and not force and (_t.monotonic() - hit[0]) < _BOOK_TTL_SECONDS:
        return hit[1]
    try:
        resp = safe_get(f"{CLOB_BASE_URL}/book?token_id={token_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _BOOK_CACHE[token_id] = (_t.monotonic(), data)
            return data
        logging.warning(f"Book fetch for {token_id}: HTTP {resp.status_code}")
    except Exception as e:
        logging.error(f"Book fetch failed for {token_id}: {e}")
    return None


def usable_ask_depth_usd(token_id, max_price):
    """$ resting on the ask side at or below `max_price`, or None if unreadable.

    The number the entry gate needs. See _usable_ask_depth_usd."""
    data = get_book(token_id)
    if data is None:
        return None
    return _usable_ask_depth_usd(data, max_price)


def estimate_fill(token_id, usd_amount, max_price=None, force=False):
    """What `usd_amount` would really fill at, by walking the live book.

    Returns {"vwap", "filled_usd", "exhausted", "usable_depth_usd", "best_ask"}
    or None when the book cannot be read. `force=True` bypasses the book cache
    — the submit path needs the book as it is NOW, not as it was at scan start."""
    data = get_book(token_id, force=force)
    if data is None:
        return None
    best_ask, _ = _best_ask_bid_from_book(data)
    vwap, filled, exhausted = _walk_asks(data, usd_amount, max_price)
    return {"vwap": vwap, "filled_usd": filled, "exhausted": exhausted,
            "usable_depth_usd": _usable_ask_depth_usd(data, max_price),
            "best_ask": best_ask}


def _walk_bids(data, shares):
    """What SELLING `shares` would actually realize, by consuming the real book.

    Returns (vwap, filled_shares, exhausted_book). Levels are taken dearest-first,
    which is how a taker sells. `vwap` is None when nothing is fillable.

    The exit-side mirror of _walk_asks, and it exists because total bid depth in
    dollars answers the wrong question. Qingdao 2026-08-11 showed $109 of bid
    depth against a $12.73 sale — a 9x cushion by that measure, and a 3x depth
    test would have waved it straight through. But the top bid held only 5.76 of
    the 15.15 shares and the rest of the book sat far below, so the sale realized
    $0.2578 against a $0.28 top bid: 2.2c THROUGH the quote the stop had measured
    its own trigger against. Slippage off the top bid is the quantity that
    actually describes that, and it needs the walk to compute.
    """
    levels = []
    for lvl in data.get("bids", []) or []:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (TypeError, ValueError, KeyError):
            continue
        if p > 0 and s > 0:
            levels.append((p, s))
    levels.sort(reverse=True)   # a seller takes the HIGHEST bids first

    got = filled = 0.0
    for price, size in levels:
        if filled >= shares - 1e-9:
            break
        take = min(size, shares - filled)
        filled += take
        got += take * price
    if filled <= 0:
        return None, 0.0, True
    return got / filled, filled, filled < shares - 1e-9


def estimate_sale(token_id, shares, force=False):
    """What selling `shares` would really realize, by walking the live bid side.

    Returns {"vwap", "filled_shares", "exhausted", "best_bid", "slippage_frac"}
    or None when the book cannot be read. `slippage_frac` is how far below the
    top bid the average fill lands — the number the exit guard tests, because it
    is the one the Qingdao print blew through. force=True bypasses the 30s book
    cache; an exit about to move real money must price the book as it is NOW."""
    data = get_book(token_id, force=force)
    if data is None:
        return None
    _, best_bid = _best_ask_bid_from_book(data)
    vwap, filled, exhausted = _walk_bids(data, shares)
    slippage = None
    if vwap is not None and best_bid and best_bid > 0:
        slippage = (best_bid - vwap) / best_bid
    return {"vwap": vwap, "filled_shares": filled, "exhausted": exhausted,
            "best_bid": best_bid, "slippage_frac": slippage}


def get_orderbook_depth_usd(token_id):
    """Total $ resting on each side of the book for `token_id`: (ask_depth, bid_depth).
    Piggybacks on the same 30s price cache get_realtime_price_status populates — call
    that first (or let this trigger the fetch) so depth isn't a second network round
    trip. Returns (None, None) if depth wasn't captured (e.g. book unreachable)."""
    cached = get_cached_depth(token_id)
    if cached is not None:
        return cached
    # Not cached yet (or price-only path was used) — force a real fetch so depth
    # gets populated, then re-check the cache.
    get_realtime_price_status(token_id)
    cached = get_cached_depth(token_id)
    return cached if cached is not None else (None, None)


def get_orderbook_top_size(token_id):
    """Shares at the best price on each side: (ask_top_size, bid_top_size).
    Piggybacks on the same 30s cache as price and depth, so on the monitor path
    — which has already read the book this cycle — this costs nothing. Returns
    (None, None) if the book wasn't readable."""
    cached = get_cached_top_size(token_id)
    if cached is not None:
        return cached
    get_realtime_price_status(token_id)
    cached = get_cached_top_size(token_id)
    return cached if cached is not None else (None, None)


def get_realtime_price(token_id):
    ask, bid, _ = get_realtime_price_status(token_id)
    return ask, bid


def get_mid_price(token_id):
    ask, bid = get_realtime_price(token_id)
    if ask > 0 and bid > 0:
        return (ask + bid) / 2.0
    return ask or bid


def get_gamma_mid_price(market_id: str, side: str):
    """Fallback mid price for `side` ('YES' or 'NO') via Gamma's outcomePrices,
    used when the CLOB order book can't be read (empty/thin book, rate limit,
    network hiccup). Gamma's outcomePrices reflects the market's last-settled
    price even when the live order book has nothing resting, so this catches
    exactly the case that silently disabled the edge-decay exit check —
    a position sitting at a real, extreme price with a temporarily unreadable
    order book. Returns None if the market can't be found or fields are missing."""
    try:
        resp = safe_get(f"{GAMMA_API_URL}?condition_ids={market_id}", timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        m = data[0]
        outcome_prices = m.get("outcomePrices")
        try:
            op = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        except (TypeError, ValueError):
            op = None
        if not op or len(op) < 2:
            return None
        yes_price = float(op[0])
        return yes_price if side == "YES" else (1.0 - yes_price)
    except Exception as e:
        logging.error(f"Gamma fallback price fetch failed for {market_id}: {e}")
        return None


def get_wallet_token_sizes(user_address: str) -> dict | None:
    """Actual conditional-token balances held by the wallet, via the Data-API
    positions endpoint. Returns {token_id_str: size}. A token absent from the
    dict means the wallet holds none of it (sizeThreshold=0 so even dust rows
    are returned when the API has them).

    Returns None when the API can't be read — callers MUST treat None as
    "unknown", never as "empty": booking a close because a balance endpoint
    was down would fabricate an exit."""
    try:
        resp = safe_get(
            f"{DATA_API_URL}/positions",
            params={"user": user_address, "sizeThreshold": 0},
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning(f"Wallet positions for {user_address}: HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, list):
            return None
        sizes: dict = {}
        for p in data:
            asset = str(p.get("asset") or "")
            if not asset:
                continue
            try:
                sizes[asset] = sizes.get(asset, 0.0) + float(p.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
        return sizes
    except Exception as e:
        logging.error(f"Wallet positions fetch failed for {user_address}: {e}")
        return None


def get_wallet_sells(user_address: str, market_id: str, token_id: str,
                     since_epoch: float) -> list | None:
    """SELL fills by this wallet for `token_id` at/after `since_epoch` (unix
    seconds), via the Data-API trades endpoint filtered to `market_id`
    (conditionId). Returns [(price, size), ...] newest-first — includes both
    manual website sales and the bot's own CLOB sells; the caller decides what
    a fill means. A 60s grace window absorbs clock skew between our entry
    timestamp and the API's. Returns None when the API can't be read (unknown,
    not "no sales")."""
    try:
        resp = safe_get(
            f"{DATA_API_URL}/trades",
            params={"user": user_address, "market": market_id, "limit": 100},
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning(f"Wallet trades for {market_id}: HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, list):
            return None
        fills = []
        for t in data:
            if t.get("side") != "SELL" or str(t.get("asset") or "") != str(token_id):
                continue
            try:
                ts = float(t.get("timestamp") or 0)
                price, size = float(t["price"]), float(t["size"])
            except (TypeError, ValueError, KeyError):
                continue
            if ts >= since_epoch - 60 and size > 0:
                fills.append((price, size))
        return fills
    except Exception as e:
        logging.error(f"Wallet trades fetch failed for {market_id}: {e}")
        return None


_RESOLUTION_CACHE = {}

def get_market_resolution(market_id: str) -> dict | None:
    """
    Query the CLOB API for a market's resolution status using its conditionId.

    Returns a dict with keys:
      - resolved: bool
      - outcome: "YES" | "NO" | None
      - question: str

    Returns None if the API call fails or the market is not found.
    """
    if market_id in _RESOLUTION_CACHE:
        return _RESOLUTION_CACHE[market_id]

    try:
        url = f"{CLOB_BASE_URL}/markets/{market_id}"
        resp = safe_get(url, timeout=10)
        if resp.status_code != 200:
            logging.warning(f"Resolution check for {market_id}: HTTP {resp.status_code}")
            return None
        data = resp.json()

        closed = bool(data.get("closed"))
        active = bool(data.get("active"))
        resolved = closed and not active

        # Determine outcome from tokens[].winner
        outcome = None
        for token in data.get("tokens", []):
            if token.get("winner"):
                outcome = token.get("outcome", "").upper()  # "YES" or "NO"
                break

        # A market where a winner is declared is definitively resolved
        if outcome:
            resolved = True

        res = {
            "resolved": resolved,
            "outcome": outcome,
            "question": data.get("question", ""),
        }
        if resolved:
            _RESOLUTION_CACHE[market_id] = res
        return res
    except Exception as e:
        logging.error(f"Error fetching market resolution for {market_id}: {e}")
        return None


def _batch_fetch_prices(token_ids: list[str], max_workers: int = 20) -> dict[str, float]:
    """Fetch mid prices for many token IDs in parallel. Returns {token_id: mid_price}."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(get_mid_price, tid): tid for tid in token_ids}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                results[tid] = future.result()
            except Exception:
                results[tid] = 0.0
    return results


def prefetch_order_books(opportunities, max_workers: int = 20) -> None:
    """Warm the shared price cache (utils._PRICE_CACHE, 30s TTL, thread-safe) for every
    YES/NO token across all opportunities, in parallel, before the sequential Phase-2
    evaluation loop runs.

    Why this and not parallelizing the eval loop itself: evaluate_opportunity's sizing
    reads portfolio_state, and execute_trade mutates it — two markets evaluated out of
    order against a stale cash balance could both pass the "enough cash?" check and
    jointly overspend past MAX_TOTAL_EXPOSURE_FRACTION. That loop must stay sequential.
    The actual cost that scales with candidate count is get_live_spread_fraction's two
    live CLOB /book calls per market (one per side), made synchronously inside that
    sequential loop — at ~0.4-0.5s/market this is what turned a 1200-candidate scan
    into ~400s, eating most of the 600s scan interval. Those calls are independent,
    side-effect-free network reads (unlike execute_trade), so pre-fetching them
    concurrently ahead of time and letting the sequential loop read the warm cache
    is safe: it changes nothing about ordering or portfolio state, only when the
    HTTP round-trip happens.
    """
    token_ids = set()
    for opp in opportunities:
        token_ids.add(opp.token_id_no)
        # Only query YES order book if high markets / YES trades are enabled
        if TRADE_HIGH_MARKETS:
            token_ids.add(opp.token_id_yes)
    if not token_ids:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(get_realtime_price_status, tid) for tid in token_ids]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass  # get_realtime_price_status already caches (0,0,False) on failure


def _log_skip(market_id, question, reason, hours_to_res=None, volume=None, end_date=None):
    now = datetime.now(timezone.utc).isoformat()
    end_date_str = end_date.isoformat() if isinstance(end_date, datetime) else end_date
    try:
        execute_query(
            "INSERT INTO scan_log (timestamp, market_id, question, skip_reason, hours_to_res, volume, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, market_id or "UNKNOWN", question or "", reason, hours_to_res, volume, end_date_str)
        )
    except Exception as e:
        logging.error(f"Failed to log skip: {e}")


def _resolve_token_sides(market_data):
    tokens_raw = market_data.get("clobTokenIds", "[]")
    try:
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except (json.JSONDecodeError, TypeError):
        return None, None

    if len(tokens) < 2:
        return None, None

    outcomes_raw = market_data.get("outcomes", "[]")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    if outcomes and len(outcomes) >= 2:
        outcome_lower = [o.lower() if isinstance(o, str) else "" for o in outcomes]
        if "yes" in outcome_lower and "no" in outcome_lower:
            yes_idx = outcome_lower.index("yes")
            no_idx = outcome_lower.index("no")
            if yes_idx < len(tokens) and no_idx < len(tokens):
                return tokens[yes_idx], tokens[no_idx]

    logging.debug(f"Token order assumed for market {market_data.get('id')}: no 'outcomes' field found")
    return tokens[0], tokens[1]


def is_weather_market(question: str, metadata: dict) -> tuple[bool, str]:
    q_lower = question.lower()

    # 1. Negative keyword filtering
    negative_keywords = [
        "elections", "sports", "nba", "fifa", "nhl", "bitcoin", "crypto",
        "gta", "album", "prison", "sentencing", "politics", "war", "president"
    ]

    for word in negative_keywords:
        # Use regex word boundaries to avoid matching substrings like "war" in "Stewart"
        if re.search(rf"\b{word}\b", q_lower):
            if word in ["nba", "fifa", "nhl", "sports"]:
                return False, "sports-related market"
            elif word in ["elections", "politics", "president"]:
                return False, "politics-related market"
            elif word in ["bitcoin", "crypto"]:
                return False, "crypto-related market"
            else:
                return False, f"unrelated semantic domain ({word})"

    # 2. Positive keyword validation
    positive_keywords = [
        "temperature", "high", "low", "rain", "snowfall", "snow",
        "precipitation", "humidity", "wind", "hurricane", "storm", "weather",
        "°f", "°c"
    ]
    if not any(word in q_lower for word in positive_keywords):
        return False, "missing weather keywords"

    # 3. Structural validation
    city_key, _ = get_station_coords(question)
    if not city_key:
        return False, "no location"

    lb, ub = parse_bucket(question)
    if lb is None and ub is None:
        return False, "no measurable weather condition"

    return True, ""


def _fetch_events_page(offset: int, limit: int, session) -> list | None:
    """Fetch one page of active weather events (tag_id=84). Returns event list or None on error."""
    url = (
        f"{GAMMA_EVENTS_URL}?tag_id=84&active=true&limit={limit}&offset={offset}"
        f"&order=createdAt&ascending=false"
    )
    try:
        resp = safe_get(url, timeout=15)
    except Exception as e:
        logging.error(f"Discovery: failed to fetch events page at offset={offset}: {e}")
        return None

    if resp.status_code != 200:
        logging.error(f"Discovery: Gamma Events API returned {resp.status_code} at offset={offset}")
        return None

    data = resp.json()
    if not isinstance(data, list):
        logging.error(f"Discovery: unexpected events response type at offset={offset}: {type(data)}")
        return None

    return data


def _discover_weather_markets(now: datetime) -> tuple[list, dict]:
    """
    Fetch active weather events via the Gamma Events API (tag_id=84) and extract
    individual bucket markets from each event.

    The /markets endpoint buries weather markets under thousands of crypto/sports
    markets and filtering parameters are ignored. The events endpoint with tag_id=84
    directly targets the Weather tag and returns events with their markets pre-nested.

    Each event can have 10-15 bucket markets (temperature ranges). We extract all
    non-closed bucket markets and return them as the candidate list for Phase 2.

    Stops when:
      - MARKET_DISCOVERY_STOP_AFTER_WEATHER bucket markets have been collected, or
      - MARKET_DISCOVERY_MAX_PAGES event pages have been fetched, or
      - the API returns an empty page (exhausted).

    Returns:
      weather_markets: list of raw market dicts (individual buckets)
      discovery_stats: dict with counters for logging
    """
    session = get_session()
    weather_markets = []
    total_events_fetched = 0
    pages_fetched = 0
    events_skipped_closed = 0

    for page in range(MARKET_DISCOVERY_MAX_PAGES):
        offset = page * MARKET_DISCOVERY_LIMIT
        page_data = _fetch_events_page(offset, MARKET_DISCOVERY_LIMIT, session)

        if page_data is None:
            break

        if len(page_data) == 0:
            logging.info(f"Discovery: empty events page at offset={offset}, exhausted list")
            break

        pages_fetched += 1
        total_events_fetched += len(page_data)

        page_had_active = False
        for event in page_data:
            if event.get("closed"):
                events_skipped_closed += 1
                continue

            event_end_str = event.get("endDate") or event.get("endDateIso")
            if event_end_str:
                try:
                    event_end = parse_utc_datetime(event_end_str)
                    hours_away = (event_end - now).total_seconds() / 3600.0
                    # Allow active events whose listed endDate was within the last 36h
                    # (covering US 00:00Z endDate conventions during target day trading & settlement)
                    if hours_away < -36.0 or hours_away > MAX_HOURS_TO_RESOLUTION + 24.0:
                        continue
                except Exception:
                    pass

            markets = event.get("markets", [])
            for m in markets:
                if not m.get("active") or m.get("closed"):
                    continue
                # US daily-high markets carry a 00:00Z endDate (e.g. 7pm Houston time
                # the evening BEFORE target day), while remaining active & traded on the CLOB
                # all day long. Check target_date end-of-day UTC to determine true expiry.
                m_end_str = m.get("endDateIso") or m.get("endDate")
                if m_end_str:
                    try:
                        m_end = parse_utc_datetime(m_end_str)
                        q_text = m.get("question", "")
                        d_text = m.get("description", "")
                        t_date = parse_target_date(d_text, m_end, q_text)
                        ck, _ = get_station_coords(q_text)
                        if t_date:
                            t_end = get_target_day_end_utc(t_date, ck)
                            m_hours_away = (t_end - now).total_seconds() / 3600.0
                        else:
                            m_hours_away = (m_end - now).total_seconds() / 3600.0
                        if m_hours_away < -24.0 or m_hours_away > MAX_HOURS_TO_RESOLUTION + 24.0:
                            continue
                    except Exception:
                        pass
                weather_markets.append(m)
                page_had_active = True

        logging.debug(
            f"Discovery: events page {page + 1}/{MARKET_DISCOVERY_MAX_PAGES} "
            f"(offset={offset}, events={len(page_data)}), "
            f"bucket_markets_so_far={len(weather_markets)}"
        )

        # A quiet page is NOT a stopping signal: pages are createdAt-ordered, not
        # endDate-ordered (see comment above), so a burst of far-out or just-closed
        # events can produce a fully out-of-window page with live markets behind it.
        # Keep going; the empty-page check and MARKET_DISCOVERY_MAX_PAGES bound cost.
        if not page_had_active:
            logging.info(
                f"Discovery: page at offset={offset} had no in-window events, continuing."
            )

    discovery_stats = {
        "pages_fetched": pages_fetched,
        "total_raw_fetched": total_events_fetched,
        "weather_candidates": len(weather_markets),
        "top_skip_reasons": {},
    }

    return weather_markets, discovery_stats


def _log_discovery_summary(discovery_stats: dict) -> None:
    s = discovery_stats
    logging.info(
        f"Discovery complete: "
        f"{s['pages_fetched']} event page(s) fetched, "
        f"{s['total_raw_fetched']} weather events, "
        f"{s['weather_candidates']} bucket market(s) extracted"
    )
    if s["weather_candidates"] == 0:
        logging.warning(
            "Discovery: 0 weather bucket markets found after fetching "
            f"{s['total_raw_fetched']} weather events across "
            f"{s['pages_fetched']} page(s). "
            "Weather markets may not be live right now, or the Events API tag_id=84 returned no active events."
        )


def _run_debug_weather_discovery() -> None:
    """
    DEBUG_WEATHER_DISCOVERY mode: print all weather bucket markets and exit.
    Does not interact with the DB or CLOB. Called from scan_markets when the flag is set.
    """
    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"DEBUG_WEATHER_DISCOVERY mode — {now.isoformat()}")
    print(f"Fetching up to {MARKET_DISCOVERY_MAX_PAGES} event pages x {MARKET_DISCOVERY_LIMIT} events")
    print(f"Stop after: {MARKET_DISCOVERY_STOP_AFTER_WEATHER} bucket market candidates")
    print(f"Source: Gamma Events API tag_id=84 (Weather tag)")
    print(f"{'='*60}\n")

    weather_markets, stats = _discover_weather_markets(now)

    print(f"Pages fetched   : {stats['pages_fetched']}")
    print(f"Raw markets     : {stats['total_raw_fetched']}")
    print(f"Weather found   : {stats['weather_candidates']}")
    print(f"Top skip reasons: {stats['top_skip_reasons']}")
    print()

    if not weather_markets:
        print("No weather markets found. The bot will idle until weather markets become available.")
        return

    print(f"{'─'*60}")
    for m in weather_markets:
        question = m.get("question", "")
        end_date_str = m.get("endDateIso") or m.get("endDate") or "N/A"
        volume = float(m.get("liquidityNum") or m.get("liquidityClob") or 0)
        city_key, _ = get_station_coords(question)
        lb, ub = parse_bucket(question)

        if lb is not None and ub is not None:
            bucket_str = f"EXACT {lb}°F" if lb == ub else f"BETWEEN {lb}–{ub}°F"
        elif lb is not None:
            bucket_str = f"ABOVE {lb}°F"
        elif ub is not None:
            bucket_str = f"BELOW {ub}°F"
        else:
            bucket_str = "UNKNOWN"

        try:
            end_date = parse_utc_datetime(end_date_str)
            hours_to_res = (end_date - now).total_seconds() / 3600.0
            hrs_str = f"{hours_to_res:.1f}h"
        except Exception:
            hrs_str = "N/A"

        print(f"Market  : {question}")
        print(f"End     : {end_date_str}  ({hrs_str} to resolution)")
        print(f"City    : {city_key}  |  Bucket: {bucket_str}  |  Volume: ${volume:,.0f}")
        print(f"{'─'*60}")


def scan_markets():
    """
    Discover active weather markets and build trading opportunities.

    Discovery layer: paginates through all active Polymarket markets using
    offset-based pagination and applies is_weather_market() locally. The Gamma
    API's tag/category filter parameters are non-functional (verified: tag=weather
    and tag=NONSENSE return identical results). We do not rely on server-side
    filtering at all.
    """
    if DEBUG_WEATHER_DISCOVERY:
        _run_debug_weather_discovery()
        return []

    now = datetime.now(timezone.utc)

    # --- Phase 1: discover weather-classified raw markets ---
    weather_markets, discovery_stats = _discover_weather_markets(now)
    _log_discovery_summary(discovery_stats)

    if not weather_markets:
        return []

    # --- Phase 1.5: cheap pre-filter before hitting CLOB ---
    # Drop markets with no station mapping, unparseable bucket, or insufficient volume
    # so CLOB calls (and, more importantly, a scarce MAX_CLOB_CANDIDATES slot) are only
    # spent on markets that could actually be traded. MIN_VOLUME used to be enforced
    # only in Phase 2, AFTER the MAX_CLOB_CANDIDATES cap already truncated the pool —
    # meaning a sub-$500-liquidity market could win a scored slot over a real candidate
    # only to be dropped downstream anyway, wasting the same scarce capacity the
    # already-expired-market bug did. Filtering on volume here, before scoring/capping,
    # means every slot that survives to Phase 2 is one that could actually fill.
    prefiltered = []
    prefilter_skipped = 0
    volume_skipped = 0
    direction_skipped = 0
    for m in weather_markets:
        q = m.get("question", "")
        # Early direction filter: avoid CLOB queries and scoring for disabled market directions
        if not TRADE_HIGH_MARKETS and TRADE_LOW_MARKETS:
            is_low_m, is_high_m = parse_market_direction(q)
            if is_high_m or not is_low_m:
                direction_skipped += 1
                continue
        elif TRADE_HIGH_MARKETS and not TRADE_LOW_MARKETS:
            is_low_m, is_high_m = parse_market_direction(q)
            if is_low_m or not is_high_m:
                direction_skipped += 1
                continue

        city_key, _ = get_station_coords(q)
        if not city_key:
            prefilter_skipped += 1
            continue
        lb, ub = parse_bucket(q)
        if lb is None and ub is None:
            prefilter_skipped += 1
            continue
        volume = float(m.get("liquidityNum") or m.get("liquidityClob") or 0)
        if volume < MIN_VOLUME:
            volume_skipped += 1
            continue
        prefiltered.append(m)
    if prefilter_skipped or volume_skipped or direction_skipped:
        logging.info(
            f"Pre-filter: {direction_skipped} dropped (direction disabled), "
            f"{prefilter_skipped} dropped (no station/bucket), "
            f"{volume_skipped} dropped (volume < {MIN_VOLUME:.0f}), "
            f"{len(prefiltered)} remain for CLOB evaluation"
        )

    # Score candidates: 60% log-liquidity + 40% price uncertainty.
    # Price uncertainty peaks at 50/50 (YES price = 0.5) where edge potential is highest;
    # near-certain markets (0.02 or 0.98) score zero — the market already agrees.
    liq_max = max((float(m.get("liquidityNum") or 0) for m in prefiltered), default=1.0)
    for m in prefiltered:
        liq = float(m.get("liquidityNum") or 0)
        liq_norm = math.log1p(liq) / math.log1p(max(liq_max, 1.0))
        op = m.get("outcomePrices")
        try:
            prices = json.loads(op) if isinstance(op, str) else op
            yes_p = float(prices[0]) if prices and len(prices) >= 2 else 0.5
        except (TypeError, ValueError, IndexError):
            yes_p = 0.5
        m["_score"] = 0.6 * liq_norm + 0.4 * (1.0 - abs(yes_p - 0.5) * 2.0)

    prefiltered.sort(key=lambda m: m.get("_score", 0.0), reverse=True)

    if len(prefiltered) > MAX_CLOB_CANDIDATES:
        original_count = len(prefiltered)
        # Diversity cap: at most MAX_BUCKETS_PER_CITY_DATE buckets per city/date pair
        # so one city's full bucket ladder can't crowd out other cities.
        city_date_seen: dict = {}
        diverse: list = []
        overflow: list = []
        for m in prefiltered:
            q = m.get("question", "")
            ck, _ = get_station_coords(q)
            end_str = m.get("endDateIso") or m.get("endDate") or ""
            try:
                dk = parse_utc_datetime(end_str).strftime("%Y-%m-%d")
            except Exception:
                dk = end_str[:10]
            key = (ck, dk)
            count = city_date_seen.get(key, 0)
            if count < MAX_BUCKETS_PER_CITY_DATE:
                diverse.append(m)
                city_date_seen[key] = count + 1
            else:
                overflow.append(m)
        prefiltered = (diverse + overflow)[:MAX_CLOB_CANDIDATES]
        logging.info(
            f"Pre-filter: smart ranking capped at {MAX_CLOB_CANDIDATES} from {original_count} "
            f"(score=60%% liquidity+40%% price uncertainty, max {MAX_BUCKETS_PER_CITY_DATE} per city/date)"
        )

    weather_markets = prefiltered

    # --- Phase 2: apply trading filters to weather candidates ---
    opportunities = []
    scan_stats = {"total": len(weather_markets), "skipped": 0, "accepted": 0}
    skip_reasons_count: dict[str, int] = {}

    for m in weather_markets:
        market_id = m.get("conditionId", m.get("id", "UNKNOWN"))
        question = m.get("question", "")
        end_date_str = m.get("endDateIso") or m.get("endDate")
        volume = float(m.get("liquidityNum") or m.get("liquidityClob") or 0)

        hours_to_res = None
        end_date = None

        if end_date_str:
            try:
                end_date = parse_utc_datetime(end_date_str)
                hours_to_res = (end_date - now).total_seconds() / 3600.0
            except ValueError:
                pass

        city_key, _ = get_station_coords(question)
        lb, ub = parse_bucket(question)

        end_date = None
        if end_date_str:
            try:
                end_date = parse_utc_datetime(end_date_str)
            except ValueError:
                pass

        target_date = parse_target_date(m.get("description", ""), end_date, question)

        if target_date:
            target_end_dt = get_target_day_end_utc(target_date, city_key)
            hours_to_res = (target_end_dt - now).total_seconds() / 3600.0
        elif end_date:
            hours_to_res = (end_date - now).total_seconds() / 3600.0
        else:
            hours_to_res = None

        # Per-candidate detail only in verbose debug mode
        if DEBUG_MARKET_SCAN_VERBOSE:
            bucket_str = "UNKNOWN"
            if lb is not None and ub is not None:
                bucket_str = f"EXACT {lb}°F" if lb == ub else f"BETWEEN {lb}–{ub}°F"
            elif lb is not None:
                bucket_str = f"ABOVE {lb}°F"
            elif ub is not None:
                bucket_str = f"BELOW {ub}°F"
            hr_str = f"{hours_to_res:.1f}" if hours_to_res is not None else "N/A"
            end_date_disp = end_date.isoformat() if end_date else "N/A"
            logging.info(
                f"WEATHER_CANDIDATE | {market_id[:8]}.. | {question[:50]} | "
                f"End: {end_date_disp} | Hrs: {hr_str} | City: {city_key} | Bucket: {bucket_str}"
            )

        checks = {
            "within_72h": None,
            "not_expired": None,
            "station_mapping_exists": bool(city_key),
            "price_data_exists": None,
            "bucket_parse_success": (lb is not None or ub is not None),
            "sufficient_volume": volume >= MIN_VOLUME,
            "model_data_available": "N/A",  # checked in strategy.py
        }

        def print_debug(final_res):
            if "SKIPPED" in final_res and not DEBUG_MARKET_SCAN_VERBOSE:
                return
            if "ACCEPTED" in final_res and not DEBUG_MARKET_SCAN:
                return

            bucket_str_inner = "UNKNOWN"
            threshold_str = "N/A"
            if lb is not None and ub is not None:
                if lb == ub:
                    bucket_str_inner = "EXACT"
                    threshold_str = f"{lb}°F"
                else:
                    bucket_str_inner = "BETWEEN"
                    threshold_str = f"{lb}°F to {ub}°F"
            elif lb is not None:
                bucket_str_inner = "ABOVE"
                threshold_str = f"{lb}°F"
            elif ub is not None:
                bucket_str_inner = "BELOW"
                threshold_str = f"{ub}°F"

            status_msg = "CLOSED" if m.get("closed") else "ACTIVE"
            category = m.get("category", "Weather")

            msg = f"\n{'='*50}\n"
            msg += f"Market: {question}\n"
            msg += f"ID: {market_id}\n"
            msg += f"Category: {category}\n"
            msg += f"Status: {status_msg}\n"
            msg += f"Volume: ${volume:,.0f}\n\n"

            msg += "Time:\n"
            msg += f"- Raw End: {end_date_str}\n"
            msg += f"- Parsed UTC End: {end_date.isoformat() if end_date else 'N/A'}\n"
            msg += f"- Target Date: {target_date}\n"
            msg += f"- Current UTC: {now.isoformat()}\n"
            hr_str_inner = f"{hours_to_res:.1f}" if hours_to_res is not None else "N/A"
            msg += f"- Hours to resolution: {hr_str_inner}\n\n"

            msg += "Checks:\n"
            for k, v in checks.items():
                status_str = "PASS" if v is True else ("FAIL" if v is False else str(v))
                msg += f"- {k}: {status_str}\n"
            msg += f"- Bucket type: {bucket_str_inner}\n"
            msg += f"- Threshold: {threshold_str}\n"
            msg += f"- City: {city_key if city_key else 'N/A'}\n"

            msg += "\nFINAL:\n"
            msg += f"{final_res}\n"
            msg += f"{'='*50}"
            logging.info(msg)

        def do_skip(reason_msg, reason_key):
            _log_skip(market_id, question, reason_msg, hours_to_res, volume, end_date or end_date_str)
            scan_stats["skipped"] += 1
            skip_reasons_count[reason_key] = skip_reasons_count.get(reason_key, 0) + 1
            print_debug(f"SKIPPED: {reason_key}")

        try:
            if not checks["sufficient_volume"]:
                do_skip(f"Volume too low ({volume:.0f} < {MIN_VOLUME})", "volume_too_low")
                continue

            if hours_to_res is None:
                checks["not_expired"] = False
                checks["within_72h"] = False
                do_skip("Invalid or missing end date", "other")
                continue

            checks["not_expired"] = hours_to_res >= -24.0

            if not checks["not_expired"]:
                checks["within_72h"] = False
                do_skip("Already expired", "already_expired")
                continue

            checks["within_72h"] = hours_to_res <= MAX_HOURS_TO_RESOLUTION
            if not checks["within_72h"]:
                do_skip(
                    f"Too far out ({hours_to_res:.0f}h > {MAX_HOURS_TO_RESOLUTION}h)",
                    "outside_72h"
                )
                continue

            if not checks["station_mapping_exists"]:
                do_skip("No station mapping", "no_station_match")
                continue

            # A city whose SETTLEMENT WINDOW has not been established is not
            # tradeable, however good the forecast: the bet would settle on a
            # rule nobody has read. Karachi is the precedent — its description
            # named Masroor Airbase and linked Jinnah International, and
            # refusing to trade it was correct. Nothing is UNKNOWN as of the
            # 2026-08-05 audit; this is the enforcement that keeps it that way
            # when a resolution source changes.
            # `city_key`, not `city` — there is no `city` in this scope. Shipped
            # as a NameError in Phase 1.1 and caught only on the first real scan
            # after deploy (2026-08-06): the exception is swallowed by the
            # per-candidate handler, so every weather market failed with
            # "Error processing weather candidate ... name 'city' is not
            # defined" and the scan reported 0 candidates. Trading stopped
            # completely, and this guard never ran once.
            if not is_tradeable_window(city_key):
                do_skip(f"Settlement window not established for {city_key} "
                        f"(window={settlement_window(city_key)!r}) — see "
                        f"audit_settlement_windows.py", "unknown_settlement_window")
                continue

            # Resolve the measurement date from the market's own "...on <DATE>"
            # description text (the resolver's source of truth), not from the endDate
            # timestamp — the close-time convention drifted (00:00Z → 12:00Z) and a
            # UTC-date read mis-dates far-offset stations. Falls back to the endDate
            # UTC date when the description has no parseable date.
            target_date = parse_target_date(m.get("description", ""), end_date, question)

            if not checks["bucket_parse_success"]:
                do_skip("Cannot parse bucket", "bucket_parse_failed")
                continue

            # Use the immutably-stored bucket for this market_id, not a fresh
            # re-parse — see get_or_store_bucket's docstring for why this matters.
            lb, ub = get_or_store_bucket(market_id, question, city_key, target_date)

            # Can this bucket settle YES at all? A bucket containing no value
            # the station can report cannot pay, whatever the weather does.
            #
            # Routed for manual review rather than traded, and deliberately so:
            # US markets quote °F against °F-reporting stations and the rest
            # quote °C against °C-reporting stations, so a real impossible
            # bucket should never occur. One firing therefore says our PARSER is
            # wrong far more loudly than it says the market maker is — the
            # 2026-06 Celsius zero-width bucket bug is exactly this shape — and
            # betting real money on the opposite reading is the wrong way round
            # given that history.
            impossible, lattice_detail = star_tag(lb, ub, city_key, market_id, question)
            if impossible:
                flag_impossible_bucket(market_id, question, city_key, lb, ub,
                                       lattice_detail)
                do_skip(f"Impossible bucket [{lb}, {ub}]°F on {city_key}'s "
                        f"{lattice_detail['lattice']} grid — flagged for manual "
                        f"review, not traded", "impossible_bucket")
                continue

            token_yes, token_no = _resolve_token_sides(m)
            if not token_yes or not token_no:
                do_skip("Cannot resolve token IDs", "cannot_resolve_tokens")
                continue

            # Use prices already returned by the Gamma API — no CLOB call needed.
            # outcomePrices = [yes_price, no_price] mid; bestBid/bestAsk are for YES token.
            outcome_prices = m.get("outcomePrices")
            try:
                op = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                yes_mid = float(op[0]) if op and len(op) >= 2 else 0.0
                no_mid = float(op[1]) if op and len(op) >= 2 else 0.0
            except (TypeError, ValueError, IndexError):
                yes_mid = 0.0
                no_mid = 0.0

            # Fall back to bestBid/bestAsk mid if outcomePrices absent
            if yes_mid == 0.0:
                bid = float(m.get("bestBid") or 0)
                ask = float(m.get("bestAsk") or 0)
                if bid > 0 and ask > 0:
                    yes_mid = (bid + ask) / 2.0
                    no_mid = 1.0 - yes_mid
            checks["price_data_exists"] = yes_mid > 0.0 or no_mid > 0.0

            if not checks["price_data_exists"]:
                do_skip("No orderbook liquidity", "price_missing")
                continue

            is_low, is_high = parse_market_direction(question)

            opp = MarketOpportunity(
                market_id=market_id,
                token_id_yes=token_yes,
                token_id_no=token_no,
                city=city_key,
                date=target_date,
                bucket_low=lb,
                bucket_high=ub,
                yes_price=yes_mid,
                no_price=no_mid,
                volume=volume,
                hours_to_resolution=hours_to_res,
                question=question,
                is_high=is_high,
            )
            opportunities.append(opp)
            scan_stats["accepted"] += 1
            print_debug("ACCEPTED")

        except Exception as e:
            logging.error(f"Error processing weather candidate {market_id}: {e}", exc_info=True)
            do_skip(f"Parse error: {str(e)[:200]}", "other")

    # --- Phase 2 summary ---
    summary_counts = {
        "accepted": scan_stats["accepted"],
        "outside_72h": skip_reasons_count.get("outside_72h", 0),
        "already_expired": skip_reasons_count.get("already_expired", 0),
        "volume_too_low": skip_reasons_count.get("volume_too_low", 0),
        "no_station_match": skip_reasons_count.get("no_station_match", 0),
        "bucket_parse_failed": skip_reasons_count.get("bucket_parse_failed", 0),
        "price_missing": skip_reasons_count.get("price_missing", 0),
    }
    known = sum(v for k, v in skip_reasons_count.items() if k in summary_counts)
    summary_counts["other"] = scan_stats["skipped"] - known

    logging.info(
        f"Scan complete: {discovery_stats['total_raw_fetched']} weather events fetched across "
        f"{discovery_stats['pages_fetched']} event page(s), "
        f"{discovery_stats['weather_candidates']} bucket market candidates, "
        f"{scan_stats['accepted']} accepted for strategy evaluation"
    )
    logging.info("Filter breakdown (weather candidates only):")
    for key, count in summary_counts.items():
        if count:
            logging.info(f"  - {key}: {count}")

    return opportunities
