"""Portfolio-level correlated risk limits.

The gap these close: MAX_CONCURRENT_POSITIONS counts positions and
ONE_TRADE_PER_CITY_DATE deduplicates a city-day, so four NO bets across Dallas,
Austin, Houston and Atlanta on one target date look like four independent
positions to every check the bot had. They are one bet on one ridge, sized 4x.

The live book on 2026-08-05 held Dallas and Austin on the same target date, both
high-bucket NO. That is the shape, at half the size.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C
import risk as R


def _pos(city, date, size, direction="HOT"):
    return {"city": city, "target_date": date, "size_usdc": size,
            "risk_direction": direction}


@pytest.fixture
def stake6():
    """Production's stake, so the stake-denominated caps mean dollars here."""
    before = C.setting("FIXED_POSITION_SIZE")
    C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 6.0})
    yield 6.0
    C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before})


class TestSynopticGrouping:
    def test_every_station_is_mapped(self):
        assert R.validate_synoptic_groups() == []

    def test_texas_cities_share_a_group(self):
        """The concrete case. One ridge covers both."""
        assert R.synoptic_group("Dallas") == R.synoptic_group("Austin") == "US_SOUTH"

    def test_distant_cities_do_not_share_a_group(self):
        """Beijing and Hong Kong are 2,000km apart across a monsoon boundary. A
        single 'East Asia' group would bind on positions that are genuinely
        uncorrelated while still permitting four correlated South China bets."""
        assert R.synoptic_group("Beijing") != R.synoptic_group("Hong Kong")
        assert R.synoptic_group("Guangzhou") == R.synoptic_group("Hong Kong") == "SOUTH_CHINA"

    def test_loose_market_names_resolve(self):
        """Markets name cities loosely; the group lookup has to match what
        get_station_coords does or the cap silently misses."""
        assert R.synoptic_group("Highest temperature in New York City") == "US_EAST"
        assert R.synoptic_group("Hong Kong high temp") == "SOUTH_CHINA"

    def test_an_unmapped_city_is_reported_not_silently_exempt(self):
        assert R.synoptic_group("Reykjavik") is None


class TestRiskDirectionIsTheAxisAHeatWaveCorrelates:
    def test_no_on_a_bucket_above_the_forecast_busts_on_heat(self):
        assert R.risk_direction("NO", 95.0, 96.8, ensemble_mean=90.0) == "HOT"

    def test_no_on_a_bucket_below_the_forecast_busts_on_cold(self):
        assert R.risk_direction("NO", 84.0, 85.8, ensemble_mean=90.0) == "COLD"

    def test_open_ended_above_and_below(self):
        assert R.risk_direction("NO", 95.0, None, 90.0) == "HOT"
        assert R.risk_direction("NO", None, 85.0, 90.0) == "COLD"

    def test_is_high_is_the_wrong_axis(self):
        """Two NO bets on 'highest temperature' markets can be exposed in
        OPPOSITE directions. Grouping by is_high would pool positions that hedge
        each other and split positions that compound — which is why the cap is
        keyed on this function and not on the is_high column."""
        hot = R.risk_direction("NO", 95.0, 96.8, 90.0)   # bucket above forecast
        cold = R.risk_direction("NO", 84.0, 85.8, 90.0)  # bucket below forecast
        assert hot != cold                                # both are is_high markets

    def test_undeterminable_is_none_not_a_guess(self):
        assert R.risk_direction("NO", 95.0, 96.8, None) is None
        assert R.risk_direction("NO", None, None, 90.0) is None


class TestGroupCap:
    def test_current_live_book_is_not_blocked(self, stake6):
        """The caps must start loose enough not to bind on current flow. The
        live book's largest same-group same-date exposure is Dallas + Austin =
        2 stakes; a third US_SOUTH entry that day still fits under 3."""
        book = [_pos("Dallas", "2026-08-05", 6.0), _pos("Austin", "2026-08-05", 6.0)]
        allowed, why, _ = R.check_correlation_limits(
            book, "Houston", "2026-08-05", 6.0, "HOT")
        assert allowed, why

    def test_the_fourth_correlated_entry_is_refused(self, stake6):
        """At MAX_CONCURRENT_POSITIONS=4 this is the case where the ENTIRE
        portfolio is one weather event."""
        book = [_pos("Dallas", "2026-08-05", 6.0), _pos("Austin", "2026-08-05", 6.0),
                _pos("Houston", "2026-08-05", 6.0)]
        allowed, why, detail = R.check_correlation_limits(
            book, "Atlanta", "2026-08-05", 6.0, "HOT")
        assert not allowed
        assert "US_SOUTH" in why and "Synoptic group cap" in why
        assert detail["group_exposure"] == 18.0 and detail["group_cap"] == 18.0

    def test_the_cap_is_per_target_date(self, stake6):
        """Three US_SOUTH positions on a DIFFERENT day are a different weather
        event and must not block today's entry."""
        book = [_pos("Dallas", "2026-08-04", 6.0), _pos("Austin", "2026-08-04", 6.0),
                _pos("Houston", "2026-08-04", 6.0)]
        allowed, _, _ = R.check_correlation_limits(
            book, "Atlanta", "2026-08-05", 6.0, "HOT")
        assert allowed

    def test_other_groups_do_not_count_toward_this_group(self, stake6):
        book = [_pos("Munich", "2026-08-05", 6.0), _pos("Beijing", "2026-08-05", 6.0),
                _pos("Tel Aviv", "2026-08-05", 6.0)]
        allowed, _, detail = R.check_correlation_limits(
            book, "Dallas", "2026-08-05", 6.0, "HOT")
        assert allowed and detail["group_exposure"] == 0.0

    def test_the_cap_scales_with_the_stake(self):
        """Expressed in stakes, so raising the stake cannot loosen it. Three
        $12 positions must breach exactly as three $6 ones do."""
        before = C.setting("FIXED_POSITION_SIZE")
        try:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 12.0})
            book = [_pos("Dallas", "2026-08-05", 12.0), _pos("Austin", "2026-08-05", 12.0),
                    _pos("Houston", "2026-08-05", 12.0)]
            allowed, _, detail = R.check_correlation_limits(
                book, "Atlanta", "2026-08-05", 12.0, "HOT")
            assert not allowed and detail["group_cap"] == 36.0
        finally:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before})


class TestSameDirectionCap:
    def test_exposure_across_groups_accumulates(self, stake6):
        """A hemispheric heat event correlates cities sharing no synoptic
        system. Six stakes HOT on one date, spread across six groups, passes
        every per-group check and is still one bet."""
        book = [_pos(c, "2026-08-05", 6.0, "HOT") for c in
                ("Dallas", "Munich", "Beijing", "Tel Aviv", "Lagos", "Sao Paulo")]
        allowed, why, detail = R.check_correlation_limits(
            book, "Tokyo", "2026-08-05", 6.0, "HOT")
        assert not allowed
        assert "Same-direction cap" in why
        assert detail["direction_exposure"] == 36.0

    def test_opposite_direction_does_not_count(self, stake6):
        """COLD-exposed positions are the hedge, not the concentration."""
        book = [_pos(c, "2026-08-05", 6.0, "COLD") for c in
                ("Dallas", "Munich", "Beijing", "Tel Aviv", "Lagos", "Sao Paulo")]
        allowed, _, detail = R.check_correlation_limits(
            book, "Tokyo", "2026-08-05", 6.0, "HOT")
        assert allowed and detail["direction_exposure"] == 0.0

    def test_it_is_inert_at_current_concurrency(self, stake6):
        """Documented as deliberately non-binding today: 4 concurrent x 1 stake
        cannot reach 6. If this ever fails, concurrency was raised and the cap
        has become the operative ceiling — which is the intent."""
        assert C.MAX_DIRECTION_STAKES_PER_DATE > C.MAX_CONCURRENT_POSITIONS

    def test_unknown_direction_positions_are_counted_and_reported(self, stake6):
        """Excluding them from the cap is correct — they cannot be classified —
        but doing so silently would let a book of unclassified positions hollow
        the limit out entirely."""
        book = [_pos("Dallas", "2026-08-05", 6.0, None),
                _pos("Munich", "2026-08-05", 6.0, None)]
        allowed, _, detail = R.check_correlation_limits(
            book, "Tokyo", "2026-08-05", 6.0, "HOT")
        assert allowed
        assert detail["positions_with_unknown_direction"] == 2


class TestTheRefusalIsLoggedWithItsCause:
    def test_detail_carries_the_binding_numbers(self, stake6):
        book = [_pos("Dallas", "2026-08-05", 6.0), _pos("Austin", "2026-08-05", 6.0),
                _pos("Houston", "2026-08-05", 6.0)]
        _, _, detail = R.check_correlation_limits(
            book, "Atlanta", "2026-08-05", 6.0, "HOT")
        for key in ("group", "group_exposure", "group_cap",
                    "direction_exposure", "direction_cap", "target_date"):
            assert key in detail, f"{key} missing — the log cannot say what bound"
        assert detail["group"] == "US_SOUTH"

    def test_the_flag_disables_the_caps(self, stake6, monkeypatch):
        monkeypatch.setattr(C, "ENABLE_CORRELATION_LIMITS", False)
        book = [_pos(c, "2026-08-05", 6.0) for c in ("Dallas", "Austin", "Houston")]
        allowed, why, _ = R.check_correlation_limits(
            book, "Atlanta", "2026-08-05", 6.0, "HOT")
        assert allowed and why is None


class TestWiredIntoTheEntryPath:
    def test_executor_consults_the_caps_before_opening(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        body = src.split("def execute_trade")[1].split("\n    def ")[0]
        assert "check_correlation_limits" in body
        assert body.index("check_correlation_limits") < body.index("open_position_atomic"), (
            "the cap must be checked BEFORE the position is written"
        )

    def test_strategy_supplies_the_direction(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "strategy.py")).read()
        assert '"risk_direction": risk_direction(' in src

    def test_the_direction_is_persisted(self):
        """The cap reads it back off open positions on the next scan, so a
        direction computed and then dropped would make every position unknown."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "db.py")).read()
        assert "risk_direction" in src.split("def open_position_atomic")[1][:2000]


class TestBackfillOfPreChangePositions:
    """Without this the same-direction cap is hollow until the pre-change book
    turns over — days, under hold-to-settlement, and exactly the window the
    change is meant to be watched in."""

    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        path = str(tmp_path / "bot.db")
        monkeypatch.setattr(C, "DB_PATH", path)
        import db as dbmod
        monkeypatch.setattr(dbmod, "DB_PATH", path)
        dbmod.init_db()
        return dbmod

    def test_uses_the_decision_time_mean_from_the_replay_log(self, db):
        db.execute_query(
            "INSERT INTO positions (market_id, side, city, target_date, size_usdc, mode) "
            "VALUES (?,?,?,?,?,?)", ("0xa", "NO", "Dallas", "2026-08-05", 6.0, "live"))
        # Two evaluations of the same market; the EARLIEST is closest to entry.
        for mean in (90.0, 97.0):
            db.execute_query(
                "INSERT INTO replay_signals (timestamp, schema_version, config_fingerprint, "
                "market_id, ensemble_mean, bucket_low, bucket_high) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2026-08-05T00:00:00", 1, "fp", "0xa", mean, 95.0, 96.8))
        assert R.backfill_risk_direction() == 1
        row = db.fetch_query("SELECT risk_direction FROM positions")[0]
        # Bucket 95-96.8 sits ABOVE the entry-time mean of 90 -> busts on heat.
        # Against the later mean of 97 it would have classified COLD.
        assert row["risk_direction"] == "HOT"

    def test_a_position_with_no_replay_row_stays_unknown(self, db):
        db.execute_query(
            "INSERT INTO positions (market_id, side, city, target_date, size_usdc, mode) "
            "VALUES (?,?,?,?,?,?)", ("0xghost", "NO", "Dallas", "2026-08-05", 6.0, "live"))
        assert R.backfill_risk_direction() == 0
        assert db.fetch_query("SELECT risk_direction FROM positions")[0]["risk_direction"] is None

    def test_it_does_not_reclassify_already_set_rows(self, db):
        db.execute_query(
            "INSERT INTO positions (market_id, side, city, target_date, size_usdc, mode, "
            "risk_direction) VALUES (?,?,?,?,?,?,?)",
            ("0xb", "NO", "Dallas", "2026-08-05", 6.0, "live", "COLD"))
        db.execute_query(
            "INSERT INTO replay_signals (timestamp, schema_version, config_fingerprint, "
            "market_id, ensemble_mean, bucket_low, bucket_high) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-08-05T00:00:00", 1, "fp", "0xb", 90.0, 95.0, 96.8))
        assert R.backfill_risk_direction() == 0
        assert db.fetch_query("SELECT risk_direction FROM positions")[0]["risk_direction"] == "COLD"
