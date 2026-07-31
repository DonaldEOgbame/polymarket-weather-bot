"""Acceptance tests for the monitor-cycle position trail.

What this table is for, stated once so the tests below read as checks on a
claim rather than as coverage:

signal_trail records EVALUATIONS, and a market stops being evaluated once it
drops out of the scan candidate set. On the eight historical losers the trail
watched a median of 6.5 hours after entry and was blind for a median of 14 hours
before close (Houston #68: watched 1.0h, blind 32.5h). So it is not known
whether those positions declined gradually or jumped to zero at settlement,
whether Chongqing's -56.2% was a real mid move or bid-side spread on a thin
book, and every stop-loss constant in config.py was replayed against data that
does not cover the window a stop would fire in.

The tests therefore check the three properties that make those questions
answerable in future: a row every cycle with no holes, bid/ask/mid stored
separately rather than as one derived number, and per-rule observed/threshold
pairs that let a different threshold be replayed without re-deriving anything.

NOTE: none of this can be backfilled. History before this change stays
unanswerable — see test_history_before_this_change_is_not_recoverable.
"""
import importlib
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

for _m in ("py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
sys.modules["py_clob_client_v2.client"].ClobClient = object
_ct = sys.modules["py_clob_client_v2.clob_types"]
for _n in ("MarketOrderArgsV2", "OrderType", "ApiCreds", "BalanceAllowanceParams", "AssetType"):
    if not hasattr(_ct, _n):
        setattr(_ct, _n, object)

import db as dbmod
import executor as ex
from executor import Executor, PositionObservation

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

POS = {
    "id": 7,
    "market_id": "0xabc",
    "token_id": "tok_7",
    "side": "NO",
    "city": "Houston",
    "target_date": "2026-08-02",
    "entry_price": 0.64,
    "size_usdc": 2.0,
    "shares": 3.125,
    "entry_time": "2026-08-01T10:00:00+00:00",
}


@pytest.fixture
def trail_db(tmp_path, monkeypatch):
    """A real SQLite file. Patched onto the live db module rather than reloaded:
    executor holds a direct reference to db.log_position_trail, and a reload
    would leave it writing to the previous path."""
    monkeypatch.setattr(dbmod, "DB_PATH", str(tmp_path / "bot.db"))
    dbmod.init_db()
    return dbmod


def _book(ask, bid, ask_top=None, bid_top=None, reachable=True):
    """Patch the book the observation will read."""
    def price_status(_token):
        return ask, bid, reachable
    return price_status


def _observe(monkeypatch, now, ask, bid, pos=None, ask_top=40.0, bid_top=25.0,
             ask_depth=90.0, bid_depth=60.0):
    monkeypatch.setattr(ex, "get_realtime_price_status", _book(ask, bid))
    monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda t: (ask_depth, bid_depth))
    monkeypatch.setattr(ex, "get_orderbook_top_size", lambda t: (ask_top, bid_top))
    monkeypatch.setattr(ex, "get_gamma_mid_price", lambda *a, **k: None)
    return PositionObservation(pos or POS, now)


class TestOneRowPerCycle:
    def test_24h_position_yields_288_rows_with_no_gap_over_two_cycles(
            self, trail_db, monkeypatch):
        """The stated acceptance: ~288 rows for a 24h hold, no gap > 2 cycles.

        Uses the real persist path, one call per simulated 5-minute cycle."""
        for i in range(288):
            obs = _observe(monkeypatch, T0 + timedelta(minutes=5 * i),
                           ask=0.66, bid=0.62)
            assert obs.persist() is not None

        rows = trail_db.get_position_trail(position_id=7)
        assert len(rows) == 288

        stamps = [datetime.fromisoformat(r["timestamp"]) for r in rows]
        assert stamps == sorted(stamps), "path must come back in time order"
        gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(stamps, stamps[1:])]
        assert max(gaps) <= 10.0, f"gap longer than two 5-minute cycles: {max(gaps)}min"
        assert (stamps[-1] - stamps[0]) == timedelta(hours=23, minutes=55)

    def test_row_survives_an_exit_check_that_raises(self, trail_db, monkeypatch):
        """A cycle that throws is exactly when the observation matters most, so
        persistence sits in a `finally`, not after the exit check."""
        e = Executor.__new__(Executor)
        e._loss_streak = {}
        e._exit_locks = {}
        import threading
        e._exit_locks_guard = threading.Lock()

        monkeypatch.setattr(ex, "fetch_query",
                            lambda q, p=(): [dict(POS)] if "FROM positions" in q else [])
        monkeypatch.setattr(ex, "get_position_by_id", lambda i: dict(POS))
        monkeypatch.setattr(ex, "get_realtime_price_status", _book(0.66, 0.62))
        monkeypatch.setattr(ex, "get_orderbook_depth_usd", lambda t: (90.0, 60.0))
        monkeypatch.setattr(ex, "get_orderbook_top_size", lambda t: (40.0, 25.0))
        monkeypatch.setattr(ex, "get_gamma_mid_price", lambda *a, **k: None)

        def boom(pos, obs=None):
            raise RuntimeError("book fetch exploded mid-decision")
        monkeypatch.setattr(e, "_check_exit_for_position", boom)

        with pytest.raises(RuntimeError):
            e.check_exits()

        rows = trail_db.get_position_trail(position_id=7)
        assert len(rows) == 1
        assert rows[0]["mid"] == pytest.approx(0.64)

    def test_unreadable_book_still_writes_a_row(self, trail_db, monkeypatch):
        """Blindness must be recorded as blindness. A missing row is
        indistinguishable from a monitor that never ran."""
        monkeypatch.setattr(ex, "get_realtime_price_status",
                            lambda t: (0.0, 0.0, False))
        monkeypatch.setattr(ex, "get_gamma_mid_price", lambda *a, **k: None)
        obs = PositionObservation(POS, T0)
        assert obs.persist() is not None

        row = trail_db.get_position_trail(position_id=7)[0]
        assert row["price_source"] == "unreadable"
        assert row["best_bid"] is None and row["best_ask"] is None and row["mid"] is None

    def test_gamma_fallback_is_labelled_not_disguised(self, trail_db, monkeypatch):
        """A Gamma mark has no book behind it. Recording it as a quote is the
        phantom-$0.999 failure class."""
        monkeypatch.setattr(ex, "get_realtime_price_status",
                            lambda t: (0.0, 0.0, False))
        monkeypatch.setattr(ex, "get_gamma_mid_price", lambda *a, **k: 0.55)
        obs = PositionObservation(POS, T0)
        obs.persist()

        row = trail_db.get_position_trail(position_id=7)[0]
        assert row["price_source"] == "gamma_fallback"
        assert row["mid"] == pytest.approx(0.55)
        assert row["best_bid"] is None, "a fallback mark is not a bid"


class TestBidAskMidStoredSeparately:
    def test_all_three_prices_and_the_spread_are_stored(self, trail_db, monkeypatch):
        """The bid/mid distinction IS the unresolved question. One derived
        number would decide it by accident."""
        obs = _observe(monkeypatch, T0, ask=0.70, bid=0.50)
        obs.persist()

        row = trail_db.get_position_trail(position_id=7)[0]
        assert row["best_ask"] == pytest.approx(0.70)
        assert row["best_bid"] == pytest.approx(0.50)
        assert row["mid"] == pytest.approx(0.60)
        assert row["spread_fraction"] == pytest.approx((0.70 - 0.50) / 0.60)
        assert row["bid_top_size"] == pytest.approx(25.0)
        assert row["ask_top_size"] == pytest.approx(40.0)

    def test_drawdown_is_computable_without_a_join(self, trail_db, monkeypatch):
        """Entry price and stake are denormalised deliberately."""
        obs = _observe(monkeypatch, T0, ask=0.40, bid=0.30)
        obs.persist()

        row = trail_db.get_position_trail(position_id=7)[0]
        assert row["entry_price"] == pytest.approx(0.64)
        assert row["stake_usdc"] == pytest.approx(2.0)
        # mid 0.35 against entry 0.64 on 3.125 shares
        assert row["unrealized_pnl_mid"] == pytest.approx((0.35 - 0.64) * 3.125)
        assert row["unrealized_pnl_frac_mid"] == pytest.approx((0.35 - 0.64) / 0.64)
        # and the bid basis, which is a materially different number here
        assert row["unrealized_pnl_frac_bid"] == pytest.approx((0.30 - 0.64) / 0.64)
        assert row["unrealized_pnl_frac_bid"] < row["unrealized_pnl_frac_mid"]

    def test_hours_to_resolution_is_stored(self, trail_db, monkeypatch):
        obs = _observe(monkeypatch, T0, ask=0.66, bid=0.62)
        obs.persist()
        row = trail_db.get_position_trail(position_id=7)[0]
        # T0 = 2026-08-01 12:00Z, target 2026-08-02 23:59Z
        assert row["hours_to_resolution"] == pytest.approx(35.98, abs=0.02)
        assert row["hold_minutes"] == pytest.approx(120.0)


class TestStopLossIsReplayable:
    def test_every_rule_appears_every_cycle(self, trail_db, monkeypatch):
        """Including the ones the exit path never reached — a cycle inside the
        30-minute hold, or after the target date, must not go silent."""
        obs = _observe(monkeypatch, T0, ask=0.66, bid=0.62)
        trail_id = obs.persist()

        rules = trail_db.get_position_trail_rules([trail_id])[trail_id]
        keyed = {(r["rule"], r["basis"]) for r in rules}
        assert ("stop_loss", "mid") in keyed
        assert ("stop_loss", "bid") in keyed
        assert ("take_profit", "bid") in keyed
        assert ("sustained_loss", "mid") in keyed
        assert ("thesis_break", None) in keyed

    def test_thesis_break_is_never_guessed(self, trail_db, monkeypatch):
        """It needs a live ensemble re-run. A cycle that did not do one records
        the rule as unevaluated rather than inventing an edge."""
        obs = _observe(monkeypatch, T0, ask=0.66, bid=0.62)
        trail_id = obs.persist()
        tb = [r for r in trail_db.get_position_trail_rules([trail_id])[trail_id]
              if r["rule"] == "thesis_break"][0]
        assert tb["evaluated"] == 0
        assert tb["observed"] is None
        assert "ensemble" in tb["detail"]

    def test_any_threshold_replays_to_fire_or_no_fire_on_both_bases(
            self, trail_db, monkeypatch):
        """THE acceptance criterion. Walk a position from entry into a
        collapse, then replay four stop levels off the stored rows alone."""
        # mid drifts 0.64 -> 0.20; the bid trails it by a wide spread throughout,
        # which is exactly the configuration where bid and mid disagree.
        path = [(0.66, 0.62), (0.60, 0.52), (0.52, 0.40), (0.40, 0.26), (0.24, 0.16)]
        for i, (ask, bid) in enumerate(path):
            _observe(monkeypatch, T0 + timedelta(minutes=5 * i), ask=ask, bid=bid).persist()

        rows = trail_db.get_position_trail(position_id=7)
        assert len(rows) == 5

        def first_fire(basis, pct):
            """Replay a stop of `pct` on `basis` using ONLY stored columns."""
            col = f"unrealized_pnl_frac_{basis}"
            for r in rows:
                if r[col] is not None and r[col] <= -pct:
                    return r["timestamp"]
            return None

        # mid path fractions: -0.0, -0.125, -0.281, -0.484, -0.688
        assert first_fire("mid", 0.50) == rows[4]["timestamp"]
        assert first_fire("mid", 0.60) == rows[4]["timestamp"]
        assert first_fire("mid", 0.30) == rows[3]["timestamp"]
        assert first_fire("mid", 0.90) is None
        # bid path fires EARLIER at every level — the bid/mid question, visible
        assert first_fire("bid", 0.50) == rows[3]["timestamp"]
        assert first_fire("bid", 0.30) == rows[2]["timestamp"]
        # and the two bases genuinely disagree on when a 50% stop fires
        assert first_fire("bid", 0.50) != first_fire("mid", 0.50)

    def test_stored_rule_row_agrees_with_a_replay_of_the_same_threshold(
            self, trail_db, monkeypatch):
        """The per-rule `fired` flag and the stored fraction must not drift
        apart; if they can, one of them is decoration."""
        obs = _observe(monkeypatch, T0, ask=0.24, bid=0.16)
        trail_id = obs.persist()
        row = trail_db.get_position_trail(position_id=7)[0]
        rules = {(r["rule"], r["basis"]): r
                 for r in trail_db.get_position_trail_rules([trail_id])[trail_id]}

        for basis in ("mid", "bid"):
            r = rules[("stop_loss", basis)]
            observed = row[f"unrealized_pnl_frac_{basis}"]
            assert r["observed"] == pytest.approx(observed)
            assert bool(r["fired"]) == (observed <= r["threshold"])

    def test_fired_means_the_condition_held_not_that_the_bot_acted(
            self, trail_db, monkeypatch):
        """Separate columns because they answer different questions: `fired` is
        the counterfactual, `exit_rule_fired` is the history."""
        obs = _observe(monkeypatch, T0, ask=0.24, bid=0.16)
        trail_id = obs.persist()
        row = trail_db.get_position_trail(position_id=7)[0]
        sl = [r for r in trail_db.get_position_trail_rules([trail_id])[trail_id]
              if r["rule"] == "stop_loss" and r["basis"] == "mid"][0]
        assert sl["fired"] == 1          # a 50% stop WOULD have fired here
        assert row["exit_fired"] == 0    # but nothing exited: the path never ran
        assert row["exit_rule_fired"] is None


class TestExitPathRecordsWhatItActuallyTested:
    def _exec(self):
        e = Executor.__new__(Executor)
        e._loss_streak = {}
        return e

    def test_fast_take_profit_records_and_marks_the_firing_rule(
            self, trail_db, monkeypatch):
        e = self._exec()
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.99, 0.99))
        monkeypatch.setattr(e, "_close_position", lambda *a, **k: None)

        obs = _observe(monkeypatch, T0, ask=0.99, bid=0.99,
                       pos={**POS, "target_date": "2100-01-01"})
        e._check_exit_for_position({**POS, "target_date": "2100-01-01"}, obs)
        trail_id = obs.persist()

        row = trail_db.get_position_trail(position_id=7)[0]
        assert row["exit_rule_fired"] == "take_profit"
        assert row["exit_fired"] == 1
        tp = [r for r in trail_db.get_position_trail_rules([trail_id])[trail_id]
              if r["rule"] == "take_profit"][0]
        assert tp["evaluated"] == 1 and tp["fired"] == 1

    def test_rules_not_reached_are_marked_unevaluated(self, trail_db, monkeypatch):
        """Inside the 30-minute hold the exit path tests take-profit and returns.
        The other rules are still computed — flagged as not tested live."""
        e = self._exec()
        # Entered 5 minutes ago on the real clock — the exit path's 30-minute
        # hold reads datetime.now(), so this cannot be faked with T0.
        entered = datetime.now(timezone.utc) - timedelta(minutes=5)
        fresh = {**POS, "entry_time": entered.isoformat(), "target_date": "2100-01-01"}
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.66, 0.62))
        obs = _observe(monkeypatch, T0 + timedelta(minutes=5), ask=0.66, bid=0.62,
                       pos=fresh)
        e._check_exit_for_position(fresh, obs)
        trail_id = obs.persist()

        rules = {(r["rule"], r["basis"]): r
                 for r in trail_db.get_position_trail_rules([trail_id])[trail_id]}
        assert rules[("take_profit", "bid")]["evaluated"] == 1
        assert rules[("stop_loss", "mid")]["evaluated"] == 0
        assert rules[("stop_loss", "mid")]["fired"] == 0  # still computed
        assert rules[("sustained_loss", "mid")]["evaluated"] == 0

    def test_exactly_one_row_per_rule_and_basis_per_cycle(self, trail_db, monkeypatch):
        """take_profit is tested twice in a full cycle — fast path, then against
        exit_fill. Two rows would double any `WHERE rule='take_profit'` count."""
        e = self._exec()
        # _check_exit_for_position reads the real clock for the 30-minute hold,
        # so the entry must be genuinely in the past, not just before T0.
        old = {**POS, "entry_time": "2026-01-01T00:00:00+00:00",
               "target_date": "2100-01-01"}
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.66, 0.62))
        monkeypatch.setattr(ex, "fetch_query", lambda *a, **k: [])
        obs = _observe(monkeypatch, T0, ask=0.66, bid=0.62, pos=old)
        e._check_exit_for_position(old, obs)
        trail_id = obs.persist()

        rules = trail_db.get_position_trail_rules([trail_id])[trail_id]
        keys = [(r["rule"], r["basis"]) for r in rules]
        assert len(keys) == len(set(keys)), f"duplicate rule rows: {keys}"
        assert set(keys) == {("stop_loss", "mid"), ("stop_loss", "bid"),
                             ("take_profit", "bid"), ("sustained_loss", "mid"),
                             ("thesis_break", None)}
        # the main-path record won, and it is the one with the live evaluation
        tp = [r for r in rules if r["rule"] == "take_profit"][0]
        assert tp["evaluated"] == 1
        assert "Gamma fallback" in tp["detail"]

    def test_unlogged_caller_does_not_break_the_exit_path(self, monkeypatch):
        """_check_exit_for_position is called directly from tests and scripts.
        Those calls must not need a DB or write a meaningless row."""
        e = self._exec()
        monkeypatch.setattr(ex, "get_realtime_price", lambda *a: (0.99, 0.99))
        fired = []
        monkeypatch.setattr(e, "_close_position",
                            lambda pos, pnl_dollars, exit_reason: fired.append(exit_reason))
        e._check_exit_for_position({**POS, "target_date": "2100-01-01"})
        assert len(fired) == 1


class TestTopOfBookParsing:
    def test_best_price_level_not_index_zero(self):
        """The CLOB returns asks DESCENDING and bids ASCENDING — index 0 is the
        WORST price on both sides. Same trap that inverted the order-book read."""
        from scanner import _top_of_book_size
        book = {
            "asks": [{"price": "0.90", "size": "5"}, {"price": "0.70", "size": "12"}],
            "bids": [{"price": "0.10", "size": "7"}, {"price": "0.50", "size": "31"}],
        }
        ask_top, bid_top = _top_of_book_size(book)
        assert ask_top == 12.0   # size at the MINIMUM ask
        assert bid_top == 31.0   # size at the MAXIMUM bid

    def test_orders_at_the_same_best_price_are_summed(self):
        from scanner import _top_of_book_size
        book = {
            "asks": [{"price": "0.70", "size": "4"}, {"price": "0.70", "size": "6"}],
            "bids": [{"price": "0.50", "size": "2"}],
        }
        assert _top_of_book_size(book)[0] == 10.0

    def test_malformed_levels_and_empty_sides_are_survivable(self):
        from scanner import _top_of_book_size
        assert _top_of_book_size({}) == (0.0, 0.0)
        assert _top_of_book_size({"asks": [{"price": "x", "size": "1"}]}) == (0.0, 0.0)


class TestRetention:
    def test_floor_is_enforced_not_advised(self, tmp_path, monkeypatch):
        """A shortened retention is how the skip-signal calibration sample was
        lost. Here it is clamped rather than range-checked."""
        monkeypatch.setenv("POSITION_TRAIL_RETENTION_DAYS", "7")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
        import config
        importlib.reload(config)
        try:
            assert config.POSITION_TRAIL_RETENTION_DAYS == 90
        finally:
            monkeypatch.delenv("POSITION_TRAIL_RETENTION_DAYS", raising=False)
            importlib.reload(config)

    def test_purge_drops_old_rows_and_their_rule_rows(self, trail_db, monkeypatch):
        old = datetime.now(timezone.utc) - timedelta(days=120)
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        old_id = _observe(monkeypatch, old, ask=0.66, bid=0.62).persist()
        new_id = _observe(monkeypatch, recent, ask=0.66, bid=0.62).persist()

        trail_db.purge_old_position_trail(90)

        surviving = {r["id"] for r in trail_db.get_position_trail(position_id=7)}
        assert surviving == {new_id}
        assert trail_db.get_position_trail_rules([old_id]) == {}
        assert trail_db.get_position_trail_rules([new_id])


class TestNoBackfill:
    def test_history_before_this_change_is_not_recoverable(self, trail_db):
        """Stated as a test so it cannot be quietly forgotten.

        There is no source to reconstruct from. Polymarket serves the CURRENT
        book, not a historical one; signal_trail stopped recording each of the
        eight losers a median of 14 hours before it closed; and the trades table
        holds entry and exit only. Any 'backfill' would be interpolation between
        two points across the exact interval in question — which is the thing
        being measured. The table starts empty and earns its history forward."""
        assert trail_db.get_position_trail() == []
