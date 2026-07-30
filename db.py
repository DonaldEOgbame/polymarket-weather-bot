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
            # Order-book $ depth at EXIT — the counterpart to ask_depth_usd/
            # bid_depth_usd logged at entry in signals. See close_position_atomic.
            "ALTER TABLE trades ADD COLUMN exit_ask_depth_usd REAL",
            "ALTER TABLE trades ADD COLUMN exit_bid_depth_usd REAL",
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

        # Trading eras. Each row is one funded run of the bot, archived to its
        # own frozen DB snapshot when it ends. The original design had exactly
        # one archive at a fixed path ('paper_archive.db') with a boolean
        # dashboard toggle; that cannot express "paper era, then live era 1,
        # then live era 2 after a full withdrawal and re-fund".
        # `archive_path` is NULL for the era currently running.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS eras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                mode TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                seed_amount REAL,
                final_balance REAL,
                archive_path TEXT,
                note TEXT
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


def get_eras():
    """Every era, newest first. The one with archive_path IS NULL is live."""
    return [dict(r) for r in fetch_query(
        "SELECT * FROM eras ORDER BY id DESC")]


def get_current_era():
    """The era currently running (not yet archived), or None if the ledger has
    never been started under the era system."""
    rows = fetch_query(
        "SELECT * FROM eras WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    return dict(rows[0]) if rows else None


def start_era(label, mode, seed_amount, note=None):
    """Open a new era. Called by start_new_era.py after the money tables have
    been wiped and re-seeded, so era N+1 begins with a clean ledger."""
    now = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO eras (label, mode, started_at, seed_amount, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (label, mode, now, float(seed_amount), note),
            )
            conn.commit()
            return cur.lastrowid


def close_era(era_id, final_balance, archive_path):
    """Mark an era finished and point it at its frozen snapshot."""
    now = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE eras SET ended_at=?, final_balance=?, archive_path=? WHERE id=?",
                (now, float(final_balance), archive_path, era_id),
            )
            conn.commit()


def cutover_era(new_label, new_mode, seed, reset_settings=False):
    """Close the running era and open a fresh one. The shared core behind both
    the Settings tab's "Start new era" button and start_new_era.py — one
    implementation so the two can never drift.

    Steps: refuse while positions are open; freeze the whole DB to
    era_<NNN>_<label>.db and TRIM it (SKIP signals and pre-era signals dropped —
    an untrimmed snapshot is ~250MB and a few of them would fill the volume);
    then in ONE transaction: seal the era row, wipe the money tables (bankroll,
    trades, positions, resolutions — research tables are kept so calibration
    keeps learning across eras), seed the new ledger, open the new era row.
    A crash mid-cutover therefore leaves either the old era fully intact or the
    new one fully opened, never a half-wiped ledger.

    Returns a summary dict. Raises RuntimeError on refusal."""
    import re as _re

    open_pos = fetch_query("SELECT side, question FROM positions")
    if open_pos:
        raise RuntimeError(
            f"{len(open_pos)} position(s) still open — let this era finish settling first")

    cur_era = get_current_era()
    n_trades = fetch_query("SELECT COUNT(*) AS c FROM trades")[0]["c"]
    old_rows = fetch_query("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1")
    old_balance = old_rows[0]["balance"] if old_rows else 0.0
    n_existing = fetch_query("SELECT COUNT(*) AS c FROM eras")[0]["c"]

    # ---- 1. freeze + trim the outgoing era (only if there is history) ----
    archive_path = None
    if n_trades or cur_era:
        closing_label = _re.sub(r"[^A-Za-z0-9._-]", "-",
                                (cur_era or {}).get("label") or "pre-era-history")
        era_no = (cur_era["id"] if cur_era else n_existing) or 1
        archive_path = os.path.join(
            os.path.dirname(DB_PATH), f"era_{era_no:03d}_{closing_label}.db")
        if os.path.exists(archive_path):
            raise RuntimeError(f"{archive_path} already exists — refusing to overwrite an archive")
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(archive_path)
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()
        era_started = cur_era["started_at"] if cur_era else None
        arch = sqlite3.connect(archive_path)
        try:
            with arch:
                arch.execute("DELETE FROM signals WHERE signal_type LIKE 'SKIP%'")
                if era_started:
                    arch.execute("DELETE FROM signals WHERE timestamp < ?", (era_started,))
                    arch.execute("DELETE FROM scan_log WHERE id NOT IN "
                                 "(SELECT id FROM scan_log ORDER BY id DESC LIMIT 500)")
            arch.execute("VACUUM")
        except sqlite3.OperationalError as e:
            logging.warning(f"era archive trim skipped: {e}")
        finally:
            arch.close()

    # ---- 2. seal + wipe + seed + open, atomically ----
    # Direct SQL rather than close_era()/start_era(): those helpers take
    # _write_lock themselves and it is a plain (non-reentrant) Lock — and more
    # importantly this whole block must be ONE transaction.
    now = datetime.now(timezone.utc).isoformat()
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            if cur_era:
                conn.execute(
                    "UPDATE eras SET ended_at=?, final_balance=?, archive_path=? WHERE id=?",
                    (now, float(old_balance), archive_path, cur_era["id"]))
            elif archive_path:
                # History from before the era system: record it retroactively so
                # the archive is reachable from the dashboard, not an orphan file.
                conn.execute(
                    "INSERT INTO eras (label, mode, started_at, ended_at, seed_amount, "
                    "final_balance, archive_path, note) VALUES (?,?,?,?,?,?,?,?)",
                    ("pre-era-history", new_mode, now, now, 0.0, float(old_balance),
                     archive_path, "retro row for trades made before the era system"))
            for t in ("bankroll", "trades", "positions", "resolutions"):
                conn.execute(f"DELETE FROM {t}")
            if reset_settings:
                conn.execute("DELETE FROM settings")
            conn.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) "
                "VALUES (?, 'LIVE_SEED', ?, ?, NULL)", (now, float(seed), float(seed)))
            new_era = conn.execute(
                "INSERT INTO eras (label, mode, started_at, seed_amount) VALUES (?,?,?,?)",
                (new_label, new_mode, now, float(seed))).lastrowid
            conn.commit()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("VACUUM")

    logging.info(f"Era cutover: '{new_label}' (id {new_era}) opened at ${seed:.2f}; "
                 f"{n_trades} trades archived to {archive_path or '(nothing to archive)'}")
    return {
        "new_era_id": new_era, "new_label": new_label, "mode": new_mode,
        "seed": float(seed), "archived_trades": n_trades,
        "closed_balance": float(old_balance), "archive_path": archive_path,
    }


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
    rows = fetch_query(
        "SELECT COALESCE(SUM(amount), 0) AS deposited FROM bankroll WHERE event='DEPOSIT'"
    )
    deposited = rows[0]["deposited"] if rows else 0.0
    # Two seed spellings exist: _seed_bankroll writes 'SEED', cutover_to_live.py
    # writes 'LIVE_SEED'. The live ledger opened with LIVE_SEED, so matching only
    # 'SEED' would omit the original capital and overstate the return.
    seed_rows = fetch_query(
        "SELECT amount FROM bankroll WHERE event IN ('SEED','LIVE_SEED') ORDER BY id LIMIT 1"
    )
    seed = seed_rows[0]["amount"] if seed_rows else STARTING_BANKROLL
    return seed + (deposited or 0.0)


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


def open_position_atomic(market_id, token_id, side, price, size, now_iso, question,
                          is_high, city, target_date, model_prob, edge, shares=None,
                          entry_fee=0.0):
    """Insert the position row, the trade row, and debit the bankroll all in a
    single transaction — see close_position_atomic for why the entry and exit
    sides both need this: a process kill between separate connect()/commit()
    calls (OOM-kill, deploy restart) could otherwise leave a position open
    without its cash ever being debited, silently inflating available cash.
    Returns the new trade_id."""
    with _write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO positions (market_id, token_id, side, entry_price, size_usdc, "
                "entry_time, question, is_high, city, target_date, shares) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, token_id, side, price, size, now_iso, question,
                 1 if is_high else 0, city, target_date,
                 shares if shares is not None else (size / price if price > 0 else None))
            )
            cur.execute(
                "INSERT INTO trades (market_id, side, size_usdc, fill_price, model_prob, edge, "
                "status, entry_time, is_high, city, target_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, side, size, price, model_prob, edge, "OPEN", now_iso,
                 1 if is_high else 0, city, target_date)
            )
            trade_id = cur.lastrowid
            row = cur.execute("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
            current = row[0] if row else STARTING_BANKROLL
            # entry_fee: taker fee the exchange charges ON TOP of the notional in
            # live mode — without debiting it the ledger drifts above the real
            # wallet by ~0.5-1% per round trip.
            new_balance = current - size - entry_fee
            cur.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) VALUES (?, ?, ?, ?, ?)",
                (now_iso, "TRADE_ENTRY", -(size + entry_fee), new_balance, trade_id)
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
                "ORDER BY id DESC LIMIT 1",
                (market_id, side)
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
            row = cur.execute("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
            current = row[0] if row else STARTING_BANKROLL
            new_balance = current + size_usdc + pnl_dollars
            cur.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) VALUES (?, ?, ?, ?, ?)",
                (now, "TRADE_EXIT", size_usdc + pnl_dollars, new_balance, trade_id)
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
                " ORDER BY id DESC LIMIT 1)",
                (pnl_delta, market_id, side)
            )
            if cur.rowcount == 0:
                logging.error(
                    f"reduce_position_atomic: no OPEN trade row for {market_id} ({side}) — "
                    f"partial-exit pnl ${pnl_delta:.2f} not booked to any trade."
                )
            row = cur.execute("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1").fetchone()
            current = row[0] if row else STARTING_BANKROLL
            cur.execute(
                "INSERT INTO bankroll (timestamp, event, amount, balance, trade_id) VALUES (?, ?, ?, ?, ?)",
                (now, "TRADE_PARTIAL_EXIT", proceeds, current + proceeds, None)
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


def purge_old_signals(keep_days=60, skip_keep_days=None):
    """Delete signal rows older than keep_days. Called once per day to prevent table
    bloat. SKIP rows (the ~120k/day per-market skip diagnostics, each carrying the
    full raw_models JSON) get their own, much shorter skip_keep_days window — at the
    shared 14-day retention they alone grew the DB to ~950MB and filled the volume."""
    execute_query("DELETE FROM signals WHERE timestamp < ?", (_iso_cutoff(keep_days),))
    if skip_keep_days is not None:
        execute_query(
            "DELETE FROM signals WHERE signal_type LIKE 'SKIP%' AND timestamp < ?",
            (_iso_cutoff(skip_keep_days),),
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
