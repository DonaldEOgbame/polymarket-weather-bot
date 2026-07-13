import requests
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone

import threading
import time
import random

_session: requests.Session | None = None
_PRICE_CACHE = {}
_PRICE_CACHE_LOCK = threading.Lock()

def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
            connect=3,
            read=3,
        )
        adapter = HTTPAdapter(
            pool_connections=50,
            pool_maxsize=50,
            max_retries=retry,
            pool_block=False
        )
        _session.mount("http://", adapter)
        _session.mount("https://", adapter)
    return _session

def safe_get(url: str, params=None, timeout=10, max_retries=3) -> requests.Response:
    """Wrapper around requests.get with exponential backoff and jitter for network/SSL resilience."""
    session = get_session()
    retries = 0
    backoff = 1.0
    while True:
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                retries += 1
                if retries > max_retries:
                    return resp
                sleep_time = backoff * (1.0 + random.random())
                logging.warning(f"Rate limited (429) on {url}. Retrying in {sleep_time:.2f}s...")
                time.sleep(sleep_time)
                backoff *= 2.0
                continue
            return resp
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            retries += 1
            if retries > max_retries:
                logging.error(f"Network error on {url} after {max_retries} retries: {e}")
                raise e
            sleep_time = backoff * (1.0 + random.random())
            logging.warning(f"Network error on {url} ({e}). Retrying in {sleep_time:.2f}s...")
            time.sleep(sleep_time)
            backoff *= 2.0

def get_cached_price(token_id: str):
    """Retrieve cached ask, bid, and reachability for a token_id if not expired (TTL=30s)."""
    now = time.time()
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(token_id)
        if cached and (now - cached["timestamp"] < 30):
            return cached["ask"], cached["bid"], cached["reachable"]
    return None

def get_cached_depth(token_id: str):
    """Retrieve cached ask/bid $ depth for a token_id if not expired (TTL=30s).
    Returns None if not cached or expired — separate from get_cached_price so
    callers that only need price aren't forced to also fetch/store depth."""
    now = time.time()
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(token_id)
        if cached and (now - cached["timestamp"] < 30) and "ask_depth" in cached:
            return cached["ask_depth"], cached["bid_depth"]
    return None

def set_cached_price(token_id: str, ask: float, bid: float, reachable: bool, ask_depth: float = None, bid_depth: float = None):
    """Update cache with latest price details. ask_depth/bid_depth (total $ resting
    on each side) are optional — only set when the caller already parsed the full
    book, so the common price-only path doesn't pay for data it won't use."""
    with _PRICE_CACHE_LOCK:
        entry = {
            "ask": ask,
            "bid": bid,
            "reachable": reachable,
            "timestamp": time.time()
        }
        if ask_depth is not None:
            entry["ask_depth"] = ask_depth
            entry["bid_depth"] = bid_depth
        _PRICE_CACHE[token_id] = entry

def ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware and set to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def parse_utc_datetime(date_str: str) -> datetime:
    """Safely parse an ISO format string into a UTC-aware datetime."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return ensure_utc(dt)
    except Exception as e:
        logging.warning(f"Failed to parse datetime from '{date_str}': {e}")
        raise ValueError(f"Invalid datetime format: {date_str}")
