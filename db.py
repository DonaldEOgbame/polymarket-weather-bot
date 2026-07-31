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


def _backfill_model_accuracy_is_high(conn):
    """Recover the direction of legacy model_accuracy rows, written before is_high
    was part of the key. Runs once, immediately after the column is added.

    Two passes, both non-destructive:

    1. A city/date that logged BOTH directions has exactly two distinct
       actual_temp values under one key — that collision IS the bug. The larger
       actual is the day's max and the smaller is its min, so the rows separate
       cleanly. This recovers the conflicting rows rather than dropping them.
    2. A city/date with a single actual is unambiguous only if the trades table
       shows one direction traded there; borrow it.

    Anything still unresolved keeps is_high NULL: its direction is genuinely not
    recoverable, and guessing would put a max under a min's bias fit. Those rows
    are excluded from the UNIQUE index and should be filtered out by readers."""
    try:
        pass1 = conn.execute('''
            UPDATE model_accuracy AS ma
               SET is_high = (
                   SELECT CASE WHEN ma.actual_temp >= MAX(x.actual_temp) THEN 1 ELSE 0 END
                     FROM model_accuracy x
                    WHERE x.city = ma.city AND x.target_date = ma.target_date
               )
             WHERE ma.actual_temp IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM model_accuracy y
                    WHERE y.city = ma.city AND y.target_date = ma.target_date
                    GROUP BY y.city, y.target_date
                   HAVING COUNT(DISTINCT y.actual_temp) = 2
               )
        ''').rowcount
        pass2 = conn.execute('''
            UPDATE model_accuracy AS ma
               SET is_high = (
                   SELECT MIN(t.is_high) FROM trades t
                    WHERE t.city = ma.city AND t.target_date = ma.target_date
               )
             WHERE ma.is_high IS NULL
               AND (
                   SELECT COUNT(DISTINCT t.is_high) FROM trades t
                    WHERE t.city = ma.city AND t.target_date = ma.target_date
               ) = 1
        ''').rowcount
        # Collapse exact repeats (same city/date/direction/model logged twice by
        # a re-run) keeping the newest, so the UNIQUE index can be created.
        dropped = conn.execute('''
            DELETE FROM model_accuracy
             WHERE is_high IS NOT NULL
               AND id NOT IN (
                   SELECT MAX(id) FROM model_accuracy
                    WHERE is_high IS NOT NULL
                    GROUP BY city, target_date, is_high, model
               )
        ''').rowcount
        left = conn.execute(
            "SELECT COUNT(*) FROM model_accuracy WHERE is_high IS NULL"
        ).fetchone()[0]
        logging.info(
            f"model_accuracy is_high backfill: {pass1} by actual-split, {pass2} from trades, "
            f"{dropped} duplicate rows collapsed, {left} unresolved (left NULL)"
        )
    except sqlite3.Error as e:
        # A failed backfill must not stop the bot booting — the column exists
        # either way and new rows are written correctly.
        logging.error(f"model_accuracy is_high backfill failed: {e}", exc_info=True)


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
            # Order-book $ depth at EXIT — the counterpart to ask_depth_usd/
            # bid_depth_usd logged at entry in signals. See close_position_atomic.
            "ALTER TABLE trades ADD COLUMN exit_ask_depth_usd REAL",
            "ALTER TABLE trades ADD COLUMN exit_bid_depth_usd REAL",
            # Which ledger this row belongs to: 'paper' or 'live'. Paper and live
            # are two parallel books in one file, so a simulated fill can never
            # reach a real P&L figure. DEFAULT 'paper' is the safe direction —
            # a row that slips through un-tagged reads as simulated and is
            # excluded from live money, rather than inflating it.
            "ALTER TABLE trades ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'",
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
            # Actual filled share count. Before this column, exits re-derived shares
            # from round(size_usdc/entry_price) — double rounding that could exceed
            # real holdings (CLOB rejects the sell) or book PnL on shares never held.
            "ALTER TABLE positions ADD COLUMN shares REAL",
            # See the note on trades.mode. Positions additionally cannot span
            # modes at all: the mode switch refuses while the book is open.
            "ALTER TABLE positions ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'",
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
                signal_type TEXT,
                market_spread_frac REAL
            )
        ''')
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN market_spread_frac REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN parser_version INTEGER")
        except sqlite3.OperationalError:
            pass
        # Order-book $ depth on the traded side at entry — only populated when a
        # trade actually fires (see strategy.py). Lets post-hoc analysis answer
        # "how big a position could this market have absorbed at entry" from what
        # was really resting in the book, instead of the current (unrelated) live
        # book of a market that's since moved on or resolved.
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN ask_depth_usd REAL")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN bid_depth_usd REAL")
        except sqlite3.OperationalError:
            pass

        # Immutable per-market bucket metadata. Written once, on first discovery
        # of a market_id, and never overwritten. scan_markets() looks this up
        # instead of re-deriving bucket_low/bucket_high from the question text on
        # every scan cycle — a market's bucket bounds must not silently drift over
        # its lifetime just because parse_bucket() was later fixed or changed.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                city TEXT,
                target_date TEXT,
                bucket_low REAL,
                bucket_high REAL,
                parser_version INTEGER,
                first_seen TEXT
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
        # is_high belongs in the key. Without it a city's daily MAX and MIN both
        # land on (city, target_date, model) and overwrite each other's actual —
        # 24 of 47 verified city-days in the 2026-07-31 export carried two
        # conflicting "actual" values for this reason. Per-direction bias is the
        # single largest measured effect in the forecast record (highs run cold,
        # lows run warm, see MODEL_BIAS_CORRECTIONS), so a table that cannot tell
        # the two apart cannot fit it.
        try:
            conn.execute("ALTER TABLE model_accuracy ADD COLUMN is_high INTEGER")
        except sqlite3.OperationalError:
            pass
        else:
            _backfill_model_accuracy_is_high(conn)
        # Partial UNIQUE index: legacy rows whose direction could not be recovered
        # keep is_high NULL and are excluded (SQLite also treats NULLs as distinct,
        # so they can never collide). Every new write sets is_high, so from here on
        # one row per city/date/direction/model is enforced by the database.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_acc_unique "
            "ON model_accuracy(city, target_date, is_high, model) "
            "WHERE is_high IS NOT NULL"
        )
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
        # The bankroll ledger is per-mode: the paper book keeps its simulated
        # balance while the live book tracks the real wallet, and neither is
        # ever read into the other. See the note on trades.mode.
        try:
            conn.execute("ALTER TABLE bankroll ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'")
        except sqlite3.OperationalError:
            pass
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

        # Runtime-editable settings (dashboard Settings tab). Values are TEXT
        # because config.py casts them with the same float()/int()/=="true"
        # coercions it already applies to os.getenv strings — one code path for
        # env vars and stored overrides alike.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_markets_market_id ON markets(market_id)")

        conn.commit()

    # Seed the ledger for the mode we are booting in. Per-mode, because paper
    # and live are separate books: opening the live book must not depend on
    # whether a paper book already exists, and vice versa.
    ensure_bankroll_seeded()

    logging.info("Database initialized successfully.")


def current_mode():
    """'paper' or 'live' — which ledger the bot is reading and writing now.

    Read live from config rather than captured at import: the dashboard can
    switch mode mid-process, and the very next query must land in the right
    book. Every money read/write is scoped by this."""
    return "paper" if _cfg.paper_mode() else "live"


def ensure_bankroll_seeded(mode=None, amount=None):
    """Open a ledger for `mode` if it has no rows yet. Returns the seeded
    amount, or None when the ledger already existed (the common case).

    Paper seeds at STARTING_BANKROLL so a demo has something to trade with.
    Live seeds at $0 by default: real cash is booked by the wallet sync when
    it actually lands, and inventing a balance the wallet does not hold would
    have the bot size orders it cannot pay for."""
    mode = mode or current_mode()
    rows = fetch_query("SELECT COUNT(*) AS c FROM bankroll WHERE mode = ?", (mode,))
    if rows and rows[0]["c"]:
        return None
    if amount is None:
        amount = STARTING_BANKROLL if mode == "paper" else 0.0
    _seed_bankroll(amount, mode)
    return amount


def _seed_bankroll(starting_amount, mode=None):
    mode = mode or current_mode()
    now = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, "SEED", starting_amount, starting_amount, None, mode)
            )
            conn.commit()


def get_settings():
    """All stored setting overrides as {key: str}. Empty dict if none are set.

    config.py reads the same table directly with raw sqlite3 (it cannot import
    this module — db.py imports config, so the dependency only runs one way).
    This helper exists for the dashboard API, which already imports db."""
    rows = fetch_query("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


def save_settings(values):
    """Upsert setting overrides from a {key: value} mapping. Booleans are
    normalised to 'true'/'false' so config.py's .lower() == "true" check reads
    them back correctly; everything else is str()'d.

    Returns the list of keys whose stored value actually CHANGED, so the caller
    can report (and log) exactly what the operator altered.

    Takes effect only after a process restart — every module binds config values
    at import time (from config import X), so nothing re-reads them in-flight."""
    if not values:
        return []
    now = datetime.now(timezone.utc).isoformat()
    changed = []
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            existing = {k: v for k, v in
                        conn.execute("SELECT key, value FROM settings").fetchall()}
            for key, value in values.items():
                if isinstance(value, bool):
                    stored = "true" if value else "false"
                else:
                    stored = str(value)
                if existing.get(key) == stored:
                    continue
                changed.append(key)
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, stored, now),
                )
            conn.commit()
    return changed


# Back-compat alias — save_settings is the canonical name.
set_settings = save_settings


def record_deposit(amount, note=None):
    """Append a DEPOSIT row: adds cash WITHOUT touching the P&L baseline.

    Safe by construction — realized P&L is computed only from trades.pnl
    (get_daily_pnl, the dashboard's stats), and the bankroll ledger is not an
    input to any of it. So a deposit raises available cash and leaves every
    performance figure untouched. STARTING_BANKROLL is deliberately not
    updated: it is the historical seed the ledger opened with. Use
    get_total_deposited() as the denominator for return-on-capital."""
    amount = float(amount)
    if amount <= 0:
        raise ValueError("deposit amount must be positive")
    new_balance = update_bankroll("DEPOSIT", amount)
    logging.info(f"DEPOSIT ${amount:.2f} ({note or 'no note'}) -> balance ${new_balance:.2f}")
    try:
        add_notification('deposit',
                         f'Deposit of ${amount:.2f} recorded. '
                         f'Available cash now ${new_balance:.2f}.', severity='info')
    except Exception:
        pass  # a missing notification must never lose the deposit
    return new_balance


def get_total_deposited():
    """Seed plus every recorded DEPOSIT — i.e. total capital the user has put in.

    Returns are measured against this, not STARTING_BANKROLL: a deposit adds
    cash without being profit, so dividing by the original seed would report a
    funding event as a gain."""
    mode = current_mode()
    rows = fetch_query(
        "SELECT COALESCE(SUM(amount), 0) AS deposited FROM bankroll "
        "WHERE event='DEPOSIT' AND mode = ?", (mode,)
    )
    deposited = rows[0]["deposited"] if rows else 0.0
    # Two seed spellings exist: _seed_bankroll writes 'SEED', older live ledgers
    # opened with 'LIVE_SEED'. Matching only 'SEED' would omit the original
    # capital and overstate the return.
    seed_rows = fetch_query(
        "SELECT amount FROM bankroll WHERE event IN ('SEED','LIVE_SEED') AND mode = ? "
        "ORDER BY id LIMIT 1", (mode,)
    )
    seed = seed_rows[0]["amount"] if seed_rows else (STARTING_BANKROLL if mode == "paper" else 0.0)
    return seed + (deposited or 0.0)


def get_current_bankroll():
    """Latest balance in the ACTIVE book. A paper balance must never be
    readable as live cash — the bot sizes real orders off this number."""
    mode = current_mode()
    rows = fetch_query(
        "SELECT balance FROM bankroll WHERE mode = ? ORDER BY id DESC LIMIT 1", (mode,))
    if rows:
        return rows[0]["balance"]
    return STARTING_BANKROLL if mode == "paper" else 0.0


def get_portfolio_state():
    available_cash = get_current_bankroll()
    positions = fetch_query(
        "SELECT SUM(size_usdc) as locked FROM positions WHERE mode = ?", (current_mode(),))
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
    # Bind the mode ONCE for the whole transaction: a dashboard switch
    # landing mid-write must not split a position, its trade and its cash
    # movement across two books.
    _mode = current_mode()
    with _write_lock:
        current = get_current_bankroll()
        new_balance = current + amount
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, event, amount, new_balance, trade_id, _mode)
            )
            conn.commit()
    return new_balance


def open_position_atomic(market_id, token_id, side, price, size, now_iso, question,
                          is_high, city, target_date, model_prob, edge, shares=None,
                          entry_fee=0.0):
    """Insert the position row, the trade row, and debit the bankroll all in a
    single transaction — see close_position_atomic for why the entry and exit
    sides both need this: a process kill between separate connect()/commit()
    calls (OOM-kill, deploy restart) could otherwise leave a position open
    without its cash ever being debited, silently inflating available cash.
    Returns the new trade_id."""
    # Bind the mode ONCE for the whole transaction: a dashboard switch
    # landing mid-write must not split a position, its trade and its cash
    # movement across two books.
    _mode = current_mode()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO positions (market_id, token_id, side, entry_price, size_usdc, "
                "entry_time, question, is_high, city, target_date, shares, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, token_id, side, price, size, now_iso, question,
                 1 if is_high else 0, city, target_date,
                 shares if shares is not None else (size / price if price > 0 else None),
                 _mode)
            )
            cur.execute(
                "INSERT INTO trades (market_id, side, size_usdc, fill_price, model_prob, edge, "
                "status, entry_time, is_high, city, target_date, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, side, size, price, model_prob, edge, "OPEN", now_iso,
                 1 if is_high else 0, city, target_date, _mode)
            )
            trade_id = cur.lastrowid
            row = cur.execute("SELECT balance FROM bankroll WHERE mode = ? "
                              "ORDER BY id DESC LIMIT 1", (_mode,)).fetchone()
            current = row[0] if row else STARTING_BANKROLL
            # entry_fee: taker fee the exchange charges ON TOP of the notional in
            # live mode — without debiting it the ledger drifts above the real
            # wallet by ~0.5-1% per round trip.
            new_balance = current - size - entry_fee
            cur.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso, "TRADE_ENTRY", -(size + entry_fee), new_balance, trade_id, _mode)
            )
            conn.commit()
    return trade_id


def close_position_atomic(pos_id, market_id, side, pnl_dollars, size_usdc, exit_reason,
                           exit_ask_depth_usd=None, exit_bid_depth_usd=None):
    """Delete position row, update trade record, and credit the bankroll all in a
    single transaction. Previously the bankroll credit was a separate connection/
    commit after the position delete + trade update — a process kill between the
    two (OOM-kill, deploy restart, host crash; not just the graceful SIGTERM path)
    would leave the position gone and the trade marked CLOSED with a pnl, but the
    bankroll ledger never receiving the size_usdc + pnl_dollars credit, silently
    and permanently shrinking available cash. Folding the balance read + all three
    writes into one transaction closes that window.

    exit_ask_depth_usd/exit_bid_depth_usd (optional): order-book $ depth captured
    at the moment of exit — the counterpart to ask_depth_usd/bid_depth_usd logged
    on entry in signals. Entry depth alone can't answer "was this market still
    liquid enough to actually get out" — book depth can (and has, live: Seoul and
    Madrid both went to zero asks after entry) look completely different by the
    time a position closes.

    Returns True on success, False if the position was already gone (idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    # Bind the mode ONCE for the whole transaction: a dashboard switch
    # landing mid-write must not split a position, its trade and its cash
    # movement across two books.
    _mode = current_mode()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM positions WHERE id=?", (pos_id,))
            if cur.rowcount == 0:
                return False  # already closed by another thread
            # Target exactly ONE trade row — the newest OPEN one for this market/
            # side. An unqualified market_id+side match stamped identical pnl onto
            # every duplicate OPEN row, double-counting closed P&L and feeding the
            # circuit breaker the same loss N times. COALESCE accumulates any
            # partial-exit pnl already booked on the row (see reduce_position_atomic).
            trow = cur.execute(
                "SELECT id FROM trades WHERE market_id=? AND status='OPEN' AND side=? "
                "AND mode = ? "
                "ORDER BY id DESC LIMIT 1",
                (market_id, side, _mode)
            ).fetchone()
            trade_id = trow[0] if trow else None
            if trade_id is not None:
                cur.execute(
                    "UPDATE trades SET status=?, exit_time=?, exit_reason=?, "
                    "pnl=COALESCE(pnl, 0)+?, exit_ask_depth_usd=?, exit_bid_depth_usd=? "
                    "WHERE id=?",
                    ("CLOSED", now, exit_reason, pnl_dollars,
                     exit_ask_depth_usd, exit_bid_depth_usd, trade_id)
                )
            else:
                logging.error(
                    f"close_position_atomic: no OPEN trade row for {market_id} ({side}) — "
                    f"position deleted and bankroll credited, but trade ledger has no row to close."
                )
            row = cur.execute("SELECT balance FROM bankroll WHERE mode = ? "
                              "ORDER BY id DESC LIMIT 1", (_mode,)).fetchone()
            current = row[0] if row else STARTING_BANKROLL
            new_balance = current + size_usdc + pnl_dollars
            cur.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, "TRADE_EXIT", size_usdc + pnl_dollars, new_balance, trade_id, _mode)
            )
            conn.commit()
    return True


def reduce_position_atomic(pos_id, market_id, side, sold_shares, entry_cost_freed,
                            proceeds, pnl_delta):
    """Book a PARTIAL exit: shrink the position by the shares actually sold, credit
    the bankroll with the real proceeds, and accumulate the realized pnl chunk on
    the open trade row — all in one transaction. Used when a live FAK SELL fills
    less than the full position (before this, a partial fill was booked as a FULL
    close: DB flat, bankroll credited cash never received, unsold shares stranded
    on-chain untracked). Returns True if the position row was updated."""
    now = datetime.now(timezone.utc).isoformat()
    # Bind the mode ONCE for the whole transaction: a dashboard switch
    # landing mid-write must not split a position, its trade and its cash
    # movement across two books.
    _mode = current_mode()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE positions SET shares=COALESCE(shares, size_usdc/entry_price)-?, "
                "size_usdc=size_usdc-? WHERE id=?",
                (sold_shares, entry_cost_freed, pos_id)
            )
            if cur.rowcount == 0:
                return False
            cur.execute(
                "UPDATE trades SET pnl=COALESCE(pnl, 0)+? WHERE id="
                "(SELECT id FROM trades WHERE market_id=? AND status='OPEN' AND side=? "
                "AND mode = ? "
                " ORDER BY id DESC LIMIT 1)",
                (pnl_delta, market_id, side, _mode)
            )
            if cur.rowcount == 0:
                logging.error(
                    f"reduce_position_atomic: no OPEN trade row for {market_id} ({side}) — "
                    f"partial-exit pnl ${pnl_delta:.2f} not booked to any trade."
                )
            row = cur.execute("SELECT balance FROM bankroll WHERE mode = ? "
                              "ORDER BY id DESC LIMIT 1", (_mode,)).fetchone()
            current = row[0] if row else STARTING_BANKROLL
            cur.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, "TRADE_PARTIAL_EXIT", proceeds, current + proceeds, None, _mode)
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
    """Today's realized P&L in the ACTIVE book — the circuit breaker's input.
    Unscoped, a day of paper losses would halt live trading (or a live loss
    would halt a demo), which is the opposite of what either book means."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    query = ("SELECT SUM(pnl) as tpnl FROM trades "
             "WHERE exit_time >= ? AND status='CLOSED' AND mode = ?")
    rows = fetch_query(query, (f"{today}T00:00:00", current_mode()))
    return rows[0]["tpnl"] if rows and rows[0]["tpnl"] is not None else 0.0


def get_open_position(market_id):
    rows = fetch_query("SELECT * FROM positions WHERE market_id=? AND mode=?", (market_id, current_mode()))
    return rows[0] if rows else None


def get_position_by_id(pos_id):
    """One open position by row id, or None if it is already closed.

    Used by the manual-close path to re-read the row under the exit lock: the
    monitor thread may have settled or exited it between the dashboard render
    and the button press, and closing a stale snapshot would sell shares the
    bot no longer holds.
    """
    rows = fetch_query("SELECT * FROM positions WHERE id=?", (pos_id,))
    return rows[0] if rows else None


def _iso_cutoff(keep_days):
    """UTC cutoff in the same isoformat() shape the tables store. SQLite's
    datetime('now') renders 'YYYY-MM-DD HH:MM:SS' (space separator) while stored
    rows use isoformat()'s 'T' — string comparison across the two shapes keeps
    same-day rows ~1 extra day, on a disk that has already hit 100% full."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()


def vacuum_db():
    """Return freed pages to the filesystem. Runs outside _write_lock transactions'
    hot path (daily, after the purges). VACUUM needs free disk up to the DB's
    size — the volume was extended to 3GB to guarantee that headroom."""
    with _write_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()


def purge_old_signals(keep_days=60, skip_keep_days=None, sample_pct=None,
                      near_miss_edge=None):
    """Delete signal rows older than keep_days. Called once per day to prevent table
    bloat. SKIP rows (the ~120k/day per-market skip diagnostics, each carrying the
    full raw_models JSON) get their own, much shorter skip_keep_days window — at the
    shared 14-day retention they alone grew the DB to ~950MB and filled the volume.

    Two classes of SKIP row now survive that window indefinitely, because they are
    the only large calibration sample this bot has. Every constant in config.py is
    currently fitted on 27 settled trades; the skip trail carries ~40,000 scored
    counterfactuals a day and was being deleted to save disk on a volume that has
    since been extended.

      sample_pct      keep a deterministic ~N% sample of ALL skips. Deterministic
                      on id (not random) so the same row survives every purge —
                      a random draw per run would erode the sample to nothing.
      near_miss_edge  keep every skip whose edge cleared the entry threshold but
                      failed some other gate. These are the counterfactuals that
                      carry information: "the model wanted this and a gate said
                      no" is what tells you whether the gate is earning its keep.

    Retained rows survive BOTH windows, including keep_days — a calibration
    sample that self-deletes after two weeks never accumulates enough to fit on,
    which is the whole problem being solved. To make that affordable, their
    raw_models JSON is dropped once they age past skip_keep_days: it is the bulk
    of the row (~2.5KB of ~2.7KB) and is only useful for debugging a recent
    scan, while the scalar features the calibration actually fits on — prices,
    edge, model_prob, spread, agreement — are columns and stay. That turns the
    sample from ~1.8GB/year into ~150MB/year.

    Pass None/0 to either carve-out to disable it."""
    carve = []
    params_carve = []
    if sample_pct:
        # id % 100 is stable across runs, so the retained sample is a fixed
        # cohort rather than a shrinking one.
        carve.append("(id % 100) < ?")
        params_carve.append(int(sample_pct))
    if near_miss_edge is not None:
        carve.append("(edge IS NOT NULL AND edge >= ?)")
        params_carve.append(float(near_miss_edge))
    keep_sql = f"({' OR '.join(carve)})" if carve else None

    # Outer window. Retained rows are exempt, or the sample never accumulates.
    if keep_sql:
        execute_query(
            f"DELETE FROM signals WHERE timestamp < ? AND NOT {keep_sql}",
            (_iso_cutoff(keep_days), *params_carve),
        )
    else:
        execute_query("DELETE FROM signals WHERE timestamp < ?", (_iso_cutoff(keep_days),))

    if skip_keep_days is None:
        return

    skip_cutoff = _iso_cutoff(skip_keep_days)
    if keep_sql:
        execute_query(
            f"DELETE FROM signals WHERE signal_type LIKE 'SKIP%' AND timestamp < ? "
            f"AND NOT {keep_sql}",
            (skip_cutoff, *params_carve),
        )
        # Shed the JSON on the rows we are keeping forever.
        execute_query(
            "UPDATE signals SET raw_models = NULL WHERE signal_type LIKE 'SKIP%' "
            "AND timestamp < ? AND raw_models IS NOT NULL",
            (skip_cutoff,),
        )
    else:
        execute_query(
            "DELETE FROM signals WHERE signal_type LIKE 'SKIP%' AND timestamp < ?",
            (skip_cutoff,),
        )


def purge_old_scan_log(keep_days=14):
    """Delete scan_log rows older than keep_days."""
    execute_query("DELETE FROM scan_log WHERE timestamp < ?", (_iso_cutoff(keep_days),))


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
    execute_query("DELETE FROM notifications WHERE timestamp < ?", (_iso_cutoff(keep_days),))
