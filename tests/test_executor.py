"""Regression tests for executor exit logic — specifically the phantom-fill guard.

Historical forensic finding (2026-07-04): five 'edge decayed' exits in the
deployed DB were each booked at a NO bid of ~0.999, a price never once observed
with real size (the maximum NO price across all 44,879 logged signals was 0.81).
They fired AFTER each market's target date had passed, on a resolving book whose
only resting quotes were extreme and shallow. The correct behavior is to hold
such positions for resolution settlement ($1/$0), not to book a market exit.
"""
import sys, os
import pytest
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# executor imports py_clob_client_v2 at module load; guard so tests run headless.
import types
for mod in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client_v2.client"].ClobClient = object
ct = sys.modules["py_clob_client_v2.clob_types"]
for n in ("MarketOrderArgsV2", "OrderArgsV2", "OrderType", "ApiCreds", "BalanceAllowanceParams", "AssetType"):
    if not hasattr(ct, n):
        setattr(ct, n, object)

from executor import Executor


class TestTargetDatePassedGuard:
    def _now(self):
        return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)

    def test_past_target_date_holds_for_resolution(self):
        # Target date strictly before today (UTC) → do not market-exit.
        assert Executor._target_date_passed("2026-06-29", self._now()) is True

    def test_today_target_date_still_tradeable(self):
        # Same UTC day as now → not yet resolved, exit path still allowed.
        assert Executor._target_date_passed("2026-06-30", self._now()) is False

    def test_future_target_date_tradeable(self):
        assert Executor._target_date_passed("2026-07-02", self._now()) is False

    def test_missing_target_date_is_safe(self):
        assert Executor._target_date_passed(None, self._now()) is False
        assert Executor._target_date_passed("", self._now()) is False


class TestThesisBrokenGate:
    """Edge decay must only sell when the thesis is broken, not when a NO bet is simply
    winning (price converged toward 1.0). Three live NO trades bailed at ~+$0.05 instead
    of holding to a ~$1.00 settlement because edge decay fired on a converging winner."""

    def _exec(self):
        # Build an Executor without running __init__ (which needs a CLOB client / DB).
        return Executor.__new__(Executor)

    def _patch_entry_prob(self, monkeypatch, prob):
        import executor as ex
        monkeypatch.setattr(ex, "fetch_query",
                            lambda *a, **k: [{"model_prob": prob}] if prob is not None else [])

    def test_no_bet_converged_winner_holds(self, monkeypatch):
        # Entry P(YES)=0.20 → our-side (NO) prob 0.80. Model still says P(YES)=0.20.
        # Price converged to 0.90 (winning), above entry 0.55. Thesis intact → HOLD.
        self._patch_entry_prob(monkeypatch, 0.20)
        e = self._exec()
        pos = {"market_id": "0x1", "side": "NO"}
        assert e._thesis_broken(pos, latest_prob=0.20, current_price=0.90, entry_price=0.55) is False

    def test_no_bet_forecast_turned_exits(self, monkeypatch):
        # Entry P(YES)=0.20 → NO prob 0.80. Forecast now says P(YES)=0.45 → NO prob 0.55,
        # a 0.25 drop (> 0.10 delta). Thesis broken → EXIT even though price ok.
        self._patch_entry_prob(monkeypatch, 0.20)
        e = self._exec()
        pos = {"market_id": "0x1", "side": "NO"}
        assert e._thesis_broken(pos, latest_prob=0.45, current_price=0.60, entry_price=0.55) is True

    def test_real_loss_exits_regardless(self, monkeypatch):
        # Price below entry = underwater. Exit regardless of forecast.
        self._patch_entry_prob(monkeypatch, 0.20)
        e = self._exec()
        pos = {"market_id": "0x1", "side": "NO"}
        assert e._thesis_broken(pos, latest_prob=0.20, current_price=0.40, entry_price=0.55) is True

    def test_missing_entry_prob_fails_safe_to_exit(self, monkeypatch):
        self._patch_entry_prob(monkeypatch, None)
        e = self._exec()
        pos = {"market_id": "0x1", "side": "NO"}
        assert e._thesis_broken(pos, latest_prob=0.20, current_price=0.90, entry_price=0.55) is True

    def test_yes_bet_converged_winner_holds(self, monkeypatch):
        # YES bet: entry P(YES)=0.70, still 0.70, price converged up to 0.90. Hold.
        self._patch_entry_prob(monkeypatch, 0.70)
        e = self._exec()
        pos = {"market_id": "0x1", "side": "YES"}
        assert e._thesis_broken(pos, latest_prob=0.70, current_price=0.90, entry_price=0.60) is False


class TestTakeProfitExit:
    def test_take_profit_triggers_exit(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        
        pos = {
            "id": 1,
            "market_id": "0x1",
            "token_id": "tok_1",
            "side": "NO",
            "entry_price": 0.55,
            "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00",
            "target_date": "2100-01-01"  # Target date in future relative to today
        }
        
        # Confirmed bid at 0.99 >= TAKE_PROFIT_PRICE (0.98): the fast take-profit
        # must fire immediately, even inside the 30-min hold window.
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.99, 0.99))

        exits_called = []
        monkeypatch.setattr(e, "_close_position",
                            lambda pos, pnl_dollars, exit_reason: exits_called.append(exit_reason))

        e._check_exit_for_position(pos)

        assert len(exits_called) == 1
        assert "Take Profit" in exits_called[0]

    def test_take_profit_needs_real_bid(self, monkeypatch):
        # Ask is high but the BID is 0 (unreadable/thin book) — must NOT fire the
        # fast take-profit off a non-fillable price (the phantom-exit guard).
        import executor as ex
        e = Executor.__new__(Executor)
        e._loss_streak = {}
        pos = {
            "id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
            "entry_price": 0.55, "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00", "target_date": "2100-01-01",
            "city": "Tokyo", "is_high": 0,
        }
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.99, 0.0))
        monkeypatch.setattr(ex, "get_gamma_mid_price", lambda *a: None)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        exits_called = []
        monkeypatch.setattr(e, "_close_position",
                            lambda pos, pnl_dollars, exit_reason: exits_called.append(exit_reason))
        e._check_exit_for_position(pos)
        assert exits_called == []


class TestSettleClosedTrade:
    """Early-exit (take-profit/stop) trades must still get a resolutions row so
    calibration sees their TRUE outcome, settled against the METAR actual — not
    the early-exit scalp price."""

    def _trade(self):
        return {
            "id": 1, "market_id": "0x1", "side": "NO", "city": "Tokyo",
            "target_date": "2026-07-13", "is_high": 0,
            "model_prob": 0.05, "size_usdc": 2.0, "fill_price": 0.68,
        }

    def test_writes_resolution_for_no_win(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        # Actual 77°F, bucket [69.4,70.2] → misses bucket → NO wins.
        monkeypatch.setattr(ex, "final_extreme_f", lambda *a: 77.0)
        monkeypatch.setattr(ex, "fetch_query", lambda sql, params=(): (
            [] if "FROM resolutions" in sql else [{"bucket_low": 69.4, "bucket_high": 70.2}]))
        inserted = {}
        monkeypatch.setattr(ex, "execute_query",
                            lambda sql, params=(): inserted.update({"sql": sql, "params": params}))
        assert e.settle_closed_trade(self._trade()) is True
        p = inserted["params"]
        # outcome NO, won=1, settled pnl positive (shares - size).
        assert "NO" in p and 1 in p
        assert inserted["params"][5] > 0  # pnl slot

    def test_idempotent_when_row_exists(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        monkeypatch.setattr(ex, "fetch_query", lambda sql, params=(): [{"1": 1}])
        wrote = []
        monkeypatch.setattr(ex, "execute_query", lambda *a, **k: wrote.append(1))
        assert e.settle_closed_trade(self._trade()) is False
        assert wrote == []

    def test_skips_when_metar_unpublished(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        monkeypatch.setattr(ex, "fetch_query", lambda sql, params=(): [])
        monkeypatch.setattr(ex, "final_extreme_f", lambda *a: None)
        wrote = []
        monkeypatch.setattr(ex, "execute_query", lambda *a, **k: wrote.append(1))
        assert e.settle_closed_trade(self._trade()) is False
        assert wrote == []


class TestSettleUnscoredTrades:
    """Backfill pass for closed trades with no resolution row.

    check_resolutions() alone cannot do this: trades.resolution_logged serves
    two jobs — "model accuracy recorded" and "done with this trade" — and is set
    as soon as the accuracy write succeeds. Anything that made settlement fail on
    that single pass is therefore permanent, including simply having closed
    before settle_closed_trade existed. That is why 16 of 32 take-profit trades
    in the 2026-07-31 export carry no resolution row at all.
    """

    def _rows(self):
        return [{"id": 1, "market_id": "0x1", "side": "NO", "is_high": 0,
                 "city": "Tokyo", "target_date": "2026-07-13",
                 "model_prob": 0.05, "size_usdc": 2.0, "fill_price": 0.68}]

    def test_settles_trades_the_flag_would_have_skipped(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        monkeypatch.setattr(ex, "fetch_query", lambda sql, params=(): self._rows())
        settled = []
        monkeypatch.setattr(Executor, "settle_closed_trade",
                            lambda self, t: settled.append(t["market_id"]) or True)
        assert e.settle_unscored_trades() == 1
        assert settled == ["0x1"]

    def test_query_ignores_resolution_logged(self, monkeypatch):
        """The whole point of the backfill — selecting on resolution_logged would
        reproduce the bug it exists to fix."""
        import executor as ex
        e = Executor.__new__(Executor)
        seen = {}
        def fq(sql, params=()):
            seen["sql"] = sql
            return []
        monkeypatch.setattr(ex, "fetch_query", fq)
        e.settle_unscored_trades()
        assert "resolution_logged" not in seen["sql"]
        assert "LEFT JOIN resolutions" in seen["sql"]
        assert "r.market_id IS NULL" in seen["sql"]

    def test_open_positions_are_excluded(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        seen = {}
        def fq(sql, params=()):
            seen["sql"] = sql
            return []
        monkeypatch.setattr(ex, "fetch_query", fq)
        e.settle_unscored_trades()
        assert "status != 'open'" in seen["sql"]

    def test_one_bad_trade_does_not_abort_the_batch(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        rows = [dict(self._rows()[0], market_id=f"0x{i}") for i in range(3)]
        monkeypatch.setattr(ex, "fetch_query", lambda sql, params=(): rows)
        def flaky(self, t):
            if t["market_id"] == "0x1":
                raise RuntimeError("boom")
            return True
        monkeypatch.setattr(Executor, "settle_closed_trade", flaky)
        assert e.settle_unscored_trades() == 2

    def test_query_failure_is_swallowed(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        def boom(sql, params=()):
            raise RuntimeError("db gone")
        monkeypatch.setattr(ex, "fetch_query", boom)
        assert e.settle_unscored_trades() == 0


class TestLiveExitFeeDeduction:
    """Live-mode exits recompute PnL from the actual fill price (not the paper
    mid estimate), but must still subtract the taker fee — omitting it silently
    overstates every live exit's realized PnL (stop-loss, take-profit, edge-decay)."""

    def test_live_exit_pnl_subtracts_fee(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)

        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(e, "_submit_taker",
                            lambda token_id, side, amount, fallback_price=None: {"shares": 10.0, "price": 0.70, "fee_bps": 500})
        monkeypatch.setattr(ex, "close_position_atomic", lambda **kwargs: kwargs)
        monkeypatch.setattr(ex, "send_trade_exit", lambda *a, **k: None)
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda tid: (None, None))
        monkeypatch.setattr(ex, "get_realtime_price", lambda tid: (0.71, 0.69))

        pos = {
            "id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
            "entry_price": 0.55, "size_usdc": 5.5,
            "entry_time": "2026-06-30T10:00:00+00:00", "question": "q",
        }

        captured = {}
        orig_close_position_atomic = ex.close_position_atomic
        def capture_close(**kwargs):
            captured.update(kwargs)
            return orig_close_position_atomic(**kwargs)
        monkeypatch.setattr(ex, "close_position_atomic", capture_close)

        e._close_position(pos, pnl_dollars=999.0, exit_reason="Stop Loss (-10.0%)")

        shares = pos["size_usdc"] / pos["entry_price"]
        # fee_bps is the ROUND-TRIP rate; the exchange charges half per leg
        # (verified against actual wallet USDC deltas, 16 live fills).
        expected_fee = (500 / 10000.0 / 2.0) * 0.70 * (1.0 - 0.70) * shares
        expected_pnl = (0.70 - 0.55) * shares - expected_fee

        assert captured["pnl_dollars"] == pytest.approx(expected_pnl)
        # Sanity: the naive no-fee calc would have been strictly larger (fee > 0).
        naive_pnl = (0.70 - 0.55) * shares
        assert captured["pnl_dollars"] < naive_pnl


class TestPartialExitFill:
    """A live FAK SELL that fills less than the full position must NOT book a
    full close (that stranded on-chain shares while the DB went flat and the
    bankroll was credited cash never received). It shrinks the position via
    reduce_position_atomic and leaves it open for the next cycle."""

    def test_partial_fill_reduces_not_closes(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)

        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        # Hold 10 shares; the bid only absorbs 4.
        monkeypatch.setattr(e, "_submit_taker",
                            lambda token_id, side, amount, fallback_price=None: {"shares": 4.0, "price": 0.70, "fee_bps": 500})
        monkeypatch.setattr(ex, "send_trade_exit", lambda *a, **k: None)
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda tid: (None, None))
        monkeypatch.setattr(ex, "get_realtime_price", lambda tid: (0.71, 0.69))

        reduce_calls = {}
        close_calls = {}
        monkeypatch.setattr(ex, "reduce_position_atomic",
                            lambda **kw: reduce_calls.update(kw))
        monkeypatch.setattr(ex, "close_position_atomic",
                            lambda **kw: close_calls.update(kw))

        pos = {
            "id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
            "entry_price": 0.55, "size_usdc": 5.5, "shares": 10.0,
            "entry_time": "2026-06-30T10:00:00+00:00", "question": "q",
        }

        e._close_position(pos, pnl_dollars=999.0, exit_reason="Stop Loss (-10.0%)")

        # Position was REDUCED, never fully closed.
        assert reduce_calls, "expected a partial-exit reduction"
        assert not close_calls, "must not fully close on a partial fill"
        assert reduce_calls["sold_shares"] == 4.0
        # Money conservation: entry cost freed = sold * entry_price.
        assert reduce_calls["entry_cost_freed"] == pytest.approx(4.0 * 0.55)
        # Proceeds = sold*price minus the per-leg taker fee on the sold shares
        # only (fee_bps is round-trip; the exchange charges half per leg).
        sold_fee = (500 / 10000.0 / 2.0) * 0.70 * 0.30 * 4.0
        assert reduce_calls["proceeds"] == pytest.approx(4.0 * 0.70 - sold_fee)

    def test_full_fill_closes(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(e, "_submit_taker",
                            lambda token_id, side, amount, fallback_price=None: {"shares": 10.0, "price": 0.70, "fee_bps": 500})
        monkeypatch.setattr(ex, "send_trade_exit", lambda *a, **k: None)
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda tid: (None, None))
        monkeypatch.setattr(ex, "get_realtime_price", lambda tid: (0.71, 0.69))
        reduce_calls, close_calls = {}, {}
        monkeypatch.setattr(ex, "reduce_position_atomic", lambda **kw: reduce_calls.update(kw))
        monkeypatch.setattr(ex, "close_position_atomic", lambda **kw: close_calls.update(kw))
        pos = {
            "id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
            "entry_price": 0.55, "size_usdc": 5.5, "shares": 10.0,
            "entry_time": "2026-06-30T10:00:00+00:00", "question": "q",
        }
        e._close_position(pos, pnl_dollars=999.0, exit_reason="Stop Loss (-10.0%)")
        assert close_calls, "full fill should close the position"
        assert not reduce_calls


class TestExitDepthLogging:
    """Order-book $ depth is captured at EXIT too (not just entry) — the
    counterpart to ask_depth_usd/bid_depth_usd logged on entry in signals.
    Entry-time depth alone can't tell you whether the market was still liquid
    enough to actually get out; a book that looked deep going in can (and has,
    live: Seoul and Madrid both went to zero asks after entry) thin out by the
    time a position closes. Best-effort — an exit must still proceed even if
    depth can't be read."""

    def test_close_position_captures_and_forwards_depth(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)

        monkeypatch.setattr(ex, "paper_mode", lambda: True)
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda tid: (123.45, 678.90))
        monkeypatch.setattr(ex, "send_trade_exit", lambda *a, **k: None)

        captured = {}
        monkeypatch.setattr(ex, "close_position_atomic", lambda **kwargs: captured.update(kwargs) or True)

        pos = {
            "id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
            "entry_price": 0.55, "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00", "question": "q",
        }
        e._close_position(pos, pnl_dollars=0.80, exit_reason="Take Profit (0.99 >= 0.98)")

        assert captured["exit_ask_depth_usd"] == 123.45
        assert captured["exit_bid_depth_usd"] == 678.90

    def test_close_position_survives_depth_fetch_failure(self, monkeypatch):
        # If the CLOB book can't be read, the exit must still go through — the
        # position closing is far more important than the depth analytics.
        import executor as ex
        e = Executor.__new__(Executor)

        monkeypatch.setattr(ex, "paper_mode", lambda: True)
        def boom(tid):
            raise ConnectionError("network down")
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", boom)
        monkeypatch.setattr(ex, "send_trade_exit", lambda *a, **k: None)

        captured = {}
        monkeypatch.setattr(ex, "close_position_atomic", lambda **kwargs: captured.update(kwargs) or True)

        pos = {
            "id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
            "entry_price": 0.55, "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00", "question": "q",
        }
        e._close_position(pos, pnl_dollars=0.80, exit_reason="Take Profit (0.99 >= 0.98)")

        assert captured["exit_ask_depth_usd"] is None
        assert captured["exit_bid_depth_usd"] is None


class TestIntradayMetarExit:
    def _exec(self):
        return Executor.__new__(Executor)

    def test_low_temp_market_exits_when_obs_hits_bucket(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", True)
        # Pin the stop loss off: this test asserts the METAR thesis-break exit fires,
        # and its 0.62 -> 0.19 drawdown (-69%) would trip STOP_LOSS_PCT (enabled
        # 2026-07-26) first, exiting for the wrong reason and hiding a real regression.
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", False)
        e = self._exec()

        # Target date is today
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pos = {
            "id": 1,
            "market_id": "0x1",
            "token_id": "tok_1",
            "side": "NO",
            "entry_price": 0.62,
            "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00",
            "target_date": today_str,
            "city": "New York",
            "is_high": 0
        }
        
        # Mock realtime price of NO is low (0.19)
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.19, 0.19))
        
        # Mock signal row in DB
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [{
            "id": 1,
            "model_prob": 0.22,
            "target_date": today_str,
            "bucket_low": 64.0,
            "bucket_high": 65.0
        }])
        
        # Mock get_signal_engine to return forecast
        monkeypatch.setattr(ex, "get_signal_engine", lambda *a, **k: {
            "ensemble_mean": 65.2,
            "ensemble_std": 1.5
        })
        
        # Mock metar extremes: observed min is 18.33 C = 65.0 F (after round, rounds to 18 C = 64.4 F)
        monkeypatch.setattr(ex, "fetch_day_extremes", lambda *a: (18.33, 18.33))
        
        exits_called = []
        monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits_called.append(reason))

        # Since 2026-08-11 a thesis-break IN A LOSS also answers to the physics gate,
        # so the day has to agree the position is dead. That is the honest reading of
        # this scenario: the min has fallen into the bucket AND the fall is spent, so
        # nothing can carry it back out. The gate blocking the mid-transit version of
        # the same reading is asserted in TestSustainedLossGuard.
        monkeypatch.setattr(e, "_settlement_state", lambda p: {
            "state": "LOCKED_LOSS", "observed": 64.0, "reason": "stubbed: fall spent"})
        monkeypatch.setattr(ex, "estimate_sale", lambda tok, sh, force=False: {
            "vwap": 0.19, "filled_shares": sh, "exhausted": False,
            "best_bid": 0.19, "slippage_frac": 0.0})

        e._check_exit_for_position(pos)

        # YES prob = 1.0 - CDF(63.5) = 1.0 - 0.1286 = 0.8714. NO prob = 0.1286.
        # Edge = 0.1286 - 0.19 = -0.0614 < 0.05. Price is below entry, so thesis broken.
        assert len(exits_called) == 1
        assert "Edge decayed" in exits_called[0]
        assert "thesis broken" in exits_called[0]

    def test_a_losing_thesis_break_is_blocked_while_the_day_can_still_turn(self, monkeypatch):
        """The Qingdao inversion applied to the thesis-break rule. Same collapsing
        price, same broken forecast — but if the observations have not finished the
        job, the position is not sold. _thesis_broken returns True on 'we are in a
        real loss', which is precisely the trigger that cost $11.07."""
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", True)
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", False)
        e = self._exec()

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pos = {"id": 1, "market_id": "0x1", "token_id": "tok_1", "side": "NO",
               "entry_price": 0.62, "size_usdc": 2.0,
               "entry_time": "2026-06-30T10:00:00+00:00", "target_date": today_str,
               "city": "New York", "is_high": 0}
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.19, 0.19))
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [{
            "id": 1, "model_prob": 0.22, "target_date": today_str,
            "bucket_low": 64.0, "bucket_high": 65.0}])
        monkeypatch.setattr(ex, "get_signal_engine", lambda *a, **k: {
            "ensemble_mean": 66.0, "ensemble_std": 2.0, "is_high": False,
            "city_key": "New York", "model_count": 4})
        monkeypatch.setattr(ex, "get_bucket_probability", lambda *a, **k: 0.22)
        monkeypatch.setattr(ex, "get_station", lambda c: ("KNYC", "America/New_York"))
        monkeypatch.setattr(ex, "fetch_day_extremes", lambda *a: (17.8, 17.8))

        exits_called = []
        monkeypatch.setattr(e, "_close_position", lambda *a, **k: exits_called.append(a))
        monkeypatch.setattr(e, "_settlement_state", lambda p: {
            "state": "UNDECIDED", "observed": 64.0, "reason": "stubbed: fall left"})
        e._check_exit_for_position(pos)
        assert exits_called == [], f"must hold while undecided, got {exits_called}"

    def test_low_temp_market_holds_when_obs_outside_bucket(self, monkeypatch):
        import executor as ex
        e = self._exec()
        
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pos = {
            "id": 1,
            "market_id": "0x1",
            "token_id": "tok_1",
            "side": "NO",
            "entry_price": 0.62,
            "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00",
            "target_date": today_str,
            "city": "New York",
            "is_high": 0
        }
        
        # Price has risen to 0.70 (winning)
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.70, 0.70))
        
        # Consistent model prob mock: 0.45 YES -> 0.55 NO.
        # Forecast prob also evaluates to ~0.45 YES, so no probability change.
        def mock_fetch_query(sql, params=()):
            if "trades" in sql:
                return [{"model_prob": 0.45}]
            return [{
                "id": 1,
                "model_prob": 0.45,
                "target_date": today_str,
                "bucket_low": 64.0,
                "bucket_high": 65.0
            }]
        monkeypatch.setattr(ex, "fetch_query", mock_fetch_query)
        
        monkeypatch.setattr(ex, "get_signal_engine", lambda *a, **k: {
            "ensemble_mean": 65.2,
            "ensemble_std": 1.5
        })
        
        # Observed min is 22.0 C = 71.6 F (well above bucket)
        monkeypatch.setattr(ex, "fetch_day_extremes", lambda *a: (22.0, 22.0))
        
        exits_called = []
        monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits_called.append(reason))
        
        e._check_exit_for_position(pos)
        
        # Should hold because the price converged in our favor and thesis is intact.
        assert len(exits_called) == 0


class TestSustainedLossGuard:
    """The sustained-loss guard must fire after SUSTAINED_LOSS_POLLS consecutive
    below-entry polls, independent of edge formula, and must reset when price recovers."""

    def _exec(self):
        e = Executor.__new__(Executor)
        e._loss_streak = {}
        return e

    @staticmethod
    def _open_the_physics_gate(e, monkeypatch, state="LOCKED_LOSS"):
        """Stub the 2026-08-11 physics gate to `state`.

        These tests are about the PRICE rules, and since Qingdao no price rule can
        close a losing position on its own — the day's observations have to agree
        it is dead. Stubbing LOCKED_LOSS isolates the rule under test; the gate's
        own behaviour lives in tests/test_physics_exit_gate.py, and the blocking
        direction is asserted end-to-end below."""
        monkeypatch.setattr(
            e, "_settlement_state",
            lambda pos: {"state": state, "observed": 64.0, "reason": "stubbed"})

    @staticmethod
    def _deep_bid_book(monkeypatch):
        """A bid side that can absorb the sale at the quote, so the exit-liquidity
        guard is not the thing under test here."""
        import executor as ex
        monkeypatch.setattr(ex, "estimate_sale", lambda tok, sh, force=False: {
            "vwap": 0.2299, "filled_shares": sh, "exhausted": False,
            "best_bid": 0.23, "slippage_frac": 0.0})

    def _pos(self):
        return {
            "id": 99,
            "market_id": "0xABC",
            "token_id": "tok_abc",
            "side": "NO",
            "entry_price": 0.60,
            "size_usdc": 2.0,
            "entry_time": "2026-06-30T10:00:00+00:00",
            "target_date": "2099-12-31",   # far future — target date guard won't fire
            "city": "New York",
            "is_high": 0,
        }

    def test_stop_loss_fires_past_threshold_holds_above_it(self, monkeypatch):
        """STOP_LOSS_PCT (0.60, enabled 2026-07-26) exits a collapsing position but
        leaves shallower drawdowns alone. The boundary matters and is tight: live
        Chongqing 2026-07-25 bottomed at -56.2% and recovered to a winner, so the
        threshold must sit BELOW that. Entry 0.60 -> 0.23 is -61.7% (exits); -> 0.25
        is -58.3% (holds, and is where Chongqing would have sat)."""
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", False)
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", False)
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", True)
        monkeypatch.setitem(__import__("config")._RUNTIME, "STOP_LOSS_PCT", 0.60)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])

        self._deep_bid_book(monkeypatch)
        for price, should_exit in [(0.23, True), (0.25, False)]:
            monkeypatch.setattr(ex, "get_realtime_price", lambda *a, _p=price: (_p, _p))
            e = self._exec()
            self._open_the_physics_gate(e, monkeypatch)
            exits = []
            monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits.append(reason))
            e._check_exit_for_position(self._pos())
            if should_exit:
                assert len(exits) == 1 and exits[0].startswith("Stop Loss"), exits
            else:
                assert exits == [], exits

    @pytest.mark.parametrize("state", ["LOCKED_WIN", "UNDECIDED", "UNKNOWN"])
    def test_the_physics_gate_blocks_the_stop_loss_end_to_end(self, monkeypatch, state):
        """Qingdao 2026-08-11, through the real exit path. Entry 0.60 -> 0.23 is
        -61.7%, comfortably past a 0.60 threshold, and the stop is switched ON — yet
        nothing may close while the day's observations have not killed the position.
        UNDECIDED is the state Qingdao was actually stopped out in; LOCKED_WIN is
        where it sat 42 minutes later; UNKNOWN is a dead station."""
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", False)
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", False)
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", True)
        monkeypatch.setitem(__import__("config")._RUNTIME, "STOP_LOSS_PCT", 0.60)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.23, 0.23))
        self._deep_bid_book(monkeypatch)

        e = self._exec()
        self._open_the_physics_gate(e, monkeypatch, state)
        exits = []
        monkeypatch.setattr(e, "_close_position", lambda *a, **k: exits.append(a))
        e._check_exit_for_position(self._pos())
        assert exits == [], f"{state} must not permit a loss exit, got {exits}"

    def test_a_thin_bid_book_stands_the_loss_exit_down(self, monkeypatch):
        """Even with the physics agreeing the position is dead, the sale must not
        sweep a book that cannot absorb it. Qingdao's fill landed 7.9% through its
        own top bid; the arm stays live and the next cycle re-reads the book."""
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", False)
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", False)
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", True)
        monkeypatch.setitem(__import__("config")._RUNTIME, "STOP_LOSS_PCT", 0.60)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.23, 0.23))
        monkeypatch.setattr(ex, "estimate_sale", lambda tok, sh, force=False: {
            "vwap": 0.20, "filled_shares": sh, "exhausted": False,
            "best_bid": 0.23, "slippage_frac": 0.13})

        e = self._exec()
        self._open_the_physics_gate(e, monkeypatch)
        exits = []
        monkeypatch.setattr(e, "_close_position", lambda *a, **k: exits.append(a))
        e._check_exit_for_position(self._pos())
        assert exits == [], f"a 13% walk must stand down, got {exits}"

    def _past_date_pos(self):
        p = self._pos()
        p["target_date"] = "2020-01-01"   # long past — hold-to-resolution gate is active
        return p

    def test_post_date_salvage_fires_only_on_a_real_bid_with_depth(self, monkeypatch):
        """Past the target date the bot holds for resolution, EXCEPT for a position
        already past STOP_LOSS_PCT that still has a fillable bid. The depth check is
        what separates this from the phantom-$0.999 fill class: an extreme quote with
        no size behind it must NOT book an exit."""
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", False)
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", False)
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", True)
        monkeypatch.setitem(__import__("config")._RUNTIME, "STOP_LOSS_PCT", 0.60)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])

        # entry 0.60, shares held = 2.0/0.60 = 3.333
        # bid 0.20 => -66.7%. Need depth >= 3.333*0.20 = $0.67.
        #
        # The -33% case no longer turns on a percentage: since 2026-08-11 this path
        # is gated on the physics saying LOCKED_LOSS, and past the target date the
        # day is over so that verdict is deterministic. It is expressed here by
        # stubbing the gate, which is what "the temperature already decided" means.
        cases = [
            (0.20, 5.00, "LOCKED_LOSS", True,  "real bid with depth -> salvage"),
            (0.20, 0.10, "LOCKED_LOSS", False, "real bid but no depth -> phantom, must hold"),
            (0.20, None, "LOCKED_LOSS", False, "depth unreadable -> fail closed, must hold"),
            (0.20, 5.00, "LOCKED_WIN",  False, "settles at $1 -> must never sell"),
            (0.20, 5.00, "UNKNOWN",     False, "no observation -> fail closed, must hold"),
            (0.00, 5.00, "LOCKED_LOSS", False, "no bid at all -> must hold"),
        ]
        for bid, depth, state, should_exit, label in cases:
            monkeypatch.setattr(ex, "get_realtime_price", lambda *a, _b=bid: (_b, _b))
            monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda *a, _d=depth: (None, _d))
            e = self._exec()
            self._open_the_physics_gate(e, monkeypatch, state)
            exits = []
            monkeypatch.setattr(e, "_close_position",
                                lambda pos, pnl_dollars=None, exit_reason=None: exits.append(exit_reason))
            e._check_exit_for_position(self._past_date_pos())
            if should_exit:
                assert len(exits) == 1 and "salvage" in exits[0], f"{label}: {exits}"
            else:
                assert exits == [], f"{label}: {exits}"

    def test_post_date_salvage_has_its_own_switch(self, monkeypatch):
        """Split from ENABLE_STOP_LOSS on 2026-08-11. The salvage is a different
        animal — it fires only once the temperature is realized and sweeps cents off
        a position already heading to $0 — so turning off the harmful mid-day stop
        must NOT disable it, and its own flag must still hold everything."""
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", False)
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", False)
        # The mid-day stop off, the salvage on: the salvage must still fire.
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", False)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.05, 0.05))
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda *a: (None, 50.0))

        for salvage_on, expect_exit in [(True, True), (False, False)]:
            monkeypatch.setattr(ex, "ENABLE_POST_DATE_SALVAGE", salvage_on)
            e = self._exec()
            self._open_the_physics_gate(e, monkeypatch)
            exits = []
            monkeypatch.setattr(e, "_close_position",
                                lambda pos, pnl_dollars=None, exit_reason=None: exits.append(exit_reason))
            e._check_exit_for_position(self._past_date_pos())
            assert bool(exits) is expect_exit, f"salvage={salvage_on}: {exits}"

    def test_fires_after_threshold_polls(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", True)
        monkeypatch.setattr(ex, "SUSTAINED_LOSS_POLLS", 3)
        # Price below entry (0.40 < 0.60)
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.40, 0.40))
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])

        self._deep_bid_book(monkeypatch)
        e = self._exec()
        self._open_the_physics_gate(e, monkeypatch)
        pos = self._pos()
        exits = []
        monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits.append(reason))

        # Two polls below entry — should NOT fire yet
        e._check_exit_for_position(pos)
        assert exits == []
        e._check_exit_for_position(pos)
        assert exits == []

        # Third poll — fires
        e._check_exit_for_position(pos)
        assert len(exits) == 1
        assert "Sustained loss" in exits[0]
        assert "3 polls" in exits[0]

    def test_does_not_fire_below_threshold(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "SUSTAINED_LOSS_POLLS", 3)
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.40, 0.40))
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])

        e = self._exec()
        pos = self._pos()
        exits = []
        monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits.append(reason))

        e._check_exit_for_position(pos)
        e._check_exit_for_position(pos)
        assert exits == []  # only 2 polls

    def test_streak_resets_on_price_recovery(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", True)
        monkeypatch.setattr(ex, "SUSTAINED_LOSS_POLLS", 3)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])

        e = self._exec()
        pos = self._pos()
        exits = []
        monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits.append(reason))

        # Two polls below entry
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.40, 0.40))
        e._check_exit_for_position(pos)
        e._check_exit_for_position(pos)
        assert e._loss_streak.get(99, 0) == 2

        # Price recovers above entry — streak resets
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.70, 0.70))
        e._check_exit_for_position(pos)
        assert e._loss_streak.get(99, 0) == 0
        assert exits == []

    def test_guard_disabled_by_default_holds_deep_underwater(self, monkeypatch):
        """With ENABLE_SUSTAINED_LOSS_GUARD off (the default), a position sitting far
        below entry across many polls is HELD to resolution, not force-exited. Backtest
        showed early exits killed 4 winners for every 1 loss avoided."""
        import executor as ex
        # defaults: both guards off
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", False)
        monkeypatch.setattr(ex, "ENABLE_THESIS_BREAK_EXIT", False)
        # Pin the stop loss off: this test isolates the SUSTAINED-loss guard. Its -50%
        # drawdown is shallower than STOP_LOSS_PCT (0.60) today, but pinning the flag
        # keeps the two guards independent so lowering the threshold later can't
        # silently turn this into a stop-loss test.
        monkeypatch.setitem(__import__("config")._RUNTIME, "ENABLE_STOP_LOSS", False)
        monkeypatch.setattr(ex, "SUSTAINED_LOSS_POLLS", 3)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        # deep drawdown (0.30 vs 0.60 entry = -50%), sustained across 5 polls
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.30, 0.30))

        e = self._exec()
        pos = self._pos()
        exits = []
        monkeypatch.setattr(e, "_close_position", lambda pos, pnl, reason: exits.append(reason))

        for _ in range(5):
            e._check_exit_for_position(pos)
        assert exits == []  # never exits — held to resolution
        assert e._loss_streak.get(99, 0) == 0  # streak never accrues while guard is off



class TestExternalCloseSync:
    """Positions sold manually on the Polymarket website must reconcile into the
    DB at the price actually received — not sit open until resolution and then
    book $1/$0 (first live case: Guangzhou NO sold manually at $0.87; resolution
    settlement would have credited $1.00, overstating PnL by ~$0.45).

    Evidence rules: close ONLY when the wallet balance is missing AND sell fills
    exist since entry. Missing balance alone = redemption/API lag, leave open.
    Any API failure = unknown, never 'sold'."""

    TOK = "43393472091697977127127730869570649514267918649527086255045669699126826621279"

    def _exec(self):
        return Executor.__new__(Executor)

    def _pos(self, **over):
        from datetime import timedelta
        entry = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        pos = {
            "id": 55, "market_id": "0x38b5", "token_id": self.TOK, "side": "NO",
            "entry_price": 0.59, "size_usdc": 2.0, "shares": 3.389829,
            "entry_time": entry, "question": "q", "city": "Guangzhou",
            "target_date": "2026-07-24",
        }
        pos.update(over)
        return pos

    def _wire(self, monkeypatch, pos, wallet, sells):
        import executor as ex
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(ex, "POLYMARKET_FUNDER", "0xdead")
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [pos])
        monkeypatch.setattr(ex, "get_wallet_token_sizes", lambda user: wallet)
        monkeypatch.setattr(ex, "get_wallet_sells", lambda *a, **k: sells)
        e = self._exec()
        closes, reduces = [], {}
        monkeypatch.setattr(e, "_close_position",
                            lambda pos, pnl_dollars, exit_reason: closes.append((pnl_dollars, exit_reason)))
        monkeypatch.setattr(ex, "reduce_position_atomic",
                            lambda **kw: reduces.update(kw) or True)
        return e, closes, reduces

    def test_positions_api_unreachable_no_action(self, monkeypatch):
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet=None, sells=[(0.87, 3.38)])
        assert e.sync_external_closes() == 0
        assert not closes and not reduces

    def test_fully_held_no_action(self, monkeypatch):
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={self.TOK: 3.389829}, sells=[(0.87, 3.38)])
        assert e.sync_external_closes() == 0
        assert not closes and not reduces

    def test_dust_shortfall_counts_as_held(self, monkeypatch):
        # Balance short by 0.03 sh (< dust tolerance) — rounding, not a sale.
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={self.TOK: 3.36}, sells=[(0.87, 3.38)])
        assert e.sync_external_closes() == 0
        assert not closes and not reduces

    def test_manual_full_sale_books_actual_price(self, monkeypatch):
        # The live Guangzhou case: 3.38 of 3.3898 sh sold at $0.87, wallet shows
        # nothing left. Close at real proceeds minus taker fee, NOT at $1.00.
        import executor as ex
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={}, sells=[(0.87, 3.38)])
        assert e.sync_external_closes() == 1
        assert len(closes) == 1 and not reduces
        pnl, reason = closes[0]
        fee = ex.TAKER_FEE_RATE * 0.87 * 0.13 * 3.38
        assert pnl == pytest.approx(0.87 * 3.38 - fee - 2.0)
        assert reason.startswith("EXTERNAL_CLOSE")

    def test_missing_balance_without_sells_left_open(self, monkeypatch):
        # Zero balance but no sell fills = post-resolution redemption or Data-API
        # indexing lag. The resolution path is the correct closer; do nothing.
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={}, sells=[])
        assert e.sync_external_closes() == 0
        assert not closes and not reduces

    def test_trades_api_unreachable_left_open(self, monkeypatch):
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={}, sells=None)
        assert e.sync_external_closes() == 0
        assert not closes and not reduces

    def test_fresh_position_skipped_for_indexing_lag(self, monkeypatch):
        from datetime import timedelta
        entry = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        pos = self._pos(entry_time=entry)
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={}, sells=[(0.87, 3.38)])
        assert e.sync_external_closes() == 0
        assert not closes and not reduces

    def test_paper_mode_noop(self, monkeypatch):
        import executor as ex
        pos = self._pos()
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={}, sells=[(0.87, 3.38)])
        monkeypatch.setattr(ex, "paper_mode", lambda: True)
        assert e.sync_external_closes() == 0
        assert not closes

    def test_partial_manual_sale_reduces(self, monkeypatch):
        import executor as ex
        pos = self._pos(shares=10.0, size_usdc=5.9)
        e, closes, reduces = self._wire(monkeypatch, pos, wallet={self.TOK: 6.0}, sells=[(0.90, 4.0)])
        assert e.sync_external_closes() == 1
        assert not closes, "partial sale must not fully close"
        assert reduces["sold_shares"] == pytest.approx(4.0)
        assert reduces["entry_cost_freed"] == pytest.approx(4.0 * 0.59)
        fee = ex.TAKER_FEE_RATE * 0.90 * 0.10 * 4.0
        assert reduces["proceeds"] == pytest.approx(0.90 * 4.0 - fee)
        assert reduces["pnl_delta"] == pytest.approx((0.90 * 4.0 - fee) - 4.0 * 0.59)

    def test_external_close_never_submits_clob_sell(self, monkeypatch):
        # The shares are already gone — a CLOB SELL would be rejected forever.
        import executor as ex
        e = self._exec()
        monkeypatch.setattr(ex, "paper_mode", lambda: False)
        monkeypatch.setattr(e, "_submit_taker",
                            lambda *a, **k: pytest.fail("EXTERNAL_ close must not hit the CLOB"))
        monkeypatch.setattr(ex, "send_trade_exit", lambda *a, **k: None)
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda tid: (None, None))
        close_calls = {}
        monkeypatch.setattr(ex, "close_position_atomic", lambda **kw: close_calls.update(kw) or True)
        pos = self._pos()
        e._close_position(pos, pnl_dollars=0.92, exit_reason="EXTERNAL_CLOSE (3.38 sh @ $0.870 manual sale)")
        assert close_calls["pnl_dollars"] == pytest.approx(0.92)


class TestWalletDataApiFilters:
    """get_wallet_sells must only surface SELL fills for the right token after
    entry; get_wallet_token_sizes must return None (unknown) on any API failure,
    never an empty dict that reads as 'wallet is flat'."""

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
        def json(self):
            return self._payload

    def test_sells_filters_side_asset_and_time(self, monkeypatch):
        import scanner as sc
        rows = [
            {"side": "SELL", "asset": "tok", "price": 0.87, "size": 3.38, "timestamp": 2000},
            {"side": "BUY",  "asset": "tok", "price": 0.59, "size": 3.39, "timestamp": 1500},  # wrong side
            {"side": "SELL", "asset": "other", "price": 0.98, "size": 2.0, "timestamp": 2100}, # wrong token
            {"side": "SELL", "asset": "tok", "price": 0.50, "size": 1.0, "timestamp": 100},    # before entry
        ]
        monkeypatch.setattr(sc, "safe_get", lambda *a, **k: self._Resp(200, rows))
        assert sc.get_wallet_sells("0xdead", "0xmkt", "tok", since_epoch=1500) == [(0.87, 3.38)]

    def test_sells_api_error_returns_none(self, monkeypatch):
        import scanner as sc
        monkeypatch.setattr(sc, "safe_get", lambda *a, **k: self._Resp(500, []))
        assert sc.get_wallet_sells("0xdead", "0xmkt", "tok", since_epoch=0) is None

    def test_sizes_sums_per_asset(self, monkeypatch):
        import scanner as sc
        rows = [{"asset": "a", "size": 1.5}, {"asset": "b", "size": 2.0}, {"asset": "a", "size": 0.5}]
        monkeypatch.setattr(sc, "safe_get", lambda *a, **k: self._Resp(200, rows))
        assert sc.get_wallet_token_sizes("0xdead") == {"a": 2.0, "b": 2.0}

    def test_sizes_api_error_returns_none(self, monkeypatch):
        import scanner as sc
        monkeypatch.setattr(sc, "safe_get", lambda *a, **k: self._Resp(200, {"error": "nope"}))
        assert sc.get_wallet_token_sizes("0xdead") is None
        monkeypatch.setattr(sc, "safe_get", lambda *a, **k: self._Resp(503, []))
        assert sc.get_wallet_token_sizes("0xdead") is None


class TestOneTradePerCityDate:
    """Sibling buckets on the same city/target-date settle on ONE realized
    temperature — a second entry there is stacked exposure, not a new bet. Any prior
    trade (open or closed) for the pair must block entry."""

    class _Opp:
        market_id = "0xnew"
        city = "Hong Kong"
        date = "2026-07-26"
        question = "q"
        is_high = True

    def _exec(self):
        return Executor.__new__(Executor)

    def _signal(self):
        return {"opp": self._Opp(), "side": "NO", "size_usdc": 2.0, "price": 0.6,
                "edge": 0.2, "model_prob": 0.15, "token_id": "t", "model_count": 4}

    def test_second_trade_same_city_date_blocked(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "get_open_position", lambda mid: None)
        calls = []
        def fake_fetch(sql, params=()):
            calls.append(sql)
            if "FROM trades WHERE city=?" in sql:
                return [{"id": 63}]  # prior HK trade that day
            return []
        monkeypatch.setattr(ex, "fetch_query", fake_fetch)
        opened = []
        monkeypatch.setattr(ex, "open_position_atomic",
                            lambda **kw: opened.append(kw))
        self._exec().execute_trade(self._signal())
        assert opened == []          # entry refused
        assert any("FROM trades WHERE city=?" in c for c in calls)

    def test_first_trade_for_city_date_proceeds_to_later_gates(self, monkeypatch):
        """With no prior city/date trade, execution must get PAST the restriction
        (we stop it at the concurrent-positions gate to avoid a full order path)."""
        import executor as ex
        monkeypatch.setattr(ex, "get_open_position", lambda mid: None)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        monkeypatch.setattr(Executor, "get_open_positions_count", lambda self: 10**9)
        opened = []
        monkeypatch.setattr(ex, "open_position_atomic",
                            lambda **kw: opened.append(kw))
        self._exec().execute_trade(self._signal())
        assert opened == []  # blocked at max-concurrent, i.e. restriction passed


class TestDailyTradeCap:
    """MAX_TRADES_PER_DAY (owner decision 2026-08-12): at most N new entries
    per UTC day. The take-everything rule set makes qualifying markets the
    norm, so worst-case daily exposure must be bounded at the entry point."""

    class _Opp:
        market_id = "0xnew"
        city = "Seoul"
        date = "2026-08-12"
        question = "q"
        is_high = True

    def _exec(self):
        return Executor.__new__(Executor)

    def _signal(self):
        return {"opp": self._Opp(), "side": "NO", "size_usdc": 3.0, "price": 0.75,
                "edge": 0.1, "model_prob": 0.2, "token_id": "t", "model_count": 4}

    def _run(self, monkeypatch, entries_today):
        import executor as ex
        monkeypatch.setattr(ex, "get_open_position", lambda mid: None)
        calls = []
        def fake_fetch(sql, params=()):
            calls.append(sql)
            if "COUNT(*)" in sql and "entry_time >=" in sql:
                return [{"c": entries_today}]
            if "FROM trades WHERE city=?" in sql:
                return [{"id": 1}]   # city/date guard fires AFTER the cap
            return []
        monkeypatch.setattr(ex, "fetch_query", fake_fetch)
        opened = []
        monkeypatch.setattr(ex, "open_position_atomic", lambda **kw: opened.append(kw))
        self._exec().execute_trade(self._signal())
        return calls, opened

    def test_at_cap_refuses_before_any_other_guard(self, monkeypatch):
        import executor as ex
        assert ex.MAX_TRADES_PER_DAY == 15   # deployed value, change deliberately
        calls, opened = self._run(monkeypatch, entries_today=15)
        assert opened == []
        assert not any("WHERE city=?" in c for c in calls)   # stopped at the cap

    def test_under_cap_proceeds_to_later_guards(self, monkeypatch):
        calls, opened = self._run(monkeypatch, entries_today=14)
        assert opened == []                                  # blocked later...
        assert any("WHERE city=?" in c for c in calls)       # ...past the cap


class TestManualClose:
    """Dashboard 'Close' button — Executor.close_position_manual.

    The monitor loop treats every non-close outcome the same (log, retry next
    cycle); a human pressing a button must instead be told which one happened,
    and must never be able to fire a second sell while the bot is mid-exit on
    the same position.
    """

    def _exec(self):
        return Executor.__new__(Executor)

    def _pos(self):
        return {"id": 7, "market_id": "mkt-7", "side": "NO", "shares": 10.0,
                "question": "Will it be hot?", "entry_price": 0.40,
                "size_usdc": 4.0, "token_id": "tok-7"}

    def test_closed_reports_realized_pnl(self, monkeypatch):
        import executor as ex
        pos = self._pos()
        monkeypatch.setattr(ex, "get_position_by_id",
                            lambda pid: pos if not closed else None)
        closed = False
        def fake_close(self, p, pnl_dollars, exit_reason):
            nonlocal closed
            closed = True
            assert exit_reason.startswith("MANUAL_CLOSE")
        monkeypatch.setattr(Executor, "_close_position", fake_close)
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [{"pnl": 1.25}])
        r = self._exec().close_position_manual(7)
        assert r["ok"] is True and r["status"] == "closed"
        assert r["pnl"] == 1.25 and "+1.25" in r["message"]

    def test_no_fill_leaves_position_open_and_says_so(self, monkeypatch):
        """_close_position returns silently when the sell doesn't fill; the row
        is still there and share count is unchanged → report no_fill, not success."""
        import executor as ex
        pos = self._pos()
        monkeypatch.setattr(ex, "get_position_by_id", lambda pid: pos)
        monkeypatch.setattr(Executor, "_close_position",
                            lambda self, p, pnl_dollars, exit_reason: None)
        r = self._exec().close_position_manual(7)
        assert r["ok"] is False and r["status"] == "no_fill"
        assert "still open" in r["message"]

    def test_partial_fill_reports_remaining_shares(self, monkeypatch):
        import executor as ex
        pos = self._pos()
        after = dict(pos, shares=4.0)          # 6 of 10 sold
        monkeypatch.setattr(ex, "get_position_by_id", lambda pid: after if sold else pos)
        sold = False
        def fake_close(self, p, pnl_dollars, exit_reason):
            nonlocal sold
            sold = True
        monkeypatch.setattr(Executor, "_close_position", fake_close)
        r = self._exec().close_position_manual(7)
        assert r["ok"] is True and r["status"] == "partial"
        assert r["shares_sold"] == 6.0 and r["shares_remaining"] == 4.0

    def test_already_closed_is_not_found(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "get_position_by_id", lambda pid: None)
        calls = []
        monkeypatch.setattr(Executor, "_close_position",
                            lambda self, p, pnl_dollars, exit_reason: calls.append(p))
        r = self._exec().close_position_manual(7)
        assert r["ok"] is False and r["status"] == "not_found"
        assert calls == []          # no sell attempted on a vanished position

    def test_lock_held_elsewhere_refuses_second_sell(self, monkeypatch):
        """The core race: monitor thread is mid-exit on this position, user
        clicks Close. Must refuse rather than submit a concurrent SELL."""
        import executor as ex
        e = self._exec()
        e._exit_lock(7).acquire()           # simulate the monitor holding it
        calls = []
        monkeypatch.setattr(Executor, "_close_position",
                            lambda self, p, pnl_dollars, exit_reason: calls.append(p))
        monkeypatch.setattr(ex, "get_position_by_id", lambda pid: self._pos())
        r = e.close_position_manual(7)
        assert r["ok"] is False and r["status"] == "busy"
        assert calls == []                  # no second order submitted

    def test_lock_released_after_close(self, monkeypatch):
        """A completed manual close must not leave the position permanently
        locked — that would silently disable the bot's own exits for it."""
        import executor as ex
        monkeypatch.setattr(ex, "get_position_by_id", lambda pid: None)
        e = self._exec()
        e.close_position_manual(7)
        assert e._exit_lock(7).acquire(blocking=False) is True

    def test_lock_released_when_close_raises(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "get_position_by_id", lambda pid: self._pos())
        def boom(self, p, pnl_dollars, exit_reason):
            raise RuntimeError("CLOB down")
        monkeypatch.setattr(Executor, "_close_position", boom)
        e = self._exec()
        with pytest.raises(RuntimeError):
            e.close_position_manual(7)
        assert e._exit_lock(7).acquire(blocking=False) is True
