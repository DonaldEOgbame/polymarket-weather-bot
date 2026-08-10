"""Tests for target date parsing, station local end-of-day calculation,
and market visibility for US weather daily-high markets carrying 00:00Z endDates.
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

def test_parse_target_date_from_question_without_year():
    q = "Will the highest temperature in Houston be between 88-89°F on August 10?"
    d = ""
    date_str = scanner.parse_target_date(d, None, q)
    assert date_str == f"{datetime.now(timezone.utc).year}-08-10"

def test_get_target_day_end_utc_houston():
    target_date = "2026-08-10"
    end_utc = scanner.get_target_day_end_utc(target_date, "Houston")
    # Houston (America/Chicago, CDT UTC-5) day ends at 2026-08-10 23:59:59 CDT
    # = 2026-08-11 04:59:59 UTC
    assert end_utc == datetime(2026, 8, 11, 4, 59, 59, tzinfo=timezone.utc)

def test_us_market_with_midnight_utc_enddate_remains_visible_on_target_day(monkeypatch):
    # Simulated current time: Aug 10, 2026 at 09:30 UTC (morning of target day in Houston)
    now = datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc)
    
    # US Houston Aug 10 market carrying Polymarket midnight-UTC endDate (2026-08-10T00:00:00Z)
    houston_market = {
        "id": "3433941",
        "conditionId": "0xc844f3d121805407a83dcaa4d1d1d18a49e33dec86e1e928bca6037a7879b5cf",
        "active": True,
        "closed": False,
        "endDate": "2026-08-10T00:00:00Z",
        "endDateIso": "2026-08-10",
        "question": "Will the highest temperature in Houston be between 88-89°F on August 10?",
        "description": "recorded at William P. Hobby Airport Station in degrees Fahrenheit on 10 Aug '26.",
        "liquidityNum": 2000.0,
        "outcomePrices": '["0.245", "0.755"]',
    }

    event = {
        "closed": False,
        "endDate": "2026-08-10T00:00:00Z",
        "markets": [houston_market],
    }

    monkeypatch.setattr(scanner, "_fetch_events_page", lambda offset, limit, session: [event] if offset == 0 else [])
    monkeypatch.setattr(scanner, "get_session", lambda: None)

    markets, stats = scanner._discover_weather_markets(now)
    assert houston_market in markets, "Houston Aug 10 market must remain visible during target day"
