"""Drift guards for the constants changed — and deliberately NOT changed — on
2026-07-31.

These are not behavioural tests. They exist because this project's failure mode
is a constant that moves without anyone noticing: METAR_WARM_CORRECTION_F sat at
a stale +1.3 for three weeks, and MODEL_BIAS_CORRECTIONS changed twice in one
afternoon with no record. A value decided by the owner should break the build
when it drifts, not quietly trade differently.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Every env var that could shadow a default under test. Cleared so the test
# measures config.py, not whatever the developer's shell happens to export.
_SHADOWING = (
    "NARROW_BUCKET_EDGE_THRESHOLD", "NARROW_BUCKET_STD_INFLATION",
    "NARROW_BUCKET_WIDTH_F", "EDGE_THRESHOLD", "MAX_ENTRY_PRICE",
    "STOP_LOSS_PCT", "ENABLE_STOP_LOSS", "PAPER_MODE",
    "SIGMA_SCALE_HIGH", "SIGMA_SCALE_LOW", "SIGMA_SPREAD_COEF",
    "METAR_WARM_CORRECTION_F", "MIN_SIGMA_F", "MAX_SIGMA_F",
    "PROB_CALIBRATION_INTERCEPT", "PROB_CALIBRATION_SLOPE", "MIN_BUCKET_PROB",
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """config.py as deployed: no env overrides, no settings rows."""
    for name in _SHADOWING:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    import config
    importlib.reload(config)
    return config


class TestNarrowBucketThreshold:
    def test_deployed_value_is_the_owner_decision(self, cfg):
        """0.12, decided by the owner on 2026-07-31. NOT a fitted value.

        If this fails, someone changed the threshold. That is allowed — but it
        is a decision, and the comment block in config.py must be rewritten to
        say who made it and why, exactly as the 0.20 -> 0.12 change did.
        """
        assert cfg.NARROW_BUCKET_EDGE_THRESHOLD == 0.00

    def test_narrow_gate_is_never_looser_than_the_general_gate(self, cfg):
        """The narrow-bucket gate exists to be STRICTER. Below EDGE_THRESHOLD it
        would make thin buckets the EASIEST markets to enter — the inverse of
        the guard."""
        assert cfg.NARROW_BUCKET_EDGE_THRESHOLD >= cfg.EDGE_THRESHOLD

    def test_inversion_is_caught_by_the_boot_time_guard(self, tmp_path, monkeypatch):
        """A test only fails in CI. validate_env_ranges() is what fails on a Fly
        secret, which is how a stale value actually reaches production."""
        for name in _SHADOWING:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
        monkeypatch.setenv("EDGE_THRESHOLD", "0.05")
        monkeypatch.setenv("NARROW_BUCKET_EDGE_THRESHOLD", "0.02")
        import config
        importlib.reload(config)
        try:
            problems = config.validate_env_ranges()
            assert any("NARROW_BUCKET_EDGE_THRESHOLD" in p for p in problems)
        finally:
            monkeypatch.delenv("NARROW_BUCKET_EDGE_THRESHOLD", raising=False)
            monkeypatch.delenv("EDGE_THRESHOLD", raising=False)
            importlib.reload(config)

    def test_clean_config_reports_no_stale_values(self, cfg):
        assert cfg.validate_env_ranges() == []

    def test_threshold_is_in_the_replay_fingerprint(self, cfg):
        """A threshold change must be visible in the replay log, or a later
        replay silently mixes rows from two configurations."""
        assert "NARROW_BUCKET_EDGE_THRESHOLD" in cfg._FINGERPRINT_KEYS
        before = cfg.config_fingerprint()
        cfg.NARROW_BUCKET_EDGE_THRESHOLD = 0.20
        try:
            assert cfg.config_fingerprint() != before
        finally:
            cfg.NARROW_BUCKET_EDGE_THRESHOLD = 0.12


class TestUnchangedGuardrails:
    """The threshold change ships alone. Anything else moving in the same commit
    would make the next replay uninterpretable — two changes, one sample."""

    def test_max_entry_price_still_armed(self, cfg):
        """0.80 (owner decision 2026-08-12, same-day minimal rule set): the
        band 0.70-0.80 IS the strategy — market-implied 70-80% in our
        favour. Was 0.85 (2026-08-06)."""
        assert cfg.MAX_ENTRY_PRICE == 0.77

    def test_min_model_confidence_entry_filter(self, cfg):
        assert cfg.MIN_MODEL_CONFIDENCE == 0.60

    def test_max_model_confidence_entry_filter(self, cfg):
        assert cfg.MAX_MODEL_CONFIDENCE == 0.85

    def test_min_entry_price_entry_filter(self, cfg):
        """0.65 (2026-08-06 owner decision, reaffirmed 2026-08-08 after a
        brief same-day move to 0.62): armed re-entry, not a lower floor, is
        how qualified-but-cheap markets get in — the bot waits for the price
        to reach the floor."""
        assert cfg.MIN_ENTRY_PRICE == 0.70

    def test_max_hours_to_resolution_entry_filter(self, cfg):
        """16h to the local civil day end (owner decision 2026-08-12): only
        markets resolving the same day, entered on the target day itself.
        Was 24.0."""
        assert cfg.MAX_HOURS_TO_RESOLUTION == 48.0  # 2026-08-14: widened, 16h was starved

    def test_armed_ttl_never_outlives_the_trading_window(self, cfg):
        """validate_env_ranges refuses TTL > MAX_HOURS_TO_RESOLUTION; the two
        must move together (an arm outliving the window could only fire
        outside it)."""
        assert cfg.ARMED_SIGNAL_TTL_HOURS <= cfg.MAX_HOURS_TO_RESOLUTION

    def test_non_binding_gates_membership(self, cfg):
        """Drift guard on the 2026-08-12 owner decision: exactly the edge and
        model-quality rows are non-binding. Adding a gate here loosens the
        strategy silently; removing one re-tightens it — both are owner
        calls, not refactors."""
        import strategy
        assert strategy.NON_BINDING_GATES == {
            "edge_threshold", "model_agreement", "model_spread_sd",
            "model_confidence", "max_model_confidence"}
        # forecast_margin RE-BOUND 2026-08-14 with the 48h widening: at 48h the
        # full stack (margin binding) beats direction-only on every recency
        # window — last 2 weeks 87.9% vs 79.9%, EV $0.52 vs $0.21.

    def test_narrow_bucket_std_inflation_still_on(self, cfg):
        """Second corrector of the same defect. Removing it too would be the
        third correction, and the Platt constants were fitted through it."""
        assert cfg.NARROW_BUCKET_STD_INFLATION == 1.1

    def test_paper_mode_default_unchanged(self, cfg):
        """More trade flow is not a deploy criterion."""
        assert cfg.PAPER_MODE is True
        assert cfg.paper_mode() is True

    def test_the_percentage_stop_ships_disabled(self, cfg):
        """The freeze this test used to enforce — "until the monitor trail covers
        >=10 closed positions incl. >=2 losses" — was MET on 2026-08-11 at 10 closed
        and 5 losses, and the answer was to retire the rule, not retune it.

        Every threshold scored net-negative against those 10 resolutions (20%:
        -$15.69, 25%: -$4.69, 30%: +$0.25, 35%: -$4.75, 40%: -$5.55, 45%: -$9.65,
        50%: -$10.15), and optimistically so — the grid fills at the exact threshold
        price while Qingdao really filled 5c through a gutted book. The threshold was
        never the problem: winners drew down to -55%, -28%, -24% and losers to -42%,
        -31%, with one loser never passing -10%, so no cut separates them.

        STOP_LOSS_PCT itself is left at 0.50 deliberately. It is dead code while the
        flag is off, and moving it would imply some level is defensible."""
        assert cfg.STOP_LOSS_PCT == 0.50
        assert cfg.ENABLE_STOP_LOSS is False
        # What replaced it: observations decide, price only proposes.
        assert cfg.ENABLE_PHYSICS_EXIT_GATE is True

    def test_sigma_and_bias_constants_untouched(self, cfg):
        """The 2026-07-31 direction-split recalibration, unchanged."""
        assert cfg.SIGMA_SCALE_HIGH == 1.02
        assert cfg.SIGMA_SCALE_LOW == 0.80
        assert cfg.SIGMA_SPREAD_COEF == 2.78
        assert cfg.METAR_WARM_CORRECTION_F == 0.0
        assert cfg.PROB_CALIBRATION_INTERCEPT == 0.8000
        assert cfg.PROB_CALIBRATION_SLOPE == 0.7480
        assert cfg.MIN_BUCKET_PROB == 0.05


class TestExcludedCities:
    """Wrong-thermometer exclusions (owner decision 2026-08-13): stations whose
    microclimate structurally diverges from the modeled air mass. Measured
    station-vs-forecast bias 2026-08-05..11: KLAX -7.0F etc. Change the set
    deliberately, with the refit evidence, not casually."""

    def test_the_excluded_set_is_pinned(self):
        import config
        # 2026-08-13 (same day, hours apart): blocked 5 -> 7 -> owner reversed
        # to recalibrate-first; default is EMPTY. The gate stays; re-add cities
        # here only with refit evidence.
        assert config.EXCLUDED_CITIES == set()

    def test_every_excluded_city_is_a_real_station(self):
        import config, weather
        assert not (config.EXCLUDED_CITIES - set(weather.STATIONS))

    def test_exclusion_is_a_binding_gate_not_a_scanner_filter(self):
        """The gate must refuse entry AND stay out of NON_BINDING_GATES, while
        the city keeps logging signals (which is why it is a gate at all)."""
        import strategy
        assert "excluded_city" not in strategy.NON_BINDING_GATES
        import inspect
        src = inspect.getsource(strategy._no_side_gates)
        assert "excluded_city" in src

    def test_fingerprint_tracks_the_set(self):
        import config
        assert "EXCLUDED_CITIES" in config._FINGERPRINT_KEYS


class TestLowsOnly:
    """Owner decision 2026-08-13 ('only the lows'): the five-window backtest
    showed highs efficiently priced everywhere (74.9-75.3% vs ~75.2% BE) while
    the full stack on lows held 88.9-100% in every window (year n=128, ~3.7s)."""

    def test_market_kind_defaults(self):
        import config
        assert config.TRADE_HIGH_MARKETS is False
        assert config.TRADE_LOW_MARKETS is True

    def test_market_kind_is_a_binding_gate(self):
        import strategy, inspect
        assert "market_kind" not in strategy.NON_BINDING_GATES
        assert "market_kind" in inspect.getsource(strategy._no_side_gates)

    def test_trapped_low_guard_sits_before_the_submit(self):
        import executor, inspect
        src = inspect.getsource(executor.Executor.execute_trade)
        assert src.index("resolved_extreme_f") < src.index("get_wallet_token_sizes")

    def test_fingerprint_tracks_market_kind(self):
        import config
        assert "TRADE_HIGH_MARKETS" in config._FINGERPRINT_KEYS
        assert "TRADE_LOW_MARKETS" in config._FINGERPRINT_KEYS
