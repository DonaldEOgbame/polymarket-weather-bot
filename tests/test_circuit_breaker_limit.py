"""The circuit breaker's dollar limit must stay tied to the stake.

config.py documents the relationship in prose, immediately above the constants:

    limit = -(effective_stake * DAILY_LOSS_STAKES)
    Default 4 stakes x the $2 default stake = -$8

Prose is not enforcement. The stake has since moved 2.0 -> 4.0 -> 6.0 through
the dashboard, which silently moved the breaker from -$8 to -$24 with nothing
asserting the two stayed coupled — and nothing asserting the documented example
still described the code.

These tests read the formula and the worked example OUT OF THE COMMENT and check
the functions against them, so the documentation cannot drift from the behaviour
without CI failing. Coupling the doc to the code is the point; a test that just
re-implemented the multiplication would pass forever while the comment rotted.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C

_CONFIG_SRC = open(os.path.join(os.path.dirname(__file__), "..", "config.py")).read()


class TestTheDocumentedFormulaIsTheImplementedOne:
    def test_the_formula_is_still_documented(self):
        """If the comment is deleted or reworded, the tests below lose their
        anchor — so its absence is itself a failure."""
        assert re.search(r"limit\s*=\s*-\(effective_stake\s*\*\s*DAILY_LOSS_STAKES\)",
                         _CONFIG_SRC), (
            "The daily-loss formula comment in config.py has changed shape. "
            "Update this test with it, deliberately."
        )

    def test_worked_example_in_the_comment_still_holds(self):
        """The comment claims '4 stakes x the $2 default stake = -$8'. Read the
        three numbers out of it and check the arithmetic against the real
        function, so a changed default cannot leave a lying example behind."""
        m = re.search(r"Default\s+([\d.]+)\s+stakes\s*[x×]\s*the\s*\$([\d.]+)\s*"
                      r"default stake\s*=\s*-\$([\d.]+)", _CONFIG_SRC)
        assert m, "the worked example in config.py no longer parses"
        stakes, stake, expected = float(m.group(1)), float(m.group(2)), float(m.group(3))
        assert stakes * stake == expected, "the comment's own arithmetic is wrong"

        before = (C.setting("FIXED_POSITION_SIZE"), C.setting("DAILY_LOSS_STAKES"))
        try:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": stake,
                                       "DAILY_LOSS_STAKES": stakes})
            assert C.daily_loss_limit() == pytest.approx(-expected)
        finally:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before[0],
                                       "DAILY_LOSS_STAKES": before[1]})

    def test_documented_defaults_match_the_actual_defaults(self):
        """The example describes 'the $2 default stake'. If the code default
        moves and the comment does not, every reader of config.py is misled
        about what the breaker will do on a fresh deploy."""
        m = re.search(r"the\s*\$([\d.]+)\s*default stake", _CONFIG_SRC)
        documented_stake = float(m.group(1))
        code_default = float(re.search(
            r'FIXED_POSITION_SIZE\s*=\s*float\(_tunable\("FIXED_POSITION_SIZE",\s*"([\d.]+)"\)\)',
            _CONFIG_SRC).group(1))
        assert documented_stake == code_default

        m2 = re.search(r"Default\s+([\d.]+)\s+stakes", _CONFIG_SRC)
        code_stakes = float(re.search(
            r'DAILY_LOSS_STAKES\s*=\s*float\(_tunable\("DAILY_LOSS_STAKES",\s*"([\d.]+)"\)\)',
            _CONFIG_SRC).group(1))
        assert float(m2.group(1)) == code_stakes


class TestTheLimitScalesWithTheStake:
    def test_raising_the_stake_raises_the_budget_proportionally(self):
        """A fixed dollar limit tuned for $2 stakes would halt a $6-stake day
        after barely one loss. This is the property that prevents that."""
        before = (C.setting("FIXED_POSITION_SIZE"), C.setting("DAILY_LOSS_STAKES"))
        try:
            C.apply_runtime_overrides({"DAILY_LOSS_STAKES": 4.0})
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 2.0})
            small = C.daily_loss_limit()
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 6.0})
            big = C.daily_loss_limit()
            assert small == pytest.approx(-8.0)
            assert big == pytest.approx(-24.0)
            assert big == pytest.approx(small * 3.0)
        finally:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before[0],
                                       "DAILY_LOSS_STAKES": before[1]})

    def test_the_limit_is_always_a_whole_number_of_stakes(self):
        """Expressed in stakes, never in dollars — so 'how many losing trades
        before the bot stops' has a stake-independent answer."""
        before = (C.setting("FIXED_POSITION_SIZE"), C.setting("DAILY_LOSS_STAKES"))
        try:
            for stake in (1.0, 2.0, 6.0, 12.5):
                C.apply_runtime_overrides({"FIXED_POSITION_SIZE": stake,
                                           "DAILY_LOSS_STAKES": 4.0})
                assert C.daily_loss_limit() / -stake == pytest.approx(4.0)
        finally:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before[0],
                                       "DAILY_LOSS_STAKES": before[1]})

    def test_the_limit_is_derived_at_check_time_not_frozen_at_import(self):
        """check_circuit_breaker() calls daily_loss_limit() every cycle for this
        reason. A value captured at import would keep enforcing the old stake's
        budget for the life of the process."""
        before = C.setting("FIXED_POSITION_SIZE")
        try:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 2.0})
            first = C.daily_loss_limit()
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": 6.0})
            assert C.daily_loss_limit() != first
        finally:
            C.apply_runtime_overrides({"FIXED_POSITION_SIZE": before})

    def test_the_breaker_reads_the_derived_limit(self):
        """Asserts the CALL SITE, not just the function: main.check_circuit_breaker
        must consult daily_loss_limit() rather than a constant."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
        body = src.split("def check_circuit_breaker")[1].split("\ndef ")[0]
        assert "daily_loss_limit()" in body
        assert "DAILY_LOSS_LIMIT" not in body, (
            "the breaker is reading a fixed dollar constant again"
        )
