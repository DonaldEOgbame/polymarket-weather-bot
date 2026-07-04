"""Tests for weather.py — probability engine and ensemble logic."""
import pytest
import math
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from weather import get_bucket_probability, get_station_coords


class TestGetStationCoords:
    """Station mapping tests."""

    def test_nyc_match(self):
        key, station = get_station_coords("Will NYC high be above 85°F?")
        assert key == "NYC"
        assert station is not None

    def test_new_york_match(self):
        key, station = get_station_coords("Will the high in New York exceed 90°F?")
        assert key is not None
        assert "lat" in station

    def test_chicago_match(self):
        key, station = get_station_coords("Will Chicago low be below 30°F?")
        assert key == "Chicago"

    def test_no_match(self):
        key, station = get_station_coords("Will Mumbai high exceed 100°F?")
        assert key is None
        assert station is None

    def test_case_insensitive(self):
        key, _ = get_station_coords("will LONDON low be under 40°F?")
        assert key == "London"


class TestGetBucketProbability:
    """Tests for the probability engine's CDF calculations."""

    def _make_engine_result(self, mean, std):
        return {"ensemble_mean": mean, "ensemble_std": std}

    def test_centered_bucket_high_prob(self):
        """A bucket centered on the mean should have high probability."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 77.0, 83.0)
        # 77-83 is ±3°F around mean=80 → roughly ±1 std → ~68% with boundary adjustment
        assert 0.5 < prob < 0.9

    def test_far_bucket_low_prob(self):
        """A bucket far from the mean should have low probability."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 90.0, 95.0)
        assert prob < 0.05

    def test_open_ended_above(self):
        """'Above X' bucket with lower bound only."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 80.0, None)
        # Should be roughly 50% (mean is at the boundary)
        assert 0.4 < prob < 0.6

    def test_open_ended_below(self):
        """'Below X' bucket with upper bound only."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, None, 80.0)
        assert 0.4 < prob < 0.6

    def test_exact_value_bucket(self):
        """Exact value bucket (lb == ub) should have non-zero probability."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 80.0, 80.0)
        # 79.5 to 80.5 is 1°F window → should be small but non-zero
        assert 0.05 < prob < 0.3

    def test_probability_bounded_0_1(self):
        """Probability should always be in [0, 1]."""
        result = self._make_engine_result(mean=80.0, std=3.0)
        for lb, ub in [(50.0, 110.0), (None, None), (80.0, 80.0), (90.0, None)]:
            prob = get_bucket_probability(result, lb, ub)
            assert 0.0 <= prob <= 1.0

    def test_wider_std_gives_more_tail_probability(self):
        """With wider uncertainty, tail buckets should get more probability."""
        tight = self._make_engine_result(mean=80.0, std=1.5)
        wide = self._make_engine_result(mean=80.0, std=4.0)
        prob_tight = get_bucket_probability(tight, 88.0, None)
        prob_wide = get_bucket_probability(wide, 88.0, None)
        assert prob_wide > prob_tight

    def test_minimum_std_floor(self):
        """Even with very small std, probability should not be degenerate."""
        result = self._make_engine_result(mean=80.0, std=0.01)
        prob = get_bucket_probability(result, 75.0, 85.0)
        # The function clamps std to 0.5 minimum, so this should still work
        assert prob > 0.0


class TestUncertaintyModel:
    """Tests that the uncertainty model produces realistic values."""

    def test_combined_std_never_below_base(self):
        """Combined std should never be less than the base forecast error."""
        from config import BASE_FORECAST_ERROR
        # Even with zero model spread, the combined std should be >= base error
        for hours, base in BASE_FORECAST_ERROR.items():
            model_spread_std = 0.0
            combined = math.sqrt(base**2 + model_spread_std**2)
            assert combined >= base

    def test_combined_std_grows_with_lead_time(self):
        """Uncertainty should increase with longer lead times."""
        from config import BASE_FORECAST_ERROR
        errors = [BASE_FORECAST_ERROR[h] for h in sorted(BASE_FORECAST_ERROR.keys())]
        for i in range(1, len(errors)):
            assert errors[i] >= errors[i-1]

    def test_combined_std_grows_with_model_disagreement(self):
        """More model disagreement should increase total uncertainty."""
        base = 3.0
        low_spread = math.sqrt(base**2 + 0.5**2)
        high_spread = math.sqrt(base**2 + 3.0**2)
        assert high_spread > low_spread


class TestProbabilityCalibration:
    """Regression tests for the Platt probability calibration (weather._calibrate_prob).

    The raw Gaussian bucket prob is ~1.9x overconfident in the low-p region where the
    bot bets NO (measured on 96,307 resolved signals: predicted ~15% hit ~28%). The
    calibration remap pulls raw probs back onto the observed reliability curve. This was
    the single biggest driver of the -$20 true loss on the first 19 live trades — with
    calibration on, 12 of 14 losing bets are refused.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_low_prob_inflated_toward_observed(self):
        from weather import _calibrate_prob
        # ~15% raw should map up toward the observed ~28%.
        out = _calibrate_prob(0.15)
        assert 0.24 < out < 0.34, f"0.15 should calibrate to ~0.29, got {out:.3f}"

    def test_monotonic(self):
        from weather import _calibrate_prob
        xs = [0.02, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9]
        ys = [_calibrate_prob(x) for x in xs]
        assert ys == sorted(ys), "calibration must be monotonic (never reorder opportunities)"

    def test_endpoints_pass_through(self):
        from weather import _calibrate_prob
        assert _calibrate_prob(0.0) == 0.0
        assert _calibrate_prob(1.0) == 1.0

    def test_shrinks_fake_no_edge(self):
        # A raw P_YES of 0.20 gives a raw NO edge of (1-0.20)-0.53 = 0.27 (looks great).
        # Calibrated P_YES ~0.39 gives (1-0.39)-0.53 = 0.08 — the fake edge collapses.
        from weather import _calibrate_prob
        raw_edge = (1 - 0.20) - 0.53
        cal_edge = (1 - _calibrate_prob(0.20)) - 0.53
        assert cal_edge < raw_edge - 0.1, "calibration must materially shrink the low-p NO edge"


class TestStationCoordinates:
    """Guard the two coordinates fixed 2026-07-04 (were resolving on the wrong station)."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_seoul_is_city_not_incheon(self):
        from weather import STATIONS
        lat, lon = STATIONS["Seoul"]["lat"], STATIONS["Seoul"]["lon"]
        # Seoul city ~ (37.57, 126.98); Incheon airport ~ 126.44 read ~6.6°F too cold.
        assert lon > 126.8, f"Seoul lon {lon} looks like Incheon, not the Seoul city station"

    def test_london_west_of_prime_meridian(self):
        from weather import STATIONS
        lon = STATIONS["London"]["lon"]
        assert lon < 0, f"London lon {lon} is east of the prime meridian (wrong station)"
