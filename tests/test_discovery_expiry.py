"""Regression test: recurring weather events stay open/active while nesting
individual bucket sub-markets whose OWN endDate has already passed. Discovery
only checked the event-level endDate, so these stale buckets sailed through
every scan cycle only to be dropped downstream as "Already expired" — burning
MAX_CLOB_CANDIDATES slots on dead markets every cycle (97% of scan_log skips
over a 2-day trade drought) and starving out live candidates.
"""
import sys, os
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import types
for mod in ("py_clob_client", "py_clob_client.client", "py_clob_client.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client.client"].ClobClient = object
ct = sys.modules["py_clob_client.clob_types"]
for n in ("OrderArgs", "MarketOrderArgs", "OrderType"):
    setattr(ct, n, object)

import scanner


def test_expired_submarket_dropped_even_if_event_still_open(monkeypatch):
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    expired_market = {
        "active": True, "closed": False,
        "endDateIso": (now - timedelta(hours=12)).isoformat(),
        "question": "Will the highest temperature in Houston be between 94-95F on July 9?",
    }
    live_market = {
        "active": True, "closed": False,
        "endDateIso": (now + timedelta(hours=12)).isoformat(),
        "question": "Will the highest temperature in Houston be between 96-97F on July 10?",
    }
    event = {
        "closed": False,
        "endDate": (now + timedelta(hours=48)).isoformat(),  # event-level end still within window
        "markets": [expired_market, live_market],
    }

    monkeypatch.setattr(scanner, "_fetch_events_page", lambda offset, limit, session: [event] if offset == 0 else [])
    monkeypatch.setattr(scanner, "get_session", lambda: None)

    weather_markets, stats = scanner._discover_weather_markets(now)

    assert live_market in weather_markets
    assert expired_market not in weather_markets
    assert len(weather_markets) == 1
