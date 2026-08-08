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
        assert cfg.NARROW_BUCKET_EDGE_THRESHOLD == 0.12

    def test_narrow_gate_is_never_looser_than_the_general_gate(self, cfg):
        """The narrow-bucket gate exists to be STRICTER. Below EDGE_THRESHOLD it
        would make thin buckets the EASIEST markets to enter — the inverse of
        the guard. At 0.12 vs 0.08 the margin is thin by design; below it, the
        regime is not merely abolished but reversed."""
        assert cfg.NARROW_BUCKET_EDGE_THRESHOLD >= cfg.EDGE_THRESHOLD

    def test_inversion_is_caught_by_the_boot_time_guard(self, tmp_path, monkeypatch):
        """A test only fails in CI. validate_env_ranges() is what fails on a Fly
        secret, which is how a stale value actually reaches production."""
        for name in _SHADOWING:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
        monkeypatch.setenv("NARROW_BUCKET_EDGE_THRESHOLD", "0.05")
        import config
        importlib.reload(config)
        try:
            problems = config.validate_env_ranges()
            assert any("NARROW_BUCKET_EDGE_THRESHOLD" in p for p in problems)
        finally:
            monkeypatch.delenv("NARROW_BUCKET_EDGE_THRESHOLD", raising=False)
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
        """0.85, decided by the owner on 2026-08-06."""
        assert cfg.MAX_ENTRY_PRICE == 0.85

    def test_min_model_confidence_entry_filter(self, cfg):
        assert cfg.MIN_MODEL_CONFIDENCE == 0.60

    def test_max_model_confidence_entry_filter(self, cfg):
        assert cfg.MAX_MODEL_CONFIDENCE == 0.85

    def test_min_entry_price_entry_filter(self, cfg):
        """0.65 (2026-08-06 owner decision, reaffirmed 2026-08-08 after a
        brief same-day move to 0.62): armed re-entry, not a lower floor, is
        how qualified-but-cheap markets get in — the bot waits for the price
        to reach the floor."""
        assert cfg.MIN_ENTRY_PRICE == 0.65

    def test_max_hours_to_resolution_entry_filter(self, cfg):
        assert cfg.MAX_HOURS_TO_RESOLUTION == 36.0

    def test_narrow_bucket_std_inflation_still_on(self, cfg):
        """Second corrector of the same defect. Removing it too would be the
        third correction, and the Platt constants were fitted through it."""
        assert cfg.NARROW_BUCKET_STD_INFLATION == 1.4

    def test_paper_mode_default_unchanged(self, cfg):
        """More trade flow is not a deploy criterion."""
        assert cfg.PAPER_MODE is True
        assert cfg.paper_mode() is True

    def test_stop_loss_untouched(self, cfg):
        """Frozen until the monitor trail covers >=10 closed positions incl.
        >=2 losses. Re-tuning it now would be fitting to the same blind window
        that made every previous stop-loss replay unanswerable."""
        assert cfg.STOP_LOSS_PCT == 0.50
        assert cfg.ENABLE_STOP_LOSS is True

    def test_sigma_and_bias_constants_untouched(self, cfg):
        """The 2026-07-31 direction-split recalibration, unchanged."""
        assert cfg.SIGMA_SCALE_HIGH == 1.02
        assert cfg.SIGMA_SCALE_LOW == 0.80
        assert cfg.SIGMA_SPREAD_COEF == 2.78
        assert cfg.METAR_WARM_CORRECTION_F == 0.0
        assert cfg.PROB_CALIBRATION_INTERCEPT == 0.8000
        assert cfg.PROB_CALIBRATION_SLOPE == 0.7480
        assert cfg.MIN_BUCKET_PROB == 0.05
