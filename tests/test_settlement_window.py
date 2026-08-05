"""Settlement window per city — which day, measured how, settles a market.

Established by reading 20,988 live market descriptions on 2026-08-05, not by
convention. The audit script is audit_settlement_windows.py and the report is
reports/settlement-windows-2026-08-05.md.

The finding was uniform and negative: every city settles on the LOCAL calendar
day, verbatim ("the highest temperature recorded for all times on this day for
the <STATION> Station"). No market uses 00-24Z; none uses 6-hourly synoptic max
groups. The Hong Kong, Moscow, Seoul and London corrections were STATION-identity
bugs, not day-boundary bugs.

So these tests defend two things: that the audited value is recorded for every
station rather than assumed, and that a city whose window is NOT established is
actually excluded from trading rather than merely flagged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import weather as W


class TestEveryStationDeclaresAnAuditedWindow:
    def test_no_station_is_missing_a_window(self):
        missing = [c for c, s in W.STATIONS.items() if "window" not in s]
        assert missing == [], f"unaudited stations: {missing}"

    def test_every_window_is_a_known_value(self):
        bad = {c: s["window"] for c, s in W.STATIONS.items()
               if s["window"] not in W.VALID_WINDOWS}
        assert bad == {}

    def test_the_audit_found_local_everywhere(self):
        """Pins the 2026-08-05 result. If a city ever legitimately differs, this
        fails and forces the change to be made deliberately, with the quote."""
        assert {s["window"] for s in W.STATIONS.values()} == {"local"}

    def test_boot_validation_rejects_a_missing_window(self, monkeypatch):
        """A city added without anyone reading how its markets settle must not
        boot. That omission is how the station bugs got in."""
        monkeypatch.setitem(W.STATIONS, "Atlantis",
                            {"lat": 0.0, "lon": 0.0, "region": "GLOBAL"})
        problems = W.validate_city_tables()
        assert any("Atlantis" in p and "window" in p for p in problems)

    def test_boot_validation_rejects_a_nonsense_window(self, monkeypatch):
        monkeypatch.setitem(W.STATIONS, "Atlantis",
                            {"lat": 0.0, "lon": 0.0, "region": "GLOBAL",
                             "window": "whenever"})
        assert any("Atlantis" in p and "whenever" in p
                   for p in W.validate_city_tables())


class TestUnknownWindowExcludesFromTrading:
    def test_a_known_city_is_tradeable(self):
        assert W.is_tradeable_window("Chicago") is True
        assert W.settlement_window("Chicago") == "local"

    def test_an_unknown_window_is_not_tradeable(self, monkeypatch):
        """The Karachi precedent: the description named one station and linked
        another, and refusing to trade was correct."""
        monkeypatch.setitem(W.STATIONS, "Chicago",
                            dict(W.STATIONS["Chicago"], window="UNKNOWN"))
        assert W.is_tradeable_window("Chicago") is False

    def test_an_unmapped_city_is_not_tradeable(self):
        assert W.is_tradeable_window("Atlantis") is False

    def test_the_scanner_actually_skips_it(self):
        """Asserts the call site. A window field nothing reads is documentation,
        not a gate."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "scanner.py")).read()
        assert "is_tradeable_window" in src
        assert "unknown_settlement_window" in src


class TestTheReaderMatchesTheAuditedWindow:
    def test_metar_filters_to_the_stations_local_day(self):
        """'local' means the reader must use the station's IANA timezone, not
        UTC. For Tokyo (+9) and Wellington (+12) a UTC day covers two different
        local afternoons."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "metar.py")).read()
        body = src.split("def fetch_day_extremes")[1].split("\ndef ")[0]
        assert '"tz": tz' in body, "the IEM request must pass the station timezone"
        assert 'row.get("valid", "")[:10] != date_str' in body, (
            "rows must be filtered to the target LOCAL day"
        )

    def test_every_station_has_a_timezone_to_localise_with(self):
        """A 'local' window is unimplementable without one."""
        from metar import STATION_ICAO
        missing = [c for c in W.STATIONS if c not in STATION_ICAO]
        assert missing == [], f"no timezone for: {missing}"


class TestTheAuditScriptItself:
    def test_it_classifies_the_real_resolution_text(self):
        import audit_settlement_windows as A
        real = ("This market will resolve according to the highest temperature "
                "recorded in Chicago on 5 Aug '26. The resolution source will be "
                "information from Wunderground, specifically the highest "
                "temperature recorded for all times on this day for the Chicago "
                "O'Hare Intl Airport Station, available here: https://...")
        window, quote = A.classify_window(real)
        assert window == "local" and "for all times on this day" in quote

    def test_it_does_not_guess_on_silent_text(self):
        """A loose pattern that classified ambiguous text as 'local' would
        produce exactly the confident-and-wrong outcome the audit exists to
        prevent. UNKNOWN is recoverable; a wrong guess is not."""
        import audit_settlement_windows as A
        assert A.classify_window("Will it be hot in Chicago?")[0] == "UNKNOWN"
        assert A.classify_window("")[0] == "UNKNOWN"

    def test_it_recognises_the_utc_and_six_hourly_conventions(self):
        import audit_settlement_windows as A
        assert A.classify_window("measured over the UTC day")[0] == "00-24Z"
        assert A.classify_window("the 6-hourly maximum groups")[0] == "6h-groups"

    def test_non_temperature_markets_are_excluded(self):
        """A live false alarm: NYC came back UNKNOWN on 16 markets that were all
        monthly precipitation, which have no daily settlement window because
        they are not daily."""
        import audit_settlement_windows as A
        assert not A.is_temperature_market(
            "Will NYC have less than 2 inches of precipitation in August?")
        assert A.is_temperature_market(
            "Will the highest temperature in Chicago be 33°C on August 5?")

    def test_disagreeing_markets_are_ambiguous_however_lopsided(self):
        """Two conventions in one city's population means the text does not
        determine the window — a majority vote there would be a guess."""
        import audit_settlement_windows as A
        by_city = {"Chicago": (
            [{"question": "q", "description": "for all times on this day"}] * 9
            + [{"question": "q", "description": "measured in UTC"}])}
        res = A.audit(by_city)
        assert res["Chicago"]["window"] == "UNKNOWN"
        assert "disagree" in res["Chicago"]["reason"]

    def test_no_market_is_not_the_same_as_ambiguous(self):
        """Excluding a city for having no market open today would permanently
        drop cities for being out of season."""
        import audit_settlement_windows as A
        res = A.audit({})
        assert res["Chicago"]["window"] == "NO_MARKET"
        assert res["Chicago"]["window"] != "UNKNOWN"
