"""The exit-side diurnal ratchet, and the Qingdao 2026-08-11 regression.

A daily maximum can only rise for the rest of the local day; a daily minimum can
only fall. That one-way ratchet is arithmetic, and it is strictly better evidence
about settlement than any price the order book prints — which is the lesson of
Qingdao 2026-08-11.

WHAT HAPPENED. The bot held NO on "will the highest temperature in Qingdao be
30°C on August 11", bucket 85.6-86.4°F, bought at 0.66 for $10 (15.151514 shares).
The day's max climbed THROUGH 30.0°C on its way to 30.8°C. During that transit the
book dislocated: mid went 0.58 -> 0.55 -> 0.495 -> 0.35 -> 0.295, the 50% stop
fired on the mid at 13:18 local, and the sale swept 15.15 shares into $109 of bid
depth for $0.2578/share — 2.2c THROUGH a $0.28 top bid. Within the hour NO was
back at ~1.00. Realized -$6.09 where holding paid +$4.98: the stop destroyed
$11.07 and accounted for 94% of the era's entire loss.

WHY A PERCENTAGE COULD NEVER HAVE FIXED IT. Scored against all 10 resolved
positions, every threshold is net-negative versus holding, because the drawdown
distributions do not separate: winners dipped to -55%, -28% and -24%, losers to
-42% and -31%, and one loser (Beijing) never dipped past -10%. Price drawdown
carries almost no information about the outcome here.

So these tests assert an INVERSION of authority: observations decide, price only
proposes. The two cases that matter most are the last two in
TestTheQingdaoRegression — at 13:18 the ratchet says UNDECIDED (the observed max
is sitting inside the bucket with heating left, so the book is pricing the transit
rather than the outcome), and at 14:00 it says LOCKED_WIN outright.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# executor imports py_clob_client_v2 at module load; guard so tests run headless.
for mod in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["py_clob_client_v2.client"].ClobClient = object
_ct = sys.modules["py_clob_client_v2.clob_types"]
for _n in ("MarketOrderArgsV2", "OrderArgsV2", "OrderType", "ApiCreds",
           "BalanceAllowanceParams", "AssetType"):
    if not hasattr(_ct, _n):
        setattr(_ct, _n, object)

import intraday as I

# The real market, on the settlement ruler. Qingdao settles on whole °C, so
# resolved_extreme_f rounds half-away-from-zero and converts: 30.3°C -> 30 ->
# 86.0°F (inside the bucket), 30.8°C -> 31 -> 87.8°F (clear above it).
QINGDAO_BUCKET = (85.6, 86.4)
OBS_AT_1318 = 86.0     # running max 30.3°C when the stop actually fired
OBS_AT_1400 = 87.8     # running max 30.8°C, the day's peak


def state(observed, hour, bucket=QINGDAO_BUCKET, is_high=True, side="NO",
          day_over=False):
    """Every input injected, so no test touches the network or the clock."""
    return I.settlement_state("Qingdao", "2026-08-11", is_high,
                              bucket[0], bucket[1], side,
                              observed=observed, hour=hour, day_over=day_over)


class TestTheRatchetDecidesWhatPriceCannot:
    def test_a_max_already_clear_above_the_bucket_is_locked_won(self):
        """The irreversible case. A maximum cannot come back down, so once the
        observed max is above the bucket a NO settles at $1 and no quote matters."""
        assert state(OBS_AT_1400, hour=14.0)["state"] == I.LOCKED_WIN

    def test_a_max_short_of_the_bucket_with_the_day_spent_is_locked_won(self):
        """The other way to win: the rise is over and we never reached the bucket."""
        assert state(80.0, hour=19.0)["state"] == I.LOCKED_WIN

    def test_a_max_short_of_the_bucket_with_heating_left_is_undecided(self):
        """Same reading, hours earlier — it can still climb into the bucket."""
        assert state(80.0, hour=9.0)["state"] == I.UNDECIDED

    def test_a_max_inside_the_bucket_with_the_day_spent_is_locked_lost(self):
        """The ONLY state in which selling a NO at a loss is rational."""
        assert state(86.0, hour=19.0)["state"] == I.LOCKED_LOSS

    def test_a_max_inside_the_bucket_with_heating_left_is_undecided(self):
        """It may still climb out of the bucket, which is exactly what Qingdao did."""
        assert state(86.0, hour=12.0)["state"] == I.UNDECIDED

    def test_a_minimum_ratchets_the_other_way(self):
        """For a LOW market the min only falls, so 'clear below' is the locked win."""
        assert state(70.0, hour=9.0, is_high=False,
                     bucket=(75.0, 76.0))["state"] == I.LOCKED_WIN

    def test_a_yes_position_mirrors_the_verdict(self):
        """Same physics, opposite book. A max clear above the bucket kills a YES."""
        assert state(OBS_AT_1400, hour=14.0, side="YES")["state"] == I.LOCKED_LOSS

    def test_an_open_ended_bucket_still_resolves(self):
        """'X or above' markets carry a None edge; it must read as infinity, not
        crash and not silently become a numeric bound."""
        assert state(95.0, hour=14.0, bucket=(90.0, None))["state"] == I.LOCKED_LOSS


class TestItFailsClosed:
    """Every unknown must read as 'do not sell at a loss'. UNKNOWN is not
    permission — it is the absence of evidence, and the downside of holding is
    bounded at the stake while the downside of selling a locked winner is not."""

    def test_a_missing_observation_is_unknown(self):
        assert state(None, hour=14.0)["state"] == I.UNKNOWN

    def test_a_day_that_has_not_started_is_unknown(self):
        assert state(86.0, hour=None, day_over=False)["state"] == I.UNKNOWN

    def test_unknown_is_never_locked_loss(self):
        """The gate keys off LOCKED_LOSS exactly, so this is the safety property."""
        for st in (state(None, hour=14.0), state(86.0, hour=None)):
            assert st["state"] != I.LOCKED_LOSS


class TestTheQingdaoRegression:
    """The two readings that decide whether this can recur."""

    def test_at_1318_local_the_gate_refuses_the_loss_exit(self):
        """THE regression. The stop fired here on a mid of 0.295. The observed max
        had rounded to exactly 30°C — inside the bucket — with the 14:00 peak still
        ahead, so the outcome was genuinely open and the book was pricing the
        transit. Anything other than UNDECIDED here and the bug is back."""
        st = state(OBS_AT_1318, hour=13.3)
        assert st["state"] == I.UNDECIDED
        assert st["state"] != I.LOCKED_LOSS   # the gate blocks the sale

    def test_by_1400_local_the_position_is_provably_won(self):
        """42 minutes after the stop fired, the max cleared the bucket for good."""
        assert state(OBS_AT_1400, hour=14.0)["state"] == I.LOCKED_WIN

    def test_the_peak_passed_threshold_keeps_the_afternoon_protected(self):
        """EXIT_PEAK_PASSED_FRACTION must not declare the day spent before the
        diurnal peak. If it did, a max sitting in the bucket at 13:00 would read
        LOCKED_LOSS and the stop would be re-legalised under a new name."""
        for hour in (11.0, 12.0, 13.0, 13.3, 14.0):
            assert state(86.0, hour=hour)["state"] == I.UNDECIDED, (
                f"hour {hour} must stay undecided while the max can still rise")


class TestTheGateWiring:
    """The predicate the executor actually calls, isolated from the network."""

    class _Obs:
        def __init__(self):
            self.rules = []

        def record_rule(self, *a, **k):
            self.rules.append((a, k))

    def _executor(self):
        import executor as E
        return E.Executor.__new__(E.Executor)

    @pytest.mark.parametrize("st_state,expected", [
        (I.LOCKED_LOSS, True),
        (I.LOCKED_WIN, False),
        (I.UNDECIDED, False),
        (I.UNKNOWN, False),
    ])
    def test_only_locked_loss_permits_a_loss_exit(self, st_state, expected):
        ex = self._executor()
        pos = {"city": "Qingdao", "target_date": "2026-08-11", "side": "NO"}
        allowed = ex._loss_exit_allowed(
            pos, {"state": st_state, "reason": "test"}, self._Obs())
        assert allowed is expected

    def _book(self, monkeypatch, bids):
        """Point the guard at a synthetic bid book. Levels are (price, size)."""
        import executor as E
        monkeypatch.setattr(E, "estimate_sale", lambda tok, sh, force=False: (
            self._sale(bids, sh)))

    @staticmethod
    def _sale(bids, shares):
        import scanner as S
        data = {"bids": [{"price": str(p), "size": str(s)} for p, s in bids]}
        vwap, filled, exhausted = S._walk_bids(data, shares)
        best = max(p for p, _ in bids) if bids else 0.0
        slip = (best - vwap) / best if (vwap is not None and best) else None
        return {"vwap": vwap, "filled_shares": filled, "exhausted": exhausted,
                "best_bid": best, "slippage_frac": slip}

    def test_the_liquidity_guard_blocks_the_qingdao_book(self, monkeypatch):
        """THE reason this guard is slippage-based. Qingdao's book held $109 of bid
        depth against a $12.73 sale — a 9x cushion, so a 3x DEPTH test waves it
        straight through. But only 5.76 of the 15.15 shares sat at the $0.28 top
        bid and the rest of the book was far below, so the fill lands ~7-8% under
        the quote the stop triggered on. That is what must block it."""
        self._book(monkeypatch, [(0.28, 5.76), (0.20, 10.0), (0.10, 200.0)])
        ok, detail = self._executor()._exit_liquidity_ok("tok", 15.151514)
        assert ok is False, detail
        assert "slippage" in detail

    def test_the_liquidity_guard_blocks_an_exhausted_book(self, monkeypatch):
        """Not enough resting size to fill at all — a partial sweep of a
        collapsing book is the worst of both outcomes."""
        self._book(monkeypatch, [(0.28, 5.0)])
        ok, detail = self._executor()._exit_liquidity_ok("tok", 15.151514)
        assert ok is False, detail
        assert "EXHAUSTED" in detail

    def test_the_liquidity_guard_passes_a_deep_book_at_the_quote(self, monkeypatch):
        """It must not become a blanket ban on exiting — a genuinely dead position
        on a book that can absorb it still needs to be sellable."""
        self._book(monkeypatch, [(0.28, 500.0), (0.27, 500.0)])
        ok, detail = self._executor()._exit_liquidity_ok("tok", 15.151514)
        assert ok is True, detail

    def test_a_small_walk_inside_the_cap_still_passes(self, monkeypatch):
        """The cap is 3%, not 0% — eating one thin top level is normal and fine."""
        self._book(monkeypatch, [(0.28, 14.0), (0.277, 500.0)])
        ok, detail = self._executor()._exit_liquidity_ok("tok", 15.151514)
        assert ok is True, detail

    def test_an_unreadable_book_fails_closed(self, monkeypatch):
        import executor as E
        monkeypatch.setattr(E, "estimate_sale", lambda tok, sh, force=False: None)
        ok, detail = self._executor()._exit_liquidity_ok("tok", 15.0)
        assert ok is False
        assert "unreadable" in detail


class TestTheDeployedDefaults:
    def test_the_percentage_stop_ships_disabled(self):
        """Every threshold scored net-negative against the 10 resolved positions;
        the deployed default must reflect that, not the old 0.50."""
        import config as C
        assert C.ENABLE_STOP_LOSS is False

    def test_the_physics_gate_ships_enabled(self):
        import config as C
        assert C.ENABLE_PHYSICS_EXIT_GATE is True

    def test_the_post_date_salvage_survives_the_stop_being_off(self):
        """Different animal, own switch: it fires only once the temperature is
        realized and recovers cents off a position already heading to $0."""
        import config as C
        assert C.ENABLE_POST_DATE_SALVAGE is True

    def test_the_slippage_cap_would_have_blocked_the_qingdao_fill(self):
        """The realized fill was $0.2578 against a $0.28 top bid. Whatever the cap
        is tuned to, it must sit below that 7.93%."""
        import config as C
        realized = (0.28 - 0.2578) / 0.28
        assert C.EXIT_MAX_SLIPPAGE_FRAC < realized, (
            f"cap {C.EXIT_MAX_SLIPPAGE_FRAC:.2%} would have permitted the "
            f"{realized:.2%} slippage Qingdao actually paid")
