"""Tests for scanner.parse_bucket — the most fragile parsing logic in the system."""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scanner import parse_bucket


class TestParseBucketAbove:
    """Tests for 'above X' / 'exceed X' / 'or more' patterns."""

    def test_above_basic(self):
        lb, ub = parse_bucket("Will NYC high be above 85°F?")
        assert lb == 85.0
        assert ub is None

    def test_exceed_basic(self):
        lb, ub = parse_bucket("Will the high in Chicago exceed 90°F on June 5?")
        assert lb == 90.0
        assert ub is None

    def test_or_more(self):
        lb, ub = parse_bucket("Will Dallas high be 95°F or more?")
        assert lb == 95.0
        assert ub is None

    def test_at_least(self):
        lb, ub = parse_bucket("Will the temperature be at least 75°F?")
        assert lb == 75.0
        assert ub is None

    def test_or_higher(self):
        lb, ub = parse_bucket("Will Miami high be 88°F or higher on May 20?")
        assert lb == 88.0
        assert ub is None

    def test_above_does_not_capture_date(self):
        """The parser must NOT pick up '15' from 'May 15'."""
        lb, ub = parse_bucket("Will NYC high on May 15 be above 85°F?")
        assert lb == 85.0
        assert ub is None


class TestParseBucketBelow:
    """Tests for 'below X' / 'under X' patterns."""

    def test_below_basic(self):
        lb, ub = parse_bucket("Will London low be below 40°F?")
        assert lb is None
        assert ub == 40.0

    def test_under(self):
        lb, ub = parse_bucket("Will the temperature be under 32°F?")
        assert lb is None
        assert ub == 32.0


class TestParseBucketRange:
    """Tests for range patterns like '80-85°F' or '80 to 85°F'."""

    def test_range_hyphen(self):
        lb, ub = parse_bucket("Will NYC high be between 80-85°F?")
        assert lb == 80.0
        assert ub == 85.0

    def test_range_to(self):
        lb, ub = parse_bucket("Will the high be 70 to 75°F?")
        assert lb == 70.0
        assert ub == 75.0


class TestParseBucketExact:
    """Tests for exact value patterns."""

    def test_exact_value(self):
        lb, ub = parse_bucket("Will it be exactly 72°F?")
        assert lb == 72.0
        assert ub == 72.0


class TestParseBucketEdgeCases:
    """Edge cases and failure modes."""

    def test_no_temperature(self):
        lb, ub = parse_bucket("Will it rain tomorrow?")
        assert lb is None
        assert ub is None

    def test_empty_string(self):
        lb, ub = parse_bucket("")
        assert lb is None
        assert ub is None

    def test_negative_temp(self):
        """Negative temperatures (e.g. Chicago winter)."""
        lb, ub = parse_bucket("Will Chicago low be below -5°F?")
        assert lb is None
        assert ub == -5.0

    def test_celsius_converted_to_fahrenheit(self):
        """°C markets (international cities) are converted to °F for the strategy engine."""
        lb, ub = parse_bucket("Will London high be above 30°C?")
        # 30°C with Celsius rounding resolves to >= 29.5°C (85.1°F).
        # We adjust the input to get_bucket_probability by adding 0.5 to balance the 0.5 subtraction.
        # So lb = 85.1 + 0.5 = 85.6
        assert lb == pytest.approx(85.6)
        assert ub is None
