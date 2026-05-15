import requests
import re
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from weather import get_station_coords
from db import execute_query
from config import (
    MIN_VOLUME, MAX_HOURS_TO_RESOLUTION, GAMMA_EVENTS_URL, CLOB_BASE_URL,
    DEBUG_MARKET_SCAN, DEBUG_MARKET_SCAN_VERBOSE, DEBUG_WEATHER_DISCOVERY,
    MARKET_DISCOVERY_LIMIT, MARKET_DISCOVERY_MAX_PAGES,
    MARKET_DISCOVERY_STOP_AFTER_WEATHER, MAX_CLOB_CANDIDATES,
    MAX_BUCKETS_PER_CITY_DATE,
)
from utils import get_session, parse_utc_datetime

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


def parse_bucket(question: str):
    """Parse a temperature bucket from a market question.

    Handles both °F (US/UK markets) and °C (international markets).
    All returned values are in °F to match Open-Meteo's fahrenheit output.
    """
    q_original = question.lower()

    # Detect unit: prefer explicit marker; default to °F if absent
    is_celsius = bool(re.search(r'°\s*c\b', q_original))

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

    def convert(val: float) -> float:
        return _c_to_f(val) if is_celsius else val

    if "below" in q_original or "under" in q_original or "or below" in q_original:
        if temp_matches:
            return (None, convert(float(temp_matches[-1])))
    elif ("above" in q_original or "or more" in q_original or "exceed" in q_original
          or "at least" in q_original or "or higher" in q_original):
        if temp_matches:
            return (convert(float(temp_matches[0])), None)
    elif range_match:
        low, high = float(range_match.group(1)), float(range_match.group(2))
        if low < high:
            return (convert(low), convert(high))

    if temp_matches:
        val = float(temp_matches[0])
        if -100 <= val <= 200:
            return (convert(val), convert(val))

    return (None, None)


def get_realtime_price_status(token_id):
    """Fetch best ask/bid for a token. Returns (ask, bid, reachable).

    reachable is False only when the CLOB request itself failed (network down,
    timeout, non-200) — distinct from a reachable-but-empty orderbook, which
    returns (0.0, 0.0, True). Callers can use this to tell "API down" apart
    from "market genuinely has no liquidity"."""
    try:
        resp = get_session().get(f"{CLOB_BASE_URL}/book?token_id={token_id}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            asks = data.get("asks", [])
            bids = data.get("bids", [])
            best_ask = float(asks[0]["price"]) if asks else 0.0
            best_bid = float(bids[0]["price"]) if bids else 0.0
            return best_ask, best_bid, True
        logging.warning(f"Orderbook for {token_id}: HTTP {resp.status_code}")
    except Exception as e:
        logging.error(f"Error fetching orderbook for {token_id}: {e}")
    return 0.0, 0.0, False


def get_realtime_price(token_id):
    ask, bid, _ = get_realtime_price_status(token_id)
    return ask, bid


def get_mid_price(token_id):
    ask, bid = get_realtime_price(token_id)
    if ask > 0 and bid > 0:
        return (ask + bid) / 2.0
    return ask or bid


def get_market_resolution(market_id: str) -> dict | None:
    """
    Query the CLOB API for a market's resolution status using its conditionId.

    Returns a dict with keys:
      - resolved: bool
      - outcome: "YES" | "NO" | None
      - question: str

    Returns None if the API call fails or the market is not found.
    """
    try:
        url = f"{CLOB_BASE_URL}/markets/{market_id}"
        resp = get_session().get(url, timeout=10)
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

        return {
            "resolved": resolved,
            "outcome": outcome,
            "question": data.get("question", ""),
        }
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
        resp = session.get(url, timeout=15)
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
                weather_markets.append(m)
                page_had_active = True

        logging.debug(
            f"Discovery: events page {page + 1}/{MARKET_DISCOVERY_MAX_PAGES} "
            f"(offset={offset}, events={len(page_data)}), "
            f"bucket_markets_so_far={len(weather_markets)}"
        )

        # Stop when a full page has no in-window events — we've gone back far enough.
        if not page_had_active:
            logging.info(
                f"Discovery: page at offset={offset} had no in-window events, stopping pagination."
            )
            break

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
    # Drop markets with no station mapping or unparseable bucket so CLOB calls
    # are only made for markets that could actually be traded.
    prefiltered = []
    prefilter_skipped = 0
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
        prefiltered.append(m)
    if prefilter_skipped:
        logging.info(
            f"Pre-filter: {prefilter_skipped} markets dropped (no station or bucket), "
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

            target_date = end_date.strftime("%Y-%m-%d")

            if not checks["bucket_parse_success"]:
                do_skip("Cannot parse bucket", "bucket_parse_failed")
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

            q_lower = question.lower()
            low_keywords  = ("low", "min", "lowest", "minimum", "cold", "coolest")
            high_keywords = ("high", "max", "highest", "maximum", "warm", "hottest")
            is_low  = any(w in q_lower for w in low_keywords)
            is_high = any(w in q_lower for w in high_keywords)
            if is_high and is_low:
                # Both present (e.g. "high of 70 low of 55") — pick by which comes first
                first_high = min((q_lower.find(w) for w in high_keywords if w in q_lower), default=9999)
                first_low  = min((q_lower.find(w) for w in low_keywords  if w in q_lower), default=9999)
                is_high = first_high <= first_low
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
