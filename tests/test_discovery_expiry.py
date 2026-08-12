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
for mod in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client_v2.client"].ClobClient = object
ct = sys.modules["py_clob_client_v2.clob_types"]
for n in ("MarketOrderArgsV2", "OrderArgsV2", "OrderType", "ApiCreds", "BalanceAllowanceParams", "AssetType"):
    setattr(ct, n, object)

import scanner


def test_expired_submarket_dropped_even_if_event_still_open(monkeypatch):
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    expired_market = {
        "active": True, "closed": False,
        "endDateIso": (now - timedelta(hours=72)).isoformat(),
        "question": "Will the highest temperature in Houston be between 94-95F on July 6?",
    }
    today_market = {
        "active": True, "closed": False,
        "endDateIso": (now - timedelta(hours=12)).isoformat(),  # 00:00Z on target date July 9
        "question": "Will the highest temperature in Houston be between 94-95F on July 9?",
    }
    # Tomorrow's bucket: its civil day ends July 11 05:00Z, 41h from `now` —
    # OUTSIDE the discovery horizon (MAX_HOURS_TO_RESOLUTION 16 + 24h slack =
    # 40h) since the 2026-08-12 same-day rule set. Discovery must drop it now
    # and pick it up on a later scan cycle once it enters the horizon.
    tomorrow_market = {
        "active": True, "closed": False,
        "endDateIso": (now + timedelta(hours=12)).isoformat(),
        "question": "Will the highest temperature in Houston be between 96-97F on July 10?",
    }
    event = {
        "closed": False,
        "endDate": (now + timedelta(hours=24)).isoformat(),  # event-level end still within window
        "markets": [expired_market, today_market, tomorrow_market],
    }

    monkeypatch.setattr(scanner, "_fetch_events_page", lambda offset, limit, session: [event] if offset == 0 else [])
    monkeypatch.setattr(scanner, "get_session", lambda: None)

    weather_markets, stats = scanner._discover_weather_markets(now)

    assert today_market in weather_markets
    assert expired_market not in weather_markets
    assert tomorrow_market not in weather_markets   # beyond the 40h horizon
    assert len(weather_markets) == 1
