"""Regression test: open_position_atomic / close_position_atomic must credit/debit
the bankroll ledger in the SAME transaction as the position and trade rows.

Previously each was split across separate sqlite3.connect()/commit() calls. A
process kill between them (OOM-kill, deploy restart — not just the graceful
SIGTERM path) would leave a position opened/closed with no matching bankroll
entry, silently corrupting available cash forever. These tests don't simulate
a crash directly (hard to do against a single connection); they instead pin
the observable contract: after a normal call, the bankroll balance always
reflects exactly one entry/exit event matching the position/trade state.
"""
import sys, os, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import importlib


def _fresh_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    db.init_db()
    return db


def test_open_position_atomic_debits_bankroll_once(monkeypatch):
    db = _fresh_db(monkeypatch)
    starting = db.get_current_bankroll()

    trade_id = db.open_position_atomic(
        market_id="0xabc", token_id="tok1", side="NO", price=0.55, size=2.0,
        now_iso="2026-07-09T10:00:00+00:00", question="q?", is_high=True,
        city="Tel Aviv", target_date="2026-07-10", model_prob=0.8, edge=0.25,
    )

    assert trade_id is not None
    balance_after = db.get_current_bankroll()
    assert balance_after == starting - 2.0

    positions = db.fetch_query("SELECT * FROM positions WHERE market_id=?", ("0xabc",))
    assert len(positions) == 1
    trades = db.fetch_query("SELECT * FROM trades WHERE id=?", (trade_id,))
    assert trades[0]["status"] == "OPEN"


def test_close_position_atomic_credits_bankroll_once(monkeypatch):
    db = _fresh_db(monkeypatch)

    trade_id = db.open_position_atomic(
        market_id="0xdef", token_id="tok2", side="NO", price=0.50, size=3.0,
        now_iso="2026-07-09T10:00:00+00:00", question="q?", is_high=False,
        city="London", target_date="2026-07-10", model_prob=0.7, edge=0.2,
    )
    balance_after_open = db.get_current_bankroll()

    pos = db.fetch_query("SELECT * FROM positions WHERE market_id=?", ("0xdef",))[0]
    closed = db.close_position_atomic(
        pos_id=pos["id"], market_id="0xdef", side="NO",
        pnl_dollars=1.5, size_usdc=3.0, exit_reason="Take Profit",
    )

    assert closed is True
    balance_after_close = db.get_current_bankroll()
    # size_usdc + pnl_dollars returned to bankroll
    assert balance_after_close == balance_after_open + 3.0 + 1.5

    positions = db.fetch_query("SELECT * FROM positions WHERE market_id=?", ("0xdef",))
    assert positions == []
    trades = db.fetch_query("SELECT * FROM trades WHERE id=?", (trade_id,))
    assert trades[0]["status"] == "CLOSED"
    assert trades[0]["pnl"] == 1.5


def test_close_position_atomic_idempotent_on_already_closed(monkeypatch):
    db = _fresh_db(monkeypatch)
    trade_id = db.open_position_atomic(
        market_id="0xghi", token_id="tok3", side="YES", price=0.60, size=1.0,
        now_iso="2026-07-09T10:00:00+00:00", question="q?", is_high=True,
        city="Madrid", target_date="2026-07-10", model_prob=0.65, edge=0.1,
    )
    pos = db.fetch_query("SELECT * FROM positions WHERE market_id=?", ("0xghi",))[0]

    first = db.close_position_atomic(
        pos_id=pos["id"], market_id="0xghi", side="YES",
        pnl_dollars=0.1, size_usdc=1.0, exit_reason="Stop Loss",
    )
    balance_after_first_close = db.get_current_bankroll()

    second = db.close_position_atomic(
        pos_id=pos["id"], market_id="0xghi", side="YES",
        pnl_dollars=0.1, size_usdc=1.0, exit_reason="Stop Loss",
    )

    assert first is True
    assert second is False
    # Bankroll must not be credited twice for the same position.
    assert db.get_current_bankroll() == balance_after_first_close
