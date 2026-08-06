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
            # Mirrors positions.risk_direction, kept on the trade so a CLOSED
            # position's correlation exposure is still answerable after the
            # position row is gone. The caps read open positions; the post-hoc
            # question "how concentrated was the book when that heat wave hit"
            # can only be asked of trades.
            "ALTER TABLE trades ADD COLUMN risk_direction TEXT",
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
            # Which SIGN of temperature surprise loses this position money —
            # "HOT", "COLD", or NULL when it could not be determined. Stored
            # rather than derived on read because it needs the ensemble mean at
            # DECISION time, which nothing else on the row preserves: by the
            # time the correlated-exposure cap is consulted for the next trade,
            # the forecast that classified this one has already moved. See
            # risk.risk_direction.
            "ALTER TABLE positions ADD COLUMN risk_direction TEXT",
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

        # --- Replay log -------------------------------------------------
        # One row per EVALUATED opportunity — traded, skipped, gate-failed
        # alike — carrying enough input to recompute the decision under ANY
        # configuration offline. There is deliberately no second evaluation
        # path in production: every config question is a replay against these
        # rows, so N configurations cost nothing at runtime.
        #
        # The two columns that made the 2026-07-31 audit possible only by
        # hand are raw_models_pre_correction and corrections_applied. The
        # existing signals.raw_models holds POST-correction values, so
        # reconstructing what the models actually said required hardcoding
        # which corrections shipped on which date — and MODEL_BIAS_CORRECTIONS
        # changed twice in one afternoon with no record. Storing the raw
        # values AND the correction applied to each makes the row
        # self-describing: replay never needs to know the config history.
        # Buckets that cannot settle YES on their station's reporting grid.
        # A manual-review queue, not an opportunity list: given that every
        # market's quoting unit matches its station's grid, a firing here says
        # our parser is wrong far more loudly than it says the market is
        # mispriced. See lattice.py and scanner's impossible_bucket skip.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS impossible_buckets (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                city TEXT,
                bucket_low REAL,
                bucket_high REAL,
                lattice TEXT,
                detail_json TEXT,
                first_seen TEXT,
                last_seen TEXT,
                times_seen INTEGER DEFAULT 1,
                reviewed INTEGER DEFAULT 0
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS replay_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                config_fingerprint TEXT NOT NULL,
                mode TEXT,

                market_id TEXT,
                city TEXT,
                city_key TEXT,
                station_icao TEXT,
                region TEXT,
                target_date TEXT,
                is_high INTEGER,
                lead_hours REAL,

                bucket_low REAL,
                bucket_high REAL,
                bucket_type TEXT,
                bucket_width REAL,
                is_narrow INTEGER,

                raw_models_pre_correction TEXT,
                corrections_applied TEXT,
                model_weights TEXT,
                model_run_init TEXT,
                model_count INTEGER,
                weighted_spread_sd REAL,
                unweighted_range REAL,
                model_agreement REAL,
                raw_weighted_mean REAL,
                ensemble_mean REAL,

                best_bid REAL,
                best_ask REAL,
                mid REAL,
                spread_fraction REAL,
                volume REAL,
                ask_depth_usd REAL,
                bid_depth_usd REAL,
                yes_price REAL,
                no_price REAL,

                sigma_base REAL,
                sigma_post_spread REAL,
                sigma_post_direction REAL,
                sigma_post_convective REAL,
                sigma_post_clamp REAL,
                sigma_post_narrow REAL,
                sigma_final REAL,

                prob_raw REAL,
                prob_post_platt REAL,
                prob_post_floor REAL,

                edge_raw REAL,
                edge_post_fee REAL,
                edge_threshold REAL,
                side_evaluated TEXT,

                decision TEXT,
                skip_reason TEXT,

                independent_source TEXT,
                independent_state TEXT,
                independent_value REAL,
                independent_fetched_at TEXT,
                independent_detail TEXT,
                disagreement_f REAL,
                veto_gross INTEGER,
                veto_band INTEGER,
                vetoed INTEGER,

                settled_value REAL,
                settled_outcome TEXT,
                settled_at TEXT
            )
        ''')
        # The independent-veto counterfactual, recorded on EVERY signal whether
        # or not the gate is armed and whether or not an earlier gate already
        # refused the trade.
        #
        # veto_gross/veto_band say what the veto CONCLUDED; `vetoed` says what it
        # DID. They differ whenever the auto-disable tripwire has fired, and
        # keeping them separate is the only way to answer "was the veto right"
        # once settled_value is populating — a disabled gate that still records
        # its opinion is the dataset the 14-day review runs on.
        #
        # independent_state is stored alongside independent_value so NO_DATA and
        # INCONCLUSIVE stay distinguishable forever. Collapsing both to a NULL
        # value would destroy exactly the distinction this feature is built on:
        # one is a coverage fact, the other is an error that says nothing.
        for _col, _type in (
            ("independent_source", "TEXT"),
            ("independent_state", "TEXT"),
            ("independent_value", "REAL"),
            ("independent_fetched_at", "TEXT"),
            ("independent_detail", "TEXT"),
            ("disagreement_f", "REAL"),
            ("veto_gross", "INTEGER"),
            ("veto_band", "INTEGER"),
            ("vetoed", "INTEGER"),
            # Execution safety, 2026-08-06. Rows written before this carry NULL,
            # which the replay reads as "never measured" rather than as "zero" —
            # the opposite of the live gate, which refuses on unknown depth. A
            # replay must not invent a measurement nobody took; the live path
            # must not trade on one.
            ("usable_depth_usd", "REAL"),
            ("stake_usd", "REAL"),
            ("walked_vwap", "REAL"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE replay_signals ADD COLUMN {_col} {_type}")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_ts ON replay_signals(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_settle "
                     "ON replay_signals(target_date, city_key, is_high)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_market "
                     "ON replay_signals(market_id, timestamp)")

        # Structured gate outcomes, one row per gate per signal. NOT a
        # free-text reason string: the prose `signals.signal_type` field is
        # what made the survivor-count reconciliation ambiguous, because
        # "which gate cut this" could only be recovered by parsing English.
        # observed_value and threshold are stored side by side so a units
        # error (the MAX_MODEL_SPREAD max-min -> sd change) shows up as a
        # gate rejecting 0% or 100% rather than as silence.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS replay_gates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                gate TEXT NOT NULL,
                observed REAL,
                threshold REAL,
                passed INTEGER NOT NULL,
                detail TEXT,
                FOREIGN KEY (signal_id) REFERENCES replay_signals(id)
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_gates_sig "
                     "ON replay_gates(signal_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_gates_gate "
                     "ON replay_gates(gate, passed)")

        # --- Monitor-cycle position trail --------------------------------
        # One row per OPEN POSITION per monitor cycle, unconditionally.
        #
        # This exists because signal_trail records EVALUATIONS, and a market
        # stops being evaluated the moment it drops out of the scan candidate
        # set. Measured on the eight historical losers: the trail watched a
        # median of 6.5 hours after entry and was BLIND for a median of 14 hours
        # before close. Houston #68 was watched for 1.0 hour and blind for 32.5.
        #
        # Three questions were unanswerable from stored data as a result, and
        # all three are load-bearing:
        #   * did the losers decline gradually, or jump to zero at settlement?
        #   * was Chongqing's recorded -56.2% drawdown a real mid-price move, or
        #     bid-side spread on a thin book?
        #   * every stop-loss threshold in config.py was replayed against data
        #     that does not cover the window the stop would fire in.
        #
        # Hence best_bid, best_ask and mid are stored SEPARATELY. The bid/mid
        # distinction is the entire unresolved question; one derived number
        # loses it. Rule outcomes go to position_trail_rules, structured, for
        # the same reason replay_gates is not a prose string.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS position_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                mode TEXT,

                position_id INTEGER,
                trade_id INTEGER,
                market_id TEXT,
                token_id TEXT,
                city TEXT,
                target_date TEXT,
                side TEXT,

                best_bid REAL,
                best_ask REAL,
                mid REAL,
                spread_fraction REAL,
                bid_top_size REAL,
                ask_top_size REAL,
                bid_depth_usd REAL,
                ask_depth_usd REAL,
                price_source TEXT,

                entry_price REAL,
                stake_usdc REAL,
                shares REAL,

                unrealized_pnl_mid REAL,
                unrealized_pnl_frac_mid REAL,
                unrealized_pnl_bid REAL,
                unrealized_pnl_frac_bid REAL,

                hours_to_resolution REAL,
                hold_minutes REAL,

                exit_fired INTEGER DEFAULT 0,
                exit_rule_fired TEXT
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ptrail_pos "
                     "ON position_trail(position_id, timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ptrail_ts "
                     "ON position_trail(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ptrail_market "
                     "ON position_trail(market_id, timestamp)")

        # observed/threshold side by side, per rule, per cycle. `fired` is
        # whether the rule's CONDITION held — not whether the bot acted on it,
        # which also depends on the enable flag and on an earlier rule having
        # already exited. Keeping those separate is what makes "would a 40% stop
        # have fired here?" answerable without re-deriving the exit path.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS position_trail_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trail_id INTEGER NOT NULL,
                rule TEXT NOT NULL,
                basis TEXT,
                enabled INTEGER,
                evaluated INTEGER,
                observed REAL,
                threshold REAL,
                fired INTEGER,
                detail TEXT,
                FOREIGN KEY (trail_id) REFERENCES position_trail(id)
            )
        ''')
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ptrail_rules_trail "
                     "ON position_trail_rules(trail_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ptrail_rules_rule "
                     "ON position_trail_rules(rule, fired)")

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
                          entry_fee=0.0, risk_direction=None):
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
                "entry_time, question, is_high, city, target_date, shares, mode, "
                "risk_direction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, token_id, side, price, size, now_iso, question,
                 1 if is_high else 0, city, target_date,
                 shares if shares is not None else (size / price if price > 0 else None),
                 _mode, risk_direction)
            )
            cur.execute(
                "INSERT INTO trades (market_id, side, size_usdc, fill_price, model_prob, edge, "
                "status, entry_time, is_high, city, target_date, mode, risk_direction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, side, size, price, model_prob, edge, "OPEN", now_iso,
                 1 if is_high else 0, city, target_date, _mode, risk_direction)
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


def log_position_trail(row, rules):
    """Persist one monitor-cycle observation of one open position, plus its
    per-rule exit evaluations.

    Best-effort by the same rule as log_replay_signal: this is a measurement
    artifact, not part of the trading path, and a schema drift must degrade the
    dataset rather than stall a monitor cycle or block an exit. Unknown keys in
    `row` are dropped rather than raising.

    `rules` is a list of dicts with keys: rule, basis, enabled, evaluated,
    observed, threshold, fired, detail.

    Returns the new trail id, or None if the write failed.
    """
    try:
        with _write_lock:
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(position_trail)")}
                data = {k: v for k, v in row.items() if k in cols}
                if not data:
                    return None
                names = ", ".join(data)
                marks = ", ".join("?" * len(data))
                cur = conn.execute(
                    f"INSERT INTO position_trail ({names}) VALUES ({marks})",
                    tuple(data.values()),
                )
                trail_id = cur.lastrowid
                if rules:
                    conn.executemany(
                        "INSERT INTO position_trail_rules (trail_id, rule, basis, enabled,"
                        " evaluated, observed, threshold, fired, detail)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(trail_id, r["rule"], r.get("basis"),
                          1 if r.get("enabled") else 0,
                          1 if r.get("evaluated") else 0,
                          None if r.get("observed") is None else float(r["observed"]),
                          None if r.get("threshold") is None else float(r["threshold"]),
                          None if r.get("fired") is None else (1 if r["fired"] else 0),
                          r.get("detail"))
                         for r in rules],
                    )
                conn.commit()
                return trail_id
            finally:
                conn.close()
    except Exception as e:
        logging.error(f"position trail logging failed: {e}", exc_info=True)
        return None


def purge_old_position_trail(keep_days=90):
    """Delete position-trail rows older than keep_days.

    Retention is "life of the position plus keep_days": a row is only eligible
    once it is older than the cutoff, and a row for a position still open is by
    construction younger than the cutoff long before it could be dropped —
    positions here resolve within 72 hours. The floor is enforced by the caller
    (POSITION_TRAIL_RETENTION_DAYS, minimum 90) rather than here, so a wrong
    value fails loudly at config load instead of silently shortening history.

    Volume is small by design: one row per open position per 5 minutes, so four
    concurrent positions held for a full day is ~1,150 rows.
    """
    cutoff = _iso_cutoff(keep_days)
    execute_query(
        "DELETE FROM position_trail_rules WHERE trail_id IN "
        "(SELECT id FROM position_trail WHERE timestamp < ?)",
        (cutoff,),
    )
    execute_query("DELETE FROM position_trail WHERE timestamp < ?", (cutoff,))


def get_position_trail(position_id=None, market_id=None, since=None, limit=None):
    """Reconstruct the bid/ask/mid path for a position, oldest first.

    Ordered by timestamp rather than id so a path stays correct if rows ever
    arrive out of insertion order (two monitor threads, a resumed process)."""
    sql = ["SELECT * FROM position_trail WHERE 1=1"]
    params = []
    if position_id is not None:
        sql.append("AND position_id = ?"); params.append(position_id)
    if market_id is not None:
        sql.append("AND market_id = ?"); params.append(market_id)
    if since is not None:
        sql.append("AND timestamp >= ?"); params.append(since)
    sql.append("ORDER BY timestamp, id")
    if limit:
        sql.append("LIMIT ?"); params.append(limit)
    return fetch_query(" ".join(sql), tuple(params))


def get_position_trail_rules(trail_ids):
    """Per-rule evaluations for a set of trail rows, as {trail_id: [rows]}."""
    ids = list(trail_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out = {}
    for r in fetch_query(
        f"SELECT * FROM position_trail_rules WHERE trail_id IN ({marks}) ORDER BY id",
        tuple(ids),
    ):
        out.setdefault(r["trail_id"], []).append(r)
    return out


def log_replay_signal(row, gates):
    """Persist one evaluated opportunity plus its structured gate outcomes.

    Best-effort: a logging failure must never stop a scan or block a trade. The
    replay log is a research artifact, not part of the trading path, and a
    schema drift here should degrade the dataset rather than the bot.

    `row` is a dict of column -> value; unknown keys are dropped rather than
    raising, so adding a field to the caller before the migration lands is safe.
    `gates` is a list of {gate, observed, threshold, passed, detail}."""
    try:
        with _write_lock:
            conn = sqlite3.connect(DB_PATH, timeout=30.0)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(replay_signals)")}
                data = {k: v for k, v in row.items() if k in cols}
                if not data:
                    return None
                names = ", ".join(data)
                marks = ", ".join("?" * len(data))
                cur = conn.execute(
                    f"INSERT INTO replay_signals ({names}) VALUES ({marks})",
                    tuple(data.values()),
                )
                sig_id = cur.lastrowid
                if gates:
                    conn.executemany(
                        "INSERT INTO replay_gates (signal_id, gate, observed, threshold,"
                        " passed, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        [(sig_id, g["gate"],
                          None if g.get("observed") is None else float(g["observed"]),
                          None if g.get("threshold") is None else float(g["threshold"]),
                          1 if g["passed"] else 0, g.get("detail"))
                         for g in gates],
                    )
                conn.commit()
                return sig_id
            finally:
                conn.close()
    except Exception as e:
        logging.error(f"replay logging failed: {e}", exc_info=True)
        return None


def independent_veto_stats(hours=24, veto_gates=("independent_gross_disagreement",
                                                 "independent_bucket_band")):
    """Rolling-window fire rate for the independent veto, plus city concentration.

    Returns {window_hours, considered, fired, fire_rate, by_city, top_city,
    top_city_share}.

    THE DENOMINATOR IS THE WHOLE POINT, so it is defined here rather than at the
    call site. `considered` counts signals that passed EVERY OTHER GATE and for
    which the provider actually returned DATA — real trade candidates that the
    veto was both the last thing standing between and an order, AND was in a
    position to refuse.

    Two exclusions, each of which would otherwise blind the tripwire:

      * signals refused by another gate. Most evaluations fail the edge
        threshold and never reach the veto; including them divides by thousands
        and guarantees the tripwire never fires however badly the gate behaves.
      * signals where the provider returned NO_DATA or INCONCLUSIVE. These can
        NEVER veto, so counting them measures provider coverage rather than gate
        behaviour. This matters concretely right now: with no DataHub key, 40 of
        51 cities are permanently INCONCLUSIVE, and a denominator including them
        would let the veto fire on literally every US signal — a total failure of
        the eleven armed cities — while reporting a rate near 11/51 x 100% = 22%
        and never tripping. The plan's §5b says "25% of gate-passing signals";
        this reads that as "of the signals the gate actually acted on", because
        the other reading cannot detect the failure the tripwire exists for.

    `all_gate_passing` is returned alongside so the looser denominator is still
    reportable, but the tripwire runs on `considered`.

    That is computed from replay_gates rather than from a stored flag, because
    the gate rows are written by the same list the decision consumes — so this
    cannot drift from what actually gated the trade, which is the property the
    structured gate table was added for.

    `fired` counts the veto's CONCLUSION (veto_gross or veto_band), not its
    effect, so the rate keeps being measurable after the tripwire has disabled
    the gate. Otherwise disabling would drive the rate to zero and the gate
    would re-arm itself into the same storm."""
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    marks = ", ".join("?" * len(veto_gates))
    rows = fetch_query(
        f"""
        SELECT s.city_key AS city_key,
               s.independent_state AS state,
               COALESCE(s.veto_gross, 0) AS vg,
               COALESCE(s.veto_band, 0)  AS vb
        FROM replay_signals s
        WHERE s.timestamp >= ?
          AND s.independent_state IS NOT NULL
          AND NOT EXISTS (
                SELECT 1 FROM replay_gates g
                WHERE g.signal_id = s.id
                  AND g.passed = 0
                  AND g.gate NOT IN ({marks})
          )
        """,
        (since, *veto_gates),
    )

    all_gate_passing = len(rows)
    actionable = [r for r in rows if r["state"] == "DATA"]
    considered = len(actionable)
    by_city = {}
    fired = 0
    for r in actionable:
        if r["vg"] or r["vb"]:
            fired += 1
            city = r["city_key"] or "unknown"
            by_city[city] = by_city.get(city, 0) + 1

    top_city, top_share = None, 0.0
    if fired:
        top_city, top_n = max(by_city.items(), key=lambda kv: kv[1])
        top_share = top_n / fired

    return {
        "window_hours": hours,
        "considered": considered,
        "all_gate_passing": all_gate_passing,
        "fired": fired,
        "fire_rate": (fired / considered) if considered else 0.0,
        "by_city": by_city,
        "top_city": top_city,
        "top_city_share": top_share,
    }


def flag_impossible_bucket(market_id, question, city, bucket_low, bucket_high,
                           detail):
    """Archive a bucket that cannot settle YES on its station's grid.

    An archive rather than a log line, because the value is in the ACCUMULATION:
    one firing is probably a parser bug in a single question, while a run of them
    sharing a phrasing is the bug's signature. UPSERT on market_id so a market
    re-seen every scan cycle stays one row with a bumped count instead of
    thousands.

    Deliberately not a `notifications` row per sighting — that table is the
    dashboard feed and a persistently mispriced market would flood it."""
    import json as _json
    execute_query('''
        INSERT INTO impossible_buckets
            (market_id, question, city, bucket_low, bucket_high, lattice,
             detail_json, first_seen, last_seen, times_seen, reviewed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
        ON CONFLICT (market_id) DO UPDATE SET
            last_seen = excluded.last_seen,
            times_seen = impossible_buckets.times_seen + 1
    ''', (market_id, question, city, bucket_low, bucket_high,
          detail.get("lattice"), _json.dumps(detail),
          datetime.now(timezone.utc).isoformat(),
          datetime.now(timezone.utc).isoformat()))


def get_impossible_buckets(limit=100, include_reviewed=False):
    """The manual-review queue, newest first."""
    where = "" if include_reviewed else "WHERE reviewed = 0"
    return fetch_query(
        f"SELECT * FROM impossible_buckets {where} ORDER BY last_seen DESC LIMIT ?",
        (limit,))


def backfill_replay_outcomes(limit=5000):
    """Attach settlement to replay rows whose target day has since resolved.

    Runs off the resolutions table rather than re-fetching METAR, so the replay
    log settles on exactly the ruler the trades settled on. Rows for markets
    that were never traded stay unsettled until a resolution exists for that
    city/date — the shadow run's whole purpose is scoring markets nobody bet on,
    so this is deliberately keyed on (city, target_date, is_high) and not on
    market_id."""
    execute_query(
        """
        UPDATE replay_signals
           SET settled_value = (
                   SELECT r.actual_value FROM resolutions r
                    WHERE r.city = replay_signals.city
                      AND r.target_date = replay_signals.target_date
                      AND r.actual_value IS NOT NULL
                    ORDER BY r.id DESC LIMIT 1),
               settled_at = ?
         WHERE settled_value IS NULL
           AND target_date IS NOT NULL
           AND EXISTS (
                   SELECT 1 FROM resolutions r
                    WHERE r.city = replay_signals.city
                      AND r.target_date = replay_signals.target_date
                      AND r.actual_value IS NOT NULL)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    # Outcome is derived, not copied: a replay row's bucket is its OWN bucket,
    # which for an untraded market is not the bucket any resolution row scored.
    execute_query(
        """
        UPDATE replay_signals
           SET settled_outcome = CASE
                   WHEN settled_value IS NULL THEN NULL
                   WHEN settled_value >= COALESCE(bucket_low, -1e9) - 0.5
                    AND settled_value <= COALESCE(bucket_high, 1e9) + 0.5
                   THEN 'YES' ELSE 'NO' END
         WHERE settled_value IS NOT NULL AND settled_outcome IS NULL
        """
    )
