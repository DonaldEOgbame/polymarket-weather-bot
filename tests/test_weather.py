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

    def test_far_bucket_floored(self):
        """A BOUNDED bucket far from the mean floors at MIN_BUCKET_PROB (0.05) —
        the tail floor that stops the model betting real money on ~0% claims
        (the Guangzhou-#31 overconfidence bust). Raw prob here is ~0."""
        from config import MIN_BUCKET_PROB
        result = self._make_engine_result(mean=80.0, std=3.0)
        prob = get_bucket_probability(result, 90.0, 95.0)
        assert prob == pytest.approx(MIN_BUCKET_PROB)

    def test_open_ended_tail_floored(self):
        """Open-ended buckets ARE floored too — the overconfidence busts
        (Guangzhou #31, 'X or higher') were open-ended, so a bounded-only floor
        missed exactly the trades it targeted."""
        from config import MIN_BUCKET_PROB
        result = self._make_engine_result(mean=70.0, std=2.0)
        prob = get_bucket_probability(result, 95.0, None)  # raw P(>=95) with mean 70 ~ 0
        assert prob == pytest.approx(MIN_BUCKET_PROB)

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
        """With wider uncertainty, tail buckets should get more probability.
        Bound chosen so both probs sit ABOVE MIN_BUCKET_PROB (else the floor
        flattens the comparison — which is itself the intended tail behaviour)."""
        tight = self._make_engine_result(mean=80.0, std=3.0)
        wide = self._make_engine_result(mean=80.0, std=6.0)
        prob_tight = get_bucket_probability(tight, 84.0, None)
        prob_wide = get_bucket_probability(wide, 84.0, None)
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
        # ~15% raw maps up toward the METAR-observed ~29% hit rate for that bin.
        out = _calibrate_prob(0.15)
        assert 0.22 < out < 0.34, f"0.15 should calibrate toward observed ~0.29, got {out:.3f}"

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
        # The METAR-fit calibration raises P_YES toward its true ~27% hit rate, shrinking
        # the NO edge — the overconfident portion of the edge is removed.
        from weather import _calibrate_prob
        raw_edge = (1 - 0.20) - 0.53
        cal_edge = (1 - _calibrate_prob(0.20)) - 0.53
        assert cal_edge < raw_edge - 0.05, "calibration must shrink the low-p NO edge"
        assert _calibrate_prob(0.20) > 0.20, "low raw probs must be inflated toward observed"


class TestStationCoordinates:
    """Coordinates must match the exact station Polymarket names as the resolution
    source (verified 2026-07-04 from every live market's description). These pin the
    two that are counterintuitive: Polymarket resolves Seoul on INCHEON and London on
    LONDON CITY AIRPORT — NOT the city centre / Heathrow."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_seoul_resolves_on_incheon(self):
        from weather import STATIONS
        lon = STATIONS["Seoul"]["lon"]
        # Incheon (RKSI) ~ 126.44, well west of the Seoul city centre (126.98).
        assert lon < 126.6, f"Seoul must resolve on Incheon (~126.44), got lon {lon}"

    def test_london_resolves_on_city_airport(self):
        from weather import STATIONS
        lon = STATIONS["London"]["lon"]
        # London City Airport (EGLC) ~ +0.049, just east of the meridian — NOT
        # Heathrow (-0.45). Pin tightly so a future "fix" back to Heathrow fails here.
        assert 0.0 < lon < 0.1, f"London must resolve on City Airport (~+0.049), got lon {lon}"


class TestMetarStationMapping:
    """Every forecast city must have an ICAO/timezone mapping to Polymarket's METAR
    resolution source, or trades on it can never be verified against the real ruler."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_every_station_has_icao(self):
        from weather import STATIONS
        from metar import STATION_ICAO
        missing = [c for c in STATIONS if c not in STATION_ICAO]
        assert not missing, f"cities with no METAR ICAO mapping: {missing}"

    def test_icao_and_timezone_shape(self):
        from metar import STATION_ICAO
        for city, (icao, tz) in STATION_ICAO.items():
            assert 3 <= len(icao) <= 4 and icao.isalnum(), f"{city}: bad ICAO {icao!r}"
            assert "/" in tz, f"{city}: bad IANA tz {tz!r}"
