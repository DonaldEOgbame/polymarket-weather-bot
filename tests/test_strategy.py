"""Tests for strategy.py — Kelly sizing, edge calculation, and decision logic."""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy import calculate_kelly


class TestCalculateKelly:
    """Kelly criterion formula tests."""

    def test_positive_edge(self):
        """With 10% edge at 50c price, Kelly should be positive."""
        f = calculate_kelly(edge=0.10, price=0.50)
        # f = 0.10 / (1 - 0.50) = 0.20, capped at 0.08
        assert f == 0.08

    def test_small_edge(self):
        """Small edge should produce small Kelly fraction."""
        f = calculate_kelly(edge=0.02, price=0.50)
        # f = 0.02 / 0.50 = 0.04
        assert abs(f - 0.04) < 0.001

    def test_zero_edge(self):
        f = calculate_kelly(edge=0.0, price=0.50)
        assert f == 0.0

    def test_negative_edge(self):
        f = calculate_kelly(edge=-0.05, price=0.50)
        assert f == 0.0

    def test_price_at_zero(self):
        f = calculate_kelly(edge=0.10, price=0.0)
        assert f == 0.0

    def test_price_at_one(self):
        f = calculate_kelly(edge=0.10, price=1.0)
        assert f == 0.0

    def test_cap_at_kelly_max(self):
        """Large edge should be capped at KELLY_CAP."""
        f = calculate_kelly(edge=0.50, price=0.30)
        # f = 0.50 / 0.70 = 0.714, should be capped at 0.08
        assert f == 0.08

    def test_high_price_low_edge(self):
        """Edge at high price (near 1.0) should produce appropriately scaled fraction."""
        f = calculate_kelly(edge=0.05, price=0.90)
        # f = 0.05 / 0.10 = 0.50, capped at 0.08
        assert f == 0.08

    def test_low_price_moderate_edge(self):
        """Moderate edge at low price."""
        f = calculate_kelly(edge=0.03, price=0.20)
        # f = 0.03 / 0.80 = 0.0375
        assert abs(f - 0.0375) < 0.001


class TestEdgeCalculation:
    """Verify that edge is computed correctly from model prob and market price."""

    def test_yes_edge(self):
        model_prob = 0.65
        yes_price = 0.50
        yes_edge = model_prob - yes_price
        assert abs(yes_edge - 0.15) < 0.001

    def test_no_edge(self):
        model_prob = 0.30
        no_price = 0.60
        no_edge = (1.0 - model_prob) - no_price
        assert abs(no_edge - 0.10) < 0.001

    def test_no_edge_negative(self):
        """When model agrees with market, no edge."""
        model_prob = 0.50
        yes_price = 0.50
        yes_edge = model_prob - yes_price
        assert abs(yes_edge) < 0.001

    def test_complementary_edges(self):
        """YES edge + NO edge + yes_price + no_price should sum consistently."""
        model_prob = 0.65
        yes_price = 0.55
        no_price = 0.48
        yes_edge = model_prob - yes_price         # 0.10
        no_edge = (1.0 - model_prob) - no_price   # -0.13
        # These don't need to sum to anything specific because yes+no prices
        # don't always sum to 1.0 (spread exists), but the formulas should be consistent
        assert abs(yes_edge - 0.10) < 0.001
        assert abs(no_edge - (-0.13)) < 0.001


class TestForecastMarginGate:
    """The margin gate ('stop cutting it close'): only bet when the ensemble mean is
    a safe distance from the bucket boundary in the direction the bet needs."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_no_bet_needs_mean_clear_outside_bucket(self):
        from strategy import forecast_margin_ok
        # bucket 80-80 (padded 79.5-80.5). NO bet, margin 2.5.
        assert forecast_margin_ok("NO", 76.0, 80.0, 80.0, 2.5) is True   # 76 << 79.5-2.5
        assert forecast_margin_ok("NO", 79.0, 80.0, 80.0, 2.5) is False  # only 0.5 clear
        assert forecast_margin_ok("NO", 83.5, 80.0, 80.0, 2.5) is True   # 83.5 >> 80.5+2.5

    def test_yes_bet_needs_mean_clear_inside_bucket(self):
        from strategy import forecast_margin_ok
        # wide bucket 70-90 (padded 69.5-90.5), YES bet, margin 2.5
        assert forecast_margin_ok("YES", 80.0, 70.0, 90.0, 2.5) is True   # centre, clear
        assert forecast_margin_ok("YES", 71.0, 70.0, 90.0, 2.5) is False  # within 2.5 of low edge

    def test_open_ended_and_zero_margin_always_pass(self):
        from strategy import forecast_margin_ok
        assert forecast_margin_ok("NO", 80.0, None, 85.0, 2.5) is True  # open-ended
        assert forecast_margin_ok("NO", 80.0, 79.9, 80.1, 0.0) is True  # margin disabled


class TestForecastDirectionAgrees:
    """Gate: on an open-ended (above/below) bucket, the bet's side must agree with
    what the RAW models (pre resolution-source correction) actually predict.
    forecast_margin_ok is a no-op on open-ended buckets (nothing to be "inside" of),
    which let a real trade fire 2026-07-10: Helsinki "above 29C", raw models
    averaging ~81F (predicting NO), but the METAR-corrected calibrated probability
    still cleared edge on YES because the market priced NO even more confidently
    than the raw forecast justified — a bet against the models' own directional
    call, riding entirely on a global (not city-verified) correction in the
    distribution's thin tail."""

    def test_helsinki_yes_bet_against_raw_models_is_blocked(self):
        from strategy import forecast_direction_agrees
        # Real data from the 2026-07-10 Helsinki trade: raw models ~80.5-81.2F,
        # threshold 83.8F ("above 29C"). Models predict NOT crossing it.
        raw_models = {"ecmwf_ifs025": 81.19, "icon_global": 81.23, "gfs_global": 80.9, "gem_global": 80.52}
        assert forecast_direction_agrees("YES", raw_models, 83.8, None) is False

    def test_helsinki_no_bet_agrees_with_raw_models(self):
        from strategy import forecast_direction_agrees
        raw_models = {"ecmwf_ifs025": 81.19, "icon_global": 81.23, "gfs_global": 80.9, "gem_global": 80.52}
        assert forecast_direction_agrees("NO", raw_models, 83.8, None) is True

    def test_yes_bet_above_bucket_agrees_when_models_predict_crossing(self):
        from strategy import forecast_direction_agrees
        raw_models = {"ecmwf_ifs025": 90.0, "icon_global": 89.5, "gfs_global": 91.0}
        assert forecast_direction_agrees("YES", raw_models, 85.0, None) is True

    def test_below_bucket_direction(self):
        from strategy import forecast_direction_agrees
        raw_models = {"ecmwf_ifs025": 40.0, "icon_global": 41.0, "gfs_global": 39.5}
        # "below 45" — models predict landing below it, so NO... wait this is a
        # "below X" market: YES means temp lands below X.
        assert forecast_direction_agrees("YES", raw_models, None, 45.0) is True
        assert forecast_direction_agrees("NO", raw_models, None, 45.0) is False

    def test_bounded_bucket_always_passes(self):
        from strategy import forecast_direction_agrees
        # forecast_margin_ok is the real gate for bounded buckets; this one is a no-op there.
        raw_models = {"ecmwf_ifs025": 90.0}
        assert forecast_direction_agrees("YES", raw_models, 80.0, 82.0) is True
        assert forecast_direction_agrees("NO", raw_models, 80.0, 82.0) is True

    def test_missing_raw_models_fails_open(self):
        from strategy import forecast_direction_agrees
        assert forecast_direction_agrees("YES", {}, 85.0, None) is True
        assert forecast_direction_agrees("YES", None, 85.0, None) is True


class TestParseTargetDate:
    """Date must come from the market's 'on <DATE>' resolution text, not the endDate
    timestamp whose UTC-close convention drifted and mis-dated far-offset stations."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

    def test_prefers_description_date(self):
        from scanner import parse_target_date
        from datetime import datetime, timezone
        # endDate is the NEXT day at 00:00Z (old convention) but the market is "on 1 Jul"
        end = datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc)
        desc = "...highest temperature recorded at the Wellington Intl Airport Station in degrees Celsius on 1 Jul '26."
        assert parse_target_date(desc, end) == "2026-07-01"

    def test_falls_back_to_enddate_utc(self):
        from scanner import parse_target_date
        from datetime import datetime, timezone
        end = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        assert parse_target_date("no date phrase here", end) == "2026-07-04"

    def test_full_month_name(self):
        from scanner import parse_target_date
        assert parse_target_date("... on 5 January '26.", None) == "2026-01-05"
