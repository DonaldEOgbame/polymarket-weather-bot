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
