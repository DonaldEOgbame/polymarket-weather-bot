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

    def test_yes_bet_on_narrow_bucket_is_satisfiable(self):
        """A real bucket here (bounded, exact/range) is 0.8-2.8°F padded-wide, well
        under 2*FORECAST_MARGIN_F (5°F at the default 2.5°F margin). An unguarded
        [lo+margin, hi-margin] window would be empty for every such bucket, making
        a YES bet structurally impossible to ever pass no matter how centered the
        forecast is — not a strict gate, a silent lockout.

        Capping the effective margin at exactly half the padded width "fixes" that
        but collapses the passing window to the bucket's exact midpoint — a single
        float value real means will essentially never land on, trading an always-
        fails bug for an almost-always-fails one. Capping at a FRACTION of the half-
        width (YES_MARGIN_WIDTH_FRACTION, default 0.6) instead leaves a real,
        non-degenerate window, still tightest at the center."""
        from strategy import forecast_margin_ok
        # bucket 80-82 (padded 79.5-82.5, half_width=1.5). At the default 0.6
        # fraction, effective_margin=0.9, so the passing window is [80.4, 81.6] —
        # a real 1.2°F-wide range, not a single point.
        assert forecast_margin_ok("YES", 81.0, 80.0, 82.0, 2.5) is True   # dead center
        assert forecast_margin_ok("YES", 80.4, 80.0, 82.0, 2.5) is True   # at window edge
        assert forecast_margin_ok("YES", 80.3, 80.0, 82.0, 2.5) is False  # just outside window
        assert forecast_margin_ok("YES", 79.6, 80.0, 82.0, 2.5) is False  # near padded edge


class TestForecastDirectionAgrees:
    """Hard rule, independent of edge size: a trade must never bet against what
    the RAW models (pre resolution-source correction) themselves predict — only
    exploit mispricing on the side the models already favor. Applies to both
    open-ended (above/below) and bounded buckets.

    Originated from a real trade 2026-07-10: Helsinki "above 29C", raw models
    averaging ~81F (predicting NO), but the METAR-corrected calibrated
    probability still cleared edge on YES because the market priced NO even
    more confidently than the raw forecast justified — a bet against the
    models' own directional call, riding entirely on a global (not
    city-verified) correction in the distribution's thin tail. The user
    subsequently asked for the same discipline on bounded buckets too, not
    just open-ended ones."""

    def test_helsinki_yes_bet_against_raw_models_is_blocked(self):
        from strategy import forecast_direction_agrees
        # Real data from the 2026-07-10 Helsinki trade: weighted mean ~81F,
        # threshold 83.8F ("above 29C"). Models predict NOT crossing it.
        assert forecast_direction_agrees("YES", 81.0, 83.8, None) is False

    def test_helsinki_no_bet_agrees_with_raw_models(self):
        from strategy import forecast_direction_agrees
        assert forecast_direction_agrees("NO", 81.0, 83.8, None) is True

    def test_yes_bet_above_bucket_agrees_when_models_predict_crossing(self):
        from strategy import forecast_direction_agrees
        assert forecast_direction_agrees("YES", 90.0, 85.0, None) is True

    def test_below_bucket_direction(self):
        from strategy import forecast_direction_agrees
        # "below 45" market: YES means temp lands below X. Models predict ~40, below 45.
        assert forecast_direction_agrees("YES", 40.0, None, 45.0) is True
        assert forecast_direction_agrees("NO", 40.0, None, 45.0) is False

    def test_bounded_bucket_no_bet_blocked_when_models_predict_landing_inside(self):
        from strategy import forecast_direction_agrees
        # bucket 80-82 (padded 79.5-82.5), weighted mean 81 — squarely inside.
        # A NO bet (temp will MISS the bucket) contradicts that — must be blocked.
        assert forecast_direction_agrees("NO", 81.0, 80.0, 82.0) is False
        assert forecast_direction_agrees("YES", 81.0, 80.0, 82.0) is True

    def test_bounded_bucket_yes_bet_blocked_when_models_predict_missing(self):
        from strategy import forecast_direction_agrees
        # bucket 80-82, weighted mean 90 — well outside. A YES bet contradicts
        # the models (this mirrors every real historical NO trade's setup, just
        # checking the opposite side is correctly rejected).
        assert forecast_direction_agrees("YES", 90.0, 80.0, 82.0) is False
        assert forecast_direction_agrees("NO", 90.0, 80.0, 82.0) is True

    def test_missing_raw_weighted_mean_fails_open(self):
        from strategy import forecast_direction_agrees
        assert forecast_direction_agrees("YES", None, 85.0, None) is True


class TestYesEntriesDisabled:
    """YES entries are hard-disabled by request: every real winning trade to date
    has been NO, and both live YES signals the bot ever generated were judged bad
    bets after the fact. No config flag — YES must never produce a BUY or EXPLORE
    signal regardless of edge, agreement, or margin."""

    def _run(self, monkeypatch, yes_price, no_price, mean, bucket_low, bucket_high):
        import strategy
        from types import SimpleNamespace

        opp = SimpleNamespace(
            city="TestCity", date="2026-07-15", is_high=True, hours_to_resolution=48.0,
            bucket_low=bucket_low, bucket_high=bucket_high, yes_price=yes_price, no_price=no_price,
            token_id_yes="y", token_id_no="n", market_id="m1",
        )
        engine_res = {
            "ensemble_mean": mean, "ensemble_std": 1.0, "model_agreement": 1.0,
            "model_spread": 1.0,
            "raw_models": {"ecmwf_ifs025": mean, "icon_global": mean, "gfs_global": mean, "gem_global": mean},
            "raw_weighted_mean": mean, "model_count": 4,
        }
        portfolio_state = {"available_cash": 100.0, "total_equity": 100.0, "locked_cash": 0.0}
        # Readable book with a tight spread: the spread gate now fails CLOSED when
        # the book is unreadable, so tests must present a real book to trade.
        monkeypatch.setattr(strategy, "get_realtime_price", lambda tid: (0.51, 0.49))
        monkeypatch.setattr(strategy, "get_orderbook_depth_usd", lambda tid: (None, None))
        monkeypatch.setattr(strategy, "execute_query", lambda *a, **k: None)
        return strategy.evaluate_opportunity(opp, portfolio_state, engine_res=engine_res)

    def test_huge_yes_edge_never_trades(self, monkeypatch):
        # Mean sits dead center in the bucket and YES is priced at 10c — a YES edge
        # this large (~0.89) would have fired BUY_YES before the gate was added.
        result = self._run(monkeypatch, yes_price=0.10, no_price=0.90,
                            mean=90.0, bucket_low=88.0, bucket_high=92.0)
        assert result is None

    def test_no_side_still_trades_normally(self, monkeypatch):
        # Sanity check the YES gate didn't collaterally break NO entries.
        result = self._run(monkeypatch, yes_price=0.90, no_price=0.10,
                            mean=70.0, bucket_low=88.0, bucket_high=92.0)
        assert result is not None
        assert result["signal"] == "BUY_NO"
        assert result["side"] == "NO"


class TestOrderbookDepthLogging:
    """Order-book $ depth (ask_depth_usd/bid_depth_usd) is only fetched and logged
    when a trade actually fires, not on every skip — the live book can't be
    reconstructed after the fact once a market moves on or resolves, so this is
    the only chance to capture "how big a position could this have absorbed"."""

    def _run(self, monkeypatch, no_price, mean, bucket_low, bucket_high, depth_return=(150.0, 300.0)):
        import strategy
        from types import SimpleNamespace

        opp = SimpleNamespace(
            city="TestCity", date="2026-07-15", is_high=True, hours_to_resolution=48.0,
            bucket_low=bucket_low, bucket_high=bucket_high, yes_price=1.0 - no_price, no_price=no_price,
            token_id_yes="y", token_id_no="n", market_id="m1",
        )
        engine_res = {
            "ensemble_mean": mean, "ensemble_std": 1.0, "model_agreement": 1.0,
            "model_spread": 1.0,
            "raw_models": {"ecmwf_ifs025": mean, "icon_global": mean, "gfs_global": mean, "gem_global": mean},
            "raw_weighted_mean": mean, "model_count": 4,
        }
        portfolio_state = {"available_cash": 100.0, "total_equity": 100.0, "locked_cash": 0.0}
        depth_calls = []
        logged_rows = []

        def fake_depth(tid):
            depth_calls.append(tid)
            return depth_return

        def fake_execute_query(sql, params=()):
            if "INSERT INTO signals" in sql:
                logged_rows.append(params)

        # Readable book with a tight spread: the spread gate now fails CLOSED when
        # the book is unreadable, so tests must present a real book to trade.
        monkeypatch.setattr(strategy, "get_realtime_price", lambda tid: (0.51, 0.49))
        monkeypatch.setattr(strategy, "get_orderbook_depth_usd", fake_depth)
        monkeypatch.setattr(strategy, "execute_query", fake_execute_query)
        result = strategy.evaluate_opportunity(opp, portfolio_state, engine_res=engine_res)
        return result, depth_calls, logged_rows

    def test_depth_fetched_and_logged_when_trade_fires(self, monkeypatch):
        # mean=70 sits well outside the [88,92] bucket — a valid NO bet (miss).
        result, depth_calls, logged_rows = self._run(
            monkeypatch, no_price=0.70, mean=70.0, bucket_low=88.0, bucket_high=92.0,
            depth_return=(150.0, 300.0),
        )
        assert result is not None
        assert result["signal"] == "BUY_NO"
        assert depth_calls == ["n"]  # token_id_no — the side actually traded
        # ask_depth_usd, bid_depth_usd are the last two bound params of the INSERT
        assert logged_rows[0][-2:] == (150.0, 300.0)

    def test_depth_not_fetched_on_skip(self, monkeypatch):
        # Edge too small to trade at all — no signal fires, so no depth call
        # should be made (would be a wasted CLOB request on every skip otherwise).
        result, depth_calls, logged_rows = self._run(
            monkeypatch, no_price=0.99, mean=70.0, bucket_low=88.0, bucket_high=92.0,
        )
        assert result is None
        assert depth_calls == []
        assert logged_rows[0][-2:] == (None, None)


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
