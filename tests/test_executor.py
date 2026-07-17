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

# executor imports py_clob_client at module load; guard so tests run headless.
import types
for mod in ("py_clob_client", "py_clob_client.client", "py_clob_client.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client.client"].ClobClient = object
ct = sys.modules["py_clob_client.clob_types"]
for n in ("OrderArgs", "MarketOrderArgs", "OrderType", "ApiCreds"):
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
        monkeypatch.setattr(ex, "resolved_extreme_f", lambda *a: 77.0)
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
        monkeypatch.setattr(ex, "resolved_extreme_f", lambda *a: None)
        wrote = []
        monkeypatch.setattr(ex, "execute_query", lambda *a, **k: wrote.append(1))
        assert e.settle_closed_trade(self._trade()) is False
        assert wrote == []


class TestLiveExitFeeDeduction:
    """Live-mode exits recompute PnL from the actual fill price (not the paper
    mid estimate), but must still subtract the taker fee — omitting it silently
    overstates every live exit's realized PnL (stop-loss, take-profit, edge-decay)."""

    def test_live_exit_pnl_subtracts_fee(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)

        monkeypatch.setattr(ex, "PAPER_MODE", False)
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
        expected_fee = (500 / 10000.0) * 0.70 * (1.0 - 0.70) * shares
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

        monkeypatch.setattr(ex, "PAPER_MODE", False)
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
        # Proceeds = sold*price minus the taker fee on the sold shares only.
        sold_fee = (500 / 10000.0) * 0.70 * 0.30 * 4.0
        assert reduce_calls["proceeds"] == pytest.approx(4.0 * 0.70 - sold_fee)

    def test_full_fill_closes(self, monkeypatch):
        import executor as ex
        e = Executor.__new__(Executor)
        monkeypatch.setattr(ex, "PAPER_MODE", False)
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

        monkeypatch.setattr(ex, "PAPER_MODE", True)
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

        monkeypatch.setattr(ex, "PAPER_MODE", True)
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
        
        e._check_exit_for_position(pos)
        
        # YES prob = 1.0 - CDF(63.5) = 1.0 - 0.1286 = 0.8714. NO prob = 0.1286.
        # Edge = 0.1286 - 0.19 = -0.0614 < 0.05. Price is below entry, so thesis broken.
        assert len(exits_called) == 1
        assert "Edge decayed" in exits_called[0]
        assert "thesis broken" in exits_called[0]

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

    def test_fires_after_threshold_polls(self, monkeypatch):
        import executor as ex
        monkeypatch.setattr(ex, "ENABLE_SUSTAINED_LOSS_GUARD", True)
        monkeypatch.setattr(ex, "SUSTAINED_LOSS_POLLS", 3)
        # Price below entry (0.40 < 0.60)
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.40, 0.40))
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])

        e = self._exec()
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

