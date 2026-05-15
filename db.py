import sqlite3
import os
import threading
import logging
from config import STARTING_BANKROLL
import config as _cfg

DB_PATH = os.path.abspath(_cfg.DB_PATH)
from datetime import datetime, timezone

# Module-level lock: serialises all write operations so concurrent bot thread
# and Flask thread never interleave mid-transaction on the bankroll ledger.
_write_lock = threading.Lock()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                side TEXT,
                size_usdc REAL,
                fill_price REAL,
                model_prob REAL,
                edge REAL,
                pnl REAL,
                status TEXT,
                entry_time TEXT,
                exit_time TEXT,
                exit_reason TEXT,
                resolution_logged BOOLEAN DEFAULT FALSE,
                is_high BOOLEAN,
                city TEXT,
                target_date TEXT
            )
        ''')
        for ddl in [
            "ALTER TABLE trades ADD COLUMN resolution_logged BOOLEAN DEFAULT FALSE",
            "ALTER TABLE trades ADD COLUMN is_high BOOLEAN",
            "ALTER TABLE trades ADD COLUMN city TEXT",
            "ALTER TABLE trades ADD COLUMN target_date TEXT",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

        conn.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                token_id TEXT,
                side TEXT,
                entry_price REAL,
                size_usdc REAL,
                entry_time TEXT,
                question TEXT,
                is_high BOOLEAN,
                city TEXT,
                target_date TEXT,
                end_date_iso TEXT
            )
        ''')
        for ddl in [
            "ALTER TABLE positions ADD COLUMN question TEXT",
            "ALTER TABLE positions ADD COLUMN is_high BOOLEAN",
            "ALTER TABLE positions ADD COLUMN city TEXT",
            "ALTER TABLE positions ADD COLUMN target_date TEXT",
            "ALTER TABLE positions ADD COLUMN end_date_iso TEXT",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

        conn.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                market_id TEXT,
                city TEXT,
                target_date TEXT,
                bucket_low REAL,
                bucket_high REAL,
                model_prob REAL,
                yes_price REAL,
                no_price REAL,
                edge REAL,
                confidence REAL,
                model_spread REAL,
                ensemble_std REAL,
                raw_models TEXT,
                signal_type TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS model_accuracy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT,
                target_date TEXT,
                model TEXT,
                forecast_temp REAL,
                actual_temp REAL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                event TEXT,
                amount REAL,
                balance REAL,
                trade_id INTEGER
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                market_id TEXT,
                question TEXT,
                skip_reason TEXT,
                hours_to_res REAL,
                volume REAL,
                end_date TEXT
            )
        ''')
        try:
            conn.execute("ALTER TABLE scan_log ADD COLUMN end_date TEXT")
        except sqlite3.OperationalError:
            pass

        conn.execute('''
            CREATE TABLE IF NOT EXISTS resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                resolved_at TEXT,
                outcome TEXT,
                actual_value REAL,
                model_prob_at_entry REAL,
                pnl REAL,
                side TEXT,
                won BOOLEAN,
                brier REAL,
                city TEXT,
                target_date TEXT
            )
        ''')
        for ddl in [
            "ALTER TABLE resolutions ADD COLUMN side TEXT",
            "ALTER TABLE resolutions ADD COLUMN won BOOLEAN",
            "ALTER TABLE resolutions ADD COLUMN brier REAL",
            "ALTER TABLE resolutions ADD COLUMN city TEXT",
            "ALTER TABLE resolutions ADD COLUMN target_date TEXT",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

        conn.execute('''
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                kind TEXT,
                severity TEXT,
                message TEXT
            )
        ''')

        # Indexes — safe to re-run; IF NOT EXISTS is idempotent
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bankroll_id ON bankroll(id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_id ON notifications(id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status_exit ON trades(status, exit_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts_city ON signals(timestamp, city)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_model_acc_model_date ON model_accuracy(model, target_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_log_ts ON scan_log(id DESC)")

        conn.commit()

    rows = fetch_query("SELECT COUNT(*) as c FROM bankroll")
    if rows[0]["c"] == 0:
        _seed_bankroll(STARTING_BANKROLL)

    logging.info("Database initialized successfully.")


def _seed_bankroll(starting_amount):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) VALUES (?, ?, ?, ?, ?)",
            (now, "SEED", starting_amount, starting_amount, None)
        )
        conn.commit()


def get_current_bankroll():
    rows = fetch_query("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1")
    if rows:
        return rows[0]["balance"]
    return STARTING_BANKROLL


def get_portfolio_state():
    available_cash = get_current_bankroll()
    positions = fetch_query("SELECT SUM(size_usdc) as locked FROM positions")
    locked = positions[0]["locked"] if positions and positions[0]["locked"] else 0.0
    total_equity = available_cash + locked
    return {
        "available_cash": available_cash,
        "locked_cash": locked,
        "total_equity": total_equity
    }


def update_bankroll(event, amount, trade_id=None):
    """Thread-safe bankroll ledger update. Reads current balance and appends
    a new row inside a single lock so concurrent closes can't double-read."""
    with _write_lock:
        current = get_current_bankroll()
        new_balance = current + amount
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) VALUES (?, ?, ?, ?, ?)",
                (now, event, amount, new_balance, trade_id)
            )
            conn.commit()
    return new_balance


def close_position_atomic(pos_id, market_id, side, pnl_dollars, size_usdc, exit_reason):
    """Delete position row and update trade record in a single transaction.
    Returns True on success, False if the position was already gone (idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM positions WHERE id=?", (pos_id,))
            if cur.rowcount == 0:
                return False  # already closed by another thread
            cur.execute(
                "UPDATE trades SET status=?, exit_time=?, exit_reason=?, pnl=? "
                "WHERE market_id=? AND status='OPEN' AND side=?",
                ("CLOSED", now, exit_reason, pnl_dollars, market_id, side)
            )
            conn.commit()
        # Bankroll update done outside the SQLite transaction but still inside _write_lock
        current = get_current_bankroll()
        new_balance = current + size_usdc + pnl_dollars
        now2 = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) VALUES (?, ?, ?, ?, ?)",
                (now2, "TRADE_EXIT", size_usdc + pnl_dollars, new_balance, None)
            )
            conn.commit()
    return True


def execute_query(query, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor.lastrowid


def fetch_query(query, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_daily_pnl():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    query = "SELECT SUM(pnl) as tpnl FROM trades WHERE exit_time >= ? AND status='CLOSED'"
    rows = fetch_query(query, (f"{today}T00:00:00",))
    return rows[0]["tpnl"] if rows and rows[0]["tpnl"] is not None else 0.0


def get_open_position(market_id):
    rows = fetch_query("SELECT * FROM positions WHERE market_id=?", (market_id,))
    return rows[0] if rows else None


def purge_old_signals(keep_days=60):
    """Delete signal rows older than keep_days. Called once per day to prevent table bloat."""
    execute_query(
        "DELETE FROM signals WHERE timestamp < datetime('now', ?)",
        (f"-{keep_days} days",)
    )


def purge_old_scan_log(keep_days=14):
    """Delete scan_log rows older than keep_days."""
    execute_query(
        "DELETE FROM scan_log WHERE timestamp < datetime('now', ?)",
        (f"-{keep_days} days",)
    )


def add_notification(kind, message, severity="info"):
    """Append a notification row for the dashboard feed.

    kind:     short machine label, e.g. 'error', 'daily_summary', 'circuit_breaker'.
    severity: 'info' | 'warning' | 'error' — drives dashboard styling.
    Failures here are swallowed: a notification must never break the bot loop.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        execute_query(
            "INSERT INTO notifications (timestamp, kind, severity, message) VALUES (?, ?, ?, ?)",
            (now, kind, severity, message)
        )
    except Exception as e:
        logging.error(f"Failed to write notification ({kind}): {e}")


def get_recent_notifications(limit=100):
    """Most recent notifications, newest first."""
    return fetch_query(
        "SELECT id, timestamp, kind, severity, message FROM notifications "
        "ORDER BY id DESC LIMIT ?",
        (limit,)
    )


def purge_old_notifications(keep_days=30):
    """Delete notification rows older than keep_days."""
    execute_query(
        "DELETE FROM notifications WHERE timestamp < datetime('now', ?)",
        (f"-{keep_days} days",)
    )
