"""The discrete grid a market actually settles on.

Two things live here, and the first is a live bug fix rather than a feature.

SETTLEMENT. metar.py rounded every city's reading to a whole °C before
converting to °F. Measured 2026-08-05, that is wrong for the eleven North
American cities: US ASOS reports whole °F, so IEM's 27.78°C is exactly 82°F,
rounded to 28°C and returned as 82.4°F. US markets settle on 2°F-wide buckets,
so up to 0.9°F of purely self-inflicted error was deciding outcomes.

  KORD  12.3% of readings on a whole °C, 9 distinct fractional parts
  EGLC  100%                             1

REACHABILITY. A bucket holding no grid point cannot pay YES. 44% of whole °F
values are unreachable from a whole °C reading, so this is possible in
principle — but 0 of 20,988 live markets have it, because every market's
quoting unit matches its station's grid. That makes a firing evidence of a
PARSER bug, which is why it routes for review instead of trading.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import lattice as L
import weather as W


class TestEveryCityDeclaresItsGrid:
    def test_no_station_is_missing_a_lattice(self):
        missing = [c for c, s in W.STATIONS.items() if "lattice" not in s]
        assert missing == []

    def test_only_f_or_c(self):
        assert {s["lattice"] for s in W.STATIONS.values()} <= {"F", "C"}

    def test_north_american_cities_are_f(self):
        """These are the eleven whose settlement was being corrupted."""
        for city in ("Chicago", "New York", "NYC", "Miami", "Dallas", "Austin",
                     "Houston", "Atlanta", "Denver", "Seattle",
                     "Los Angeles", "San Francisco"):
            assert L.lattice_for_city(city) == "F", city

    def test_toronto_is_c_despite_being_north_american(self):
        """The rule is the MARKET's quoting unit, not the continent. Toronto's
        markets quote °C and CYYZ reports whole °C — a 'US = °F' shortcut would
        have got this wrong."""
        assert L.lattice_for_city("Toronto") == "C"

    def test_the_rest_of_the_world_is_c(self):
        for city in ("London", "Tokyo", "Hong Kong", "Wellington", "Moscow",
                     "Guangzhou", "Singapore", "Sao Paulo"):
            assert L.lattice_for_city(city) == "C", city


class TestQuantisationFixesTheSettlementBug:
    def test_a_us_reading_snaps_to_whole_f(self):
        """27.78°C IS 82°F. The old path rounded it to 28°C and returned
        82.4°F."""
        assert L.quantise_c(27.78, "Chicago") == 82.0
        assert L.quantise_c(25.56, "Chicago") == 78.0
        assert L.quantise_c(26.67, "Chicago") == 80.0

    def test_the_old_behaviour_was_wrong_by_up_to_point_nine(self):
        """Quantifies the bug so the fix cannot be undone as cosmetic."""
        old = lambda c: L.round_half_away(c) * 9.0 / 5.0 + 32.0
        worst = max(abs(old(c) - L.quantise_c(c, "Chicago"))
                    for c in (27.78, 27.22, 26.67, 26.11, 25.56, 25.0))
        assert worst >= 0.5, f"worst observed error {worst}"

    def test_a_c_city_is_unchanged(self):
        for c in (24.0, 30.0, -5.0):
            assert L.quantise_c(c, "Tokyo") == c * 9.0 / 5.0 + 32.0

    def test_half_degrees_round_away_from_zero(self):
        """Wunderground's convention. Python's round() is banker's rounding,
        which mis-scores exactly the boundary readings markets settle on."""
        assert L.quantise_c(24.5, "Tokyo") == 25 * 9.0 / 5.0 + 32.0
        assert L.quantise_c(30.5, "Tokyo") == 31 * 9.0 / 5.0 + 32.0

    def test_rounding_happens_in_the_source_unit(self):
        """A regression caught by the existing metar tests while this was being
        written: going °C -> °F -> °C to round reintroduces float error at
        exactly the half-degree boundaries. 24.5°C becomes 76.1°F, which
        converts back to 24.499999999999996 and rounds DOWN to 24 — a
        one-degree settlement error on precisely the readings that decide
        outcomes."""
        assert L.quantise_c(24.5, "Tokyo") == pytest.approx(77.0)
        naive = (24.5 * 9 / 5 + 32 - 32) * 5 / 9
        assert L.round_half_away(naive) == 24, "the trap this avoids still exists"

    def test_an_unknown_city_is_not_quantised(self):
        """Rounding onto a grid a station does not live on is the bug this
        module fixes; doing it on a hunch is the same bug with less evidence."""
        assert L.quantise_c(27.78, "Atlantis") == pytest.approx(27.78 * 9 / 5 + 32)


class TestReachability:
    def test_an_exact_celsius_bucket_has_exactly_one_reachable_value(self):
        """"33°C" is stored as (91.0, 91.8), pads to [90.5, 92.3]°F =
        [32.5, 33.5]°C, and contains exactly 33°C = 91.4°F. These markets are
        bets on a single discrete outcome — 13,203 of the 20,988 live markets
        are this shape."""
        assert L.reachable_values(91.0, 91.8, "Hong Kong") == [pytest.approx(91.4)]

    def test_a_two_degree_f_bucket_has_two(self):
        assert L.reachable_values(74.0, 75.0, "Chicago") == [74.0, 75.0]

    def test_the_padded_interval_is_used(self):
        """Reachability must be computed over the SAME interval
        get_bucket_probability integrates, or it raises false alarms. The stored
        bounds are pre-padding."""
        assert L.settleable_interval(91.0, 91.8) == (90.5, pytest.approx(92.3))
        assert L.settleable_interval(None, 50.0)[0] == -200.0
        assert L.settleable_interval(50.0, None)[1] == 200.0

    def test_a_genuinely_impossible_bucket_is_detected(self):
        """On a °C grid, 44% of whole °F values are unreachable."""
        impossible, detail = L.star_tag(77.6, 78.2, "London")
        assert impossible is True
        assert detail["reachable"] == []
        assert detail["lattice"] == "C"

    def test_an_unknown_city_never_claims_impossibility(self):
        """None (grid unknown) is not the same as [] (grid known, nothing in
        it), and only the second is a finding."""
        impossible, detail = L.star_tag(78.1, 78.4, "Atlantis")
        assert impossible is False and detail["reachable"] is None

    def test_open_ended_buckets_are_reachable(self):
        vals = L.reachable_values(87.2, None, "London")
        assert vals and len(vals) > 10


class TestNoLiveMarketIsImpossible:
    """0 of 20,988 checked on 2026-08-05. Pinned as a handful of real questions
    so a parse_bucket regression surfaces here rather than as a flood of
    review-queue rows in production."""

    @pytest.mark.parametrize("question,city", [
        ("Will the highest temperature in Hong Kong be 33°C on July 1?", "Hong Kong"),
        ("Will the highest temperature in Chicago be between 74-75°F on August 6?", "Chicago"),
        ("Will the highest temperature in Chicago be 73°F or below on August 6?", "Chicago"),
        ("Will the lowest temperature in London be 11°C on August 7?", "London"),
        ("Will the lowest temperature in Tokyo be 22°C or below on August 7?", "Tokyo"),
        ("Will the highest temperature in Wellington be 12°C on July 1?", "Wellington"),
        ("Will the highest temperature in Dallas be 95°F or below on August 6?", "Dallas"),
    ])
    def test_real_questions_are_all_reachable(self, question, city):
        from scanner import parse_bucket
        lb, ub = parse_bucket(question)
        vals = L.reachable_values(lb, ub, city)
        assert vals, f"{question!r} parsed to ({lb}, {ub}) with no reachable value"


class TestWiredIntoTheScanPath:
    def test_the_scanner_flags_and_skips(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "scanner.py")).read()
        assert "star_tag(" in src
        assert "impossible_bucket" in src
        assert "flag_impossible_bucket" in src

    def test_an_impossible_bucket_is_never_traded(self):
        """'Do not auto-trade, do not bypass gates.' The skip must come before
        the opportunity is constructed."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "scanner.py")).read()
        body = src.split("def scan_markets")[1]
        assert body.index("star_tag(") < body.index("MarketOpportunity(")

    def test_metar_uses_the_city_grid(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "metar.py")).read()
        assert "quantise_c" in src
        assert "rounded_c = round_half_away(val_c)" not in src, (
            "the universal whole-°C rounding is back"
        )


class TestTheReviewQueue:
    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        import config as C
        path = str(tmp_path / "bot.db")
        monkeypatch.setattr(C, "DB_PATH", path)
        import db as dbmod
        monkeypatch.setattr(dbmod, "DB_PATH", path)
        dbmod.init_db()
        return dbmod

    def test_a_flag_is_archived(self, db):
        db.flag_impossible_bucket("0xa", "q", "London", 77.6, 78.2,
                                  {"lattice": "C", "reachable": []})
        rows = db.get_impossible_buckets()
        assert len(rows) == 1 and rows[0]["city"] == "London"

    def test_reseeing_a_market_bumps_the_count_not_the_row_count(self, db):
        """A persistently mispriced market is re-seen every scan cycle. Without
        the upsert that is thousands of rows a day."""
        for _ in range(5):
            db.flag_impossible_bucket("0xa", "q", "London", 77.6, 78.2,
                                      {"lattice": "C", "reachable": []})
        rows = db.get_impossible_buckets()
        assert len(rows) == 1 and rows[0]["times_seen"] == 5

    def test_reviewed_rows_leave_the_queue(self, db):
        db.flag_impossible_bucket("0xa", "q", "London", 77.6, 78.2, {"lattice": "C"})
        db.execute_query("UPDATE impossible_buckets SET reviewed=1 WHERE market_id=?", ("0xa",))
        assert db.get_impossible_buckets() == []
        assert len(db.get_impossible_buckets(include_reviewed=True)) == 1
