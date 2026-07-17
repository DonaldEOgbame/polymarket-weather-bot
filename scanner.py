import requests
import re
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from weather import get_station_coords, STATIONS
from db import execute_query, fetch_query
from config import (
    MIN_VOLUME, MAX_HOURS_TO_RESOLUTION, GAMMA_EVENTS_URL, GAMMA_API_URL, CLOB_BASE_URL,
    DEBUG_MARKET_SCAN, DEBUG_MARKET_SCAN_VERBOSE, DEBUG_WEATHER_DISCOVERY,
    MARKET_DISCOVERY_LIMIT, MARKET_DISCOVERY_MAX_PAGES,
    MARKET_DISCOVERY_STOP_AFTER_WEATHER, MAX_CLOB_CANDIDATES,
    MAX_BUCKETS_PER_CITY_DATE,
)
from utils import get_session, parse_utc_datetime, safe_get, get_cached_price, set_cached_price, get_cached_depth

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

    Prefers the description's "on <D Mon 'YY>"; falls back to the UTC date of end_date.
    """
    if description:
        m = re.search(
            r"\bon\s+(\d{1,2})\s+([A-Za-z]{3,9})\s+'?(\d{2,4})\b",
            description,
        )
        if m:
            day = int(m.group(1))
            mon = _MONTHS.get(m.group(2)[:3].lower())
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
            if mon and 1 <= day <= 31:
                try:
                    return datetime(yr, mon, day, tzinfo=timezone.utc).strftime("%Y-%m-%d")
                except ValueError:
                    pass
    # Fallback: UTC calendar date of the close timestamp.
    if end_date is not None:
        return end_date.strftime("%Y-%m-%d")
    return None


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
            set_cached_price(token_id, best_ask, best_bid, True, ask_depth, bid_depth)
            return best_ask, best_bid, True
        logging.warning(f"Orderbook for {token_id}: HTTP {resp.status_code}")
    except Exception as e:
        logging.error(f"Error fetching orderbook for {token_id}: {e}")

    set_cached_price(token_id, 0.0, 0.0, False)
    return 0.0, 0.0, False


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
        token_ids.add(opp.token_id_yes)
        token_ids.add(opp.token_id_no)
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
                    # Skip events already expired or too far out — but keep paginating
                    # because createdAt order doesn't guarantee endDate monotonicity.
                    if hours_away < 0 or hours_away > MAX_HOURS_TO_RESOLUTION:
                        continue
                except Exception:
                    pass

            markets = event.get("markets", [])
            for m in markets:
                if not m.get("active") or m.get("closed"):
                    continue
                # Gamma's active=true filter lags: events (and their nested bucket
                # markets) commonly sit active=true/closed=false for hours after
                # their own endDate passes, before Polymarket flips the flags.
                # The event-level date check above catches the event's own
                # endDate, but each market can carry a different endDate. Verified
                # live: 220/248 unique markets scanned over 2026-07-07→09 were
                # already past endDate, sailing through discovery every cycle only
                # to be dropped as "Already expired" in Phase 2 — burning nearly
                # all of MAX_CLOB_CANDIDATES on dead markets and starving out the
                # few live ones (root cause of the 2-day trade drought).
                m_end_str = m.get("endDateIso") or m.get("endDate")
                if m_end_str:
                    try:
                        m_end = parse_utc_datetime(m_end_str)
                        m_hours_away = (m_end - now).total_seconds() / 3600.0
                        if m_hours_away < 0 or m_hours_away > MAX_HOURS_TO_RESOLUTION:
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
    for m in weather_markets:
        q = m.get("question", "")
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
    if prefilter_skipped or volume_skipped:
        logging.info(
            f"Pre-filter: {prefilter_skipped} dropped (no station/bucket), "
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

            if not end_date:
                checks["not_expired"] = False
                checks["within_72h"] = False
                do_skip("Invalid or missing end date", "other")
                continue

            checks["not_expired"] = hours_to_res >= 0

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

            q_lower = question.lower()
            # Whole-word matching only: bare substring matching flagged "40°F or
            # below" (and even "Glasgow") as daily-LOW markets via the "low" inside
            # "below"/"glasgow".
            low_keywords  = ("low", "min", "lowest", "minimum", "cold", "coolest")
            high_keywords = ("high", "max", "highest", "maximum", "warm", "hottest")
            def _word_pos(w):
                m = re.search(r'\b' + re.escape(w) + r'\b', q_lower)
                return m.start() if m else None
            low_hits  = [p for p in (_word_pos(w) for w in low_keywords) if p is not None]
            high_hits = [p for p in (_word_pos(w) for w in high_keywords) if p is not None]
            is_low, is_high = bool(low_hits), bool(high_hits)
            if is_high and is_low:
                # Both present (e.g. "high of 70 low of 55") — pick by which comes first
                is_high = min(high_hits) <= min(low_hits)
            elif not is_high and not is_low:
                # Ambiguous — default to high (daily max is the most common market type)
                is_high = True

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
