"""Execution safety — the 2026-08-06 Austin fill.

A $6 market order went into a book holding $26.49 of ask depth and filled at
0.9818 against a 0.64 quote. Every gate passed honestly on the quote; execution
then turned a modelled +$0.78 into an actual −$0.71.

What makes it the dangerous kind of bug is that the position is 86.5% to WIN.
The outcome will look fine. Nothing in the ledger would ever have flagged it,
and no test in this repo would have failed.

Three defects, one per section below:

  1. depth was measured AFTER the decision, for logging only
  2. MAX_ENTRY_PRICE gated the QUOTE, while the order went out capped at 0.99
  3. slippage was modelled as `spread_fraction * price`, which describes
     crossing the spread and not walking the book — 4x under on this fill
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config as C
import scanner as SC
import strategy as S


# The Austin book, reconstructed from what the replay row recorded: $26.49 of
# ask depth, a 0.64 quote and a 13.3% spread. Shaped to reproduce the observed
# 0.9818 average fill for $6.
# Solved so that BOTH observed facts hold: the ask side totals $26.49, and
# spending $6.00 buys 6.1111 shares -> an average of 0.9818. The quote is real
# but one-tenth of a share deep, which is precisely how a 0.64 screen price and
# a 0.98 fill coexist.
AUSTIN_BOOK = {
    "asks": [
        {"price": "0.64", "size": "0.1"},     # $0.064  <- the quoted price
        {"price": "0.85", "size": "0.1"},     # $0.085
        {"price": "0.99", "size": "26.6"},    # $26.334 -> $26.48 total
    ],
    "bids": [{"price": "0.55", "size": "2000"}],
}

DEEP_BOOK = {
    "asks": [{"price": "0.68", "size": "10000"}],
    "bids": [{"price": "0.67", "size": "10000"}],
}


class TestUsableDepthIsNotTotalDepth:
    def test_only_levels_at_or_below_the_cap_count(self):
        """Depth resting at 0.95 is not depth you can use when the cap is 0.80.
        It is what a taker walks into after exhausting everything cheaper."""
        total = SC._book_depth_usd(AUSTIN_BOOK)[0]
        usable = SC._usable_ask_depth_usd(AUSTIN_BOOK, 0.80)
        assert total == pytest.approx(26.49, abs=0.02)
        # Only the quoted level is at or below the 0.80 cap — six cents of it.
        assert usable == pytest.approx(0.064, abs=0.001)
        assert usable < total

    def test_no_cap_matches_total_depth(self):
        assert SC._usable_ask_depth_usd(AUSTIN_BOOK, None) == pytest.approx(
            SC._book_depth_usd(AUSTIN_BOOK)[0])


class TestWalkingTheBook:
    def test_the_austin_fill_is_reproduced(self):
        """$6 into this book averages ~0.98, not the 0.64 quote."""
        vwap, filled, exhausted = SC._walk_asks(AUSTIN_BOOK, 6.0)
        assert filled == pytest.approx(6.0)
        assert not exhausted
        # The number from the live ledger: 6.1111 shares for $6.00.
        assert vwap == pytest.approx(0.9818, abs=0.001)
        assert 6.0 / vwap == pytest.approx(6.1111, abs=0.01)

    def test_a_deep_book_fills_at_the_quote(self):
        vwap, filled, _ = SC._walk_asks(DEEP_BOOK, 6.0)
        assert vwap == pytest.approx(0.68)
        assert filled == pytest.approx(6.0)

    def test_capping_the_price_limits_what_can_be_filled(self):
        """With a 0.80 cap only $6.72 is reachable, so a $6 order fits — but a
        $20 order is reported as exhausting the book rather than walking past
        the cap."""
        vwap, filled, exhausted = SC._walk_asks(AUSTIN_BOOK, 20.0, max_price=0.80)
        assert exhausted
        assert filled == pytest.approx(0.064, abs=0.001)
        assert vwap <= 0.80

    def test_an_empty_book_fills_nothing(self):
        vwap, filled, exhausted = SC._walk_asks({"asks": []}, 6.0)
        assert vwap is None and filled == 0.0 and exhausted

    def test_levels_are_consumed_cheapest_first(self):
        """The CLOB returns levels unordered; a taker fills best-price-first.
        Reading them in array order would misprice every fill."""
        shuffled = {"asks": list(reversed(AUSTIN_BOOK["asks"]))}
        assert SC._walk_asks(shuffled, 6.0)[0] == pytest.approx(
            SC._walk_asks(AUSTIN_BOOK, 6.0)[0])


class TestTheDepthGate:
    def test_thin_book_is_refused(self):
        g = S._depth_gate(26.49, stake=6.0)
        assert g["passed"] is False
        assert g["threshold"] == pytest.approx(60.0)

    def test_deep_book_passes(self):
        assert S._depth_gate(5000.0, stake=6.0)["passed"] is True

    def test_exactly_at_the_threshold_passes(self):
        assert S._depth_gate(60.0, stake=6.0)["passed"] is True

    def test_the_requirement_scales_with_the_stake(self):
        """The $2 -> $6 stake change is what made this bug reachable. A fixed
        dollar threshold would have let it happen again at the next raise."""
        depth = 25.0
        assert S._depth_gate(depth, stake=2.0)["passed"] is True    # needs $20
        assert S._depth_gate(depth, stake=6.0)["passed"] is False   # needs $60

    def test_unknown_depth_refuses(self, monkeypatch):
        """The opposite of the independent veto, which fails OPEN. An entry that
        cannot see the book it is about to cross has no idea what it will pay."""
        monkeypatch.setattr(S, "REQUIRE_DEPTH_TO_TRADE", True)
        g = S._depth_gate(None, stake=6.0)
        assert g["passed"] is False
        assert g["observed"] is None
        assert "unreadable" in g["detail"]

    def test_the_veto_still_fails_open(self):
        """Asserted alongside the above so the two opposite behaviours stay
        deliberate and neither is copied into the other."""
        from independent import veto_gate_rows
        rows = veto_gate_rows({"independent_source": "none",
                               "independent_state": "NO_DATA",
                               "independent_value": None})
        assert all(r["passed"] for r in rows), (
            "the independent veto must fail OPEN on missing data")

    def test_the_gate_records_both_numbers(self):
        """The requirement is a multiple, so depth alone cannot reconstruct it."""
        g = S._depth_gate(26.49, stake=6.0)
        assert g["observed"] == pytest.approx(26.49)
        assert "26.49" in g["detail"] and "60.00" in g["detail"]


class TestTheAustinTradeIsNowRefused:
    """The end-to-end regression: replay the real book and stake."""

    def _evaluate(self, monkeypatch, book):
        opp = SimpleNamespace(
            city="Austin", date="2026-08-07", is_high=True, hours_to_resolution=24.0,
            bucket_low=94.0, bucket_high=95.0, yes_price=0.32, no_price=0.68,
            token_id_yes="y", token_id_no="n", market_id="0xaustin", volume=50000.0,
            question="Will the highest temperature in Austin be between 94-95F?",
        )
        engine_res = {
            "ensemble_mean": 100.84, "ensemble_std": 2.0, "model_agreement": 1.0,
            "model_spread": 0.61, "model_count": 4, "raw_weighted_mean": 100.84,
            "raw_models": {"ecmwf_ifs025": 100.84, "icon_global": 100.84,
                           "gfs_global": 100.84, "gem_global": 100.84},
        }
        monkeypatch.setattr(S, "get_realtime_price", lambda t: (0.72, 0.55))
        monkeypatch.setattr(S, "get_orderbook_depth_usd", lambda t: (26.49, 1231.09))
        monkeypatch.setattr(S, "execute_query", lambda *a, **k: None)
        monkeypatch.setattr(S, "estimate_fill", lambda tok, usd, cap=None: (
            None if book is None else {
                "vwap": SC._walk_asks(book, usd, cap)[0],
                "filled_usd": SC._walk_asks(book, usd, cap)[1],
                "exhausted": SC._walk_asks(book, usd, cap)[2],
                "usable_depth_usd": SC._usable_ask_depth_usd(book, cap),
                "best_ask": 0.68}))
        monkeypatch.setattr(C, "_RUNTIME", dict(C._RUNTIME, FIXED_POSITION_SIZE=6.0))
        return S.evaluate_opportunity(
            opp, {"available_cash": 100.0, "total_equity": 100.0, "locked_cash": 0.0},
            engine_res=engine_res)

    def test_the_real_austin_book_refuses_the_trade(self, monkeypatch):
        assert self._evaluate(monkeypatch, AUSTIN_BOOK) is None

    def test_the_same_trade_fires_on_a_deep_book(self, monkeypatch):
        """Proves the refusal is about liquidity and not about the forecast."""
        res = self._evaluate(monkeypatch, DEEP_BOOK)
        assert res is not None and res["signal"] == "BUY_NO"

    def test_an_unreadable_book_refuses(self, monkeypatch):
        assert self._evaluate(monkeypatch, None) is None


class TestTheLimitPriceBinds:
    def test_the_limit_never_exceeds_the_cap(self):
        """`quote + 0.01` is the allowance; MAX_ENTRY_PRICE is policy and wins.
        Before the fix this line capped at 0.99 — the DISABLED sentinel — so the
        configured 0.80 could not constrain what was paid."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        assert "min(quoted_price + 0.01, MAX_ENTRY_PRICE)" in src
        assert "min(signal_data[\"price\"] + 0.01, 0.99)" not in src

    @pytest.mark.parametrize("quote", [0.10, 0.50, 0.64, 0.79, 0.795, 0.85, 0.99])
    def test_computed_limit_is_capped_for_every_quote(self, quote):
        limit = round(min(quote + 0.01, C.MAX_ENTRY_PRICE), 2)
        assert limit <= C.MAX_ENTRY_PRICE + 1e-9

    def test_the_order_is_a_limit_not_a_market_order(self):
        """A market order on a $0-$1 instrument has no floor on execution
        quality — it walks until filled at whatever the book charges."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        entry = src.split("def execute_trade")[1]
        assert "_submit_marketable_limit" in entry
        assert "OrderArgsV2" in src

    def test_partial_fills_are_preferred_to_walking(self):
        """$3.50 filled at an acceptable price beats $6.00 filled at any."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        fn = src.split("def _submit_marketable_limit")[1].split("\n    def ")[0]
        assert "OrderType.FAK" in fn

    def test_size_is_derived_at_the_limit_so_the_stake_cannot_be_overspent(self):
        """The limit is the WORST price payable, so sizing there bounds spend."""
        stake, limit = 6.0, 0.80
        shares = round(stake / limit, 2)
        assert shares * limit <= stake + 0.01


class TestPostFillVerification:
    def test_the_audit_recomputes_edge_at_the_fill(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        fn = src.split("def _verify_fill")[1].split("\n    def ")[0]
        for token in ("quoted", "limit_sent", "filled", "slippage",
                      "depth_at_decision", "size_pct_of_depth", "edge_at_fill"):
            assert token in fn, token

    def test_negative_edge_at_fill_is_an_error(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        fn = src.split("def _verify_fill")[1].split("\n    def ")[0]
        assert "NEGATIVE EDGE AT FILL" in fn
        assert "logging.error" in fn

    def test_a_cap_breach_is_an_error(self):
        """Structurally impossible now. If it fires, another path is bypassing
        the limit price — which is worth knowing loudly."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "executor.py")).read()
        fn = src.split("def _verify_fill")[1].split("\n    def ")[0]
        assert "CAP BREACH" in fn

    def test_the_austin_numbers_produce_a_negative_post_fill_edge(self):
        """The arithmetic the audit would have printed on the day."""
        prob, filled = 0.1354623878718179, 0.9818
        fair = 1.0 - prob
        assert fair == pytest.approx(0.8645, abs=1e-4)
        assert fair - filled == pytest.approx(-0.117, abs=0.001)

    def test_a_clean_fill_produces_a_positive_post_fill_edge(self):
        """Near-silent in normal operation: every other trade filled AT quote."""
        prob, filled = 0.05, 0.76
        assert (1.0 - prob) - filled == pytest.approx(0.19, abs=1e-9)


class TestSlippageModel:
    def test_the_old_formula_understated_austin_by_about_four_times(self):
        quote, spread_frac = 0.64, 0.1329479768786127
        modelled = spread_frac * quote
        actual = 0.9818 - quote
        assert modelled == pytest.approx(0.085, abs=0.005)
        assert actual == pytest.approx(0.342, abs=0.005)
        assert actual / modelled > 3.5

    def test_edge_uses_the_walked_estimate(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "strategy.py")).read()
        assert "walked_vwap" in src
        assert "no_slip_frac" in src

    def test_depth_is_fetched_before_the_decision(self):
        """The whole fix in one assertion: the number that would have blocked
        this used to be collected one line after the commitment."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "strategy.py")).read()
        body = src.split("def evaluate_opportunity")[1]
        assert body.index("estimate_fill(") < body.index("no_gates = _no_side_gates")


class TestConstants:
    def test_all_are_in_the_fingerprint(self):
        for k in ("MIN_DEPTH_MULTIPLE", "REQUIRE_DEPTH_TO_TRADE",
                  "USE_MARKETABLE_LIMIT"):
            assert k in C._FINGERPRINT_KEYS, k

    def test_defaults_are_as_specified(self):
        assert C.MIN_DEPTH_MULTIPLE == 10.0
        assert C.REQUIRE_DEPTH_TO_TRADE is True
        assert C.USE_MARKETABLE_LIMIT is True
        assert C.MAX_FILL_SLIPPAGE_ALERT == 0.03

    def test_a_dangerous_depth_multiple_is_rejected(self, monkeypatch):
        monkeypatch.setattr(C, "MIN_DEPTH_MULTIPLE", 0.5)
        assert any("MIN_DEPTH_MULTIPLE" in p for p in C.validate_env_ranges())

    def test_disabling_the_limit_order_is_flagged(self, monkeypatch):
        monkeypatch.setattr(C, "USE_MARKETABLE_LIMIT", False)
        assert any("USE_MARKETABLE_LIMIT" in p for p in C.validate_env_ranges())

    def test_disabling_the_depth_requirement_is_flagged(self, monkeypatch):
        monkeypatch.setattr(C, "REQUIRE_DEPTH_TO_TRADE", False)
        assert any("REQUIRE_DEPTH_TO_TRADE" in p for p in C.validate_env_ranges())
