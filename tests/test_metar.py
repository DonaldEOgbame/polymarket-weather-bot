"""Tests for metar.py — resolution rounding and the day-extremes cache policy."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from metar import round_half_away, _METAR_CACHE
import metar


class TestRoundHalfAway:
    """Wunderground's whole-degree rollup rounds half AWAY from zero; Python's
    round() is banker's rounding and disagrees on exactly the boundary readings."""

    def test_half_up(self):
        assert round_half_away(30.5) == 31   # round() gives 30

    def test_half_even_case(self):
        assert round_half_away(29.5) == 30

    def test_negative_half(self):
        assert round_half_away(-0.5) == -1   # round() gives 0

    def test_plain_values(self):
        assert round_half_away(30.4) == 30
        assert round_half_away(30.6) == 31
        assert round_half_away(-2.4) == -2
        assert round_half_away(-2.6) == -3


class TestCachePolicy:
    """The cache froze same-day observations forever, blinding the intraday
    bucket-bust check. Only complete past days with real data may be cached."""

    def _fetch(self, monkeypatch, date_str, payload):
        class Resp:
            status_code = 200
            text = payload
        monkeypatch.setattr(metar, "safe_get", lambda *a, **k: Resp())
        return metar.fetch_day_extremes("KTST", "UTC", date_str)

    def test_today_not_cached(self, monkeypatch):
        _METAR_CACHE.clear()
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        payload = f"station,valid,tmpc\nKTST,{today} 09:00,25.0\n"
        assert self._fetch(monkeypatch, today, payload) == (25.0, 25.0)
        assert ("KTST", today) not in _METAR_CACHE

    def test_past_day_cached(self, monkeypatch):
        _METAR_CACHE.clear()
        payload = "station,valid,tmpc\nKTST,2020-01-01 09:00,10.0\nKTST,2020-01-01 15:00,20.0\n"
        assert self._fetch(monkeypatch, "2020-01-01", payload) == (20.0, 10.0)
        assert _METAR_CACHE[("KTST", "2020-01-01")] == (20.0, 10.0)

    def test_failure_not_cached(self, monkeypatch):
        _METAR_CACHE.clear()
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(metar, "safe_get", boom)
        assert metar.fetch_day_extremes("KTST", "UTC", "2020-01-01") == (None, None)
        assert ("KTST", "2020-01-01") not in _METAR_CACHE


class TestFinalExtremeF:
    """final_extreme_f must return None until the station-local day has fully
    elapsed — settling on a partial-day max booked phantom wins (Guangzhou 2026-07-23
    was 'resolved' at 8:36am local using the morning temperature as the daily high)."""

    def test_none_while_local_day_in_progress(self, monkeypatch):
        monkeypatch.setattr(metar, "day_complete", lambda tz, d: False)
        called = []
        monkeypatch.setattr(metar, "resolved_extreme_f", lambda *a: called.append(a) or 95.0)
        assert metar.final_extreme_f("Guangzhou", "2026-07-23", True) is None
        assert called == []  # must not even consult the partial-day feed

    def test_passes_through_once_day_complete(self, monkeypatch):
        monkeypatch.setattr(metar, "day_complete", lambda tz, d: True)
        monkeypatch.setattr(metar, "resolved_extreme_f", lambda *a: 96.8)
        assert metar.final_extreme_f("Guangzhou", "2026-07-22", True) == 96.8

    def test_none_for_unknown_city(self):
        assert metar.final_extreme_f("Atlantis", "2026-07-22", True) is None


class TestDayComplete:
    def test_today_is_incomplete(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today_sh = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        assert metar.day_complete("Asia/Shanghai", today_sh) is False

    def test_distant_past_is_complete(self):
        assert metar.day_complete("Asia/Shanghai", "2026-07-01") is True


class TestHKOSettlement:
    """Hong Kong settles on the HKO Daily Extract (one-decimal, 'range contains' =
    floor), never on the airport METAR the other cities use."""

    HKO_PAYLOAD = {"stn": {"data": [{"month": 7, "dayData": [
        ["26", " 997.5", "28.3", "26.1", "24.6", "23.9", "87", "97", "109.2"],
        ["27", "1005.7", "28.7", "26.8", "25.2", "24.4", "87", "87", " 14.9"],
        ["Mean/Total", "1005.5", "31.5", "29.0", "26.9", "25.4", "82", "79", "616.0"],
        ["Normal", "1005.6", "31.6", "28.9", "26.9", "25.2", "81", "72", "385.8"],
    ]}]}}

    def _mock_hko(self, monkeypatch):
        class Resp:
            status_code = 200
            def json(self):
                return TestHKOSettlement.HKO_PAYLOAD
        monkeypatch.setattr(metar, "safe_get", lambda *a, **k: Resp())
        metar._HKO_CACHE.clear()

    def test_floor_not_round_half_away(self, monkeypatch):
        """28.7°C is the 28 bucket ('range that contains'), not 29 — the one reading
        where WU rounding and HKO semantics disagree."""
        self._mock_hko(monkeypatch)
        monkeypatch.setattr(metar, "day_complete", lambda tz, ds: True)
        val = metar.final_extreme_f("Hong Kong", "2026-07-27", is_high=True)
        assert val == 28 * 9.0 / 5.0 + 32.0  # floor(28.7)=28, NOT round_half_away=29

    def test_summary_rows_skipped(self, monkeypatch):
        self._mock_hko(monkeypatch)
        assert metar.hko_day_extremes_c("2026-07-26") == (28.3, 24.6)
        assert metar.hko_day_extremes_c("2026-07-31") == (None, None)

    def test_settlement_waits_for_publication(self, monkeypatch):
        """Day complete but HKO row not yet published -> final_extreme_f must return
        None (the market itself cannot resolve yet), NEVER the airport METAR."""
        self._mock_hko(monkeypatch)
        monkeypatch.setattr(metar, "day_complete", lambda tz, ds: True)
        monkeypatch.setattr(metar, "fetch_day_extremes",
                            lambda *a: (25.0, 22.0))  # airport data exists
        assert metar.final_extreme_f("Hong Kong", "2026-07-28", is_high=True) is None

    def test_intraday_falls_back_to_airport(self, monkeypatch):
        """Before publication, resolved_extreme_f (monitoring only) may use the
        airport METAR as a rough running proxy."""
        self._mock_hko(monkeypatch)
        monkeypatch.setattr(metar, "fetch_day_extremes", lambda *a: (25.4, 22.0))
        val = metar.resolved_extreme_f("Hong Kong", "2026-07-28", is_high=True)
        assert val == 25 * 9.0 / 5.0 + 32.0

    def test_day_extremes_f_other_cities_unchanged(self, monkeypatch):
        """Non-HKO cities keep the WU round-half-away convention."""
        monkeypatch.setattr(metar, "day_complete", lambda tz, ds: True)
        monkeypatch.setattr(metar, "fetch_day_extremes", lambda *a: (30.5, 24.5))
        mx, mn = metar.day_extremes_f("Tokyo", "2026-07-27")
        assert mx == 31 * 9.0 / 5.0 + 32.0   # 30.5 rounds AWAY to 31
        assert mn == 25 * 9.0 / 5.0 + 32.0

    def test_current_month_not_cached(self, monkeypatch):
        self._mock_hko(monkeypatch)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_hk = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        metar._fetch_hko_month(now_hk.year, now_hk.month)
        assert f"{now_hk.year:04d}{now_hk.month:02d}" not in metar._HKO_CACHE
        metar._fetch_hko_month(2025, 3)
        assert "202503" in metar._HKO_CACHE
