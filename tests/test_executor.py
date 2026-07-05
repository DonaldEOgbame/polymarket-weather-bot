"""Regression tests for executor exit logic — specifically the phantom-fill guard.

Historical forensic finding (2026-07-04): five 'edge decayed' exits in the
deployed DB were each booked at a NO bid of ~0.999, a price never once observed
with real size (the maximum NO price across all 44,879 logged signals was 0.81).
They fired AFTER each market's target date had passed, on a resolving book whose
only resting quotes were extreme and shallow. The correct behavior is to hold
such positions for resolution settlement ($1/$0), not to book a market exit.
"""
import sys, os
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# executor imports py_clob_client at module load; guard so tests run headless.
import types
for mod in ("py_clob_client", "py_clob_client.client", "py_clob_client.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client.client"].ClobClient = object
ct = sys.modules["py_clob_client.clob_types"]
for n in ("OrderArgs", "MarketOrderArgs", "OrderType"):
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
