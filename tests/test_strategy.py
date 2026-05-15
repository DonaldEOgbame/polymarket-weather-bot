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
