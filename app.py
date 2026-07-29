"""
stormedge — combined bot + dashboard server.
Run with:  python app.py
The trading bot runs in a background daemon thread; Flask serves the dashboard.
"""
import os
import re
import sqlite3
import threading
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, jsonify, session, request, send_from_directory, redirect

app = Flask(__name__, static_folder='web')
app.secret_key = os.getenv('DASHBOARD_SECRET', 'stormedge-change-in-prod')
# The settings endpoints can restart the bot and change position sizing, so a
# cross-site POST is no longer a harmless nuisance. There is no CSRF token in
# this app; SameSite=Lax is the cheap protection that actually covers it, since
# it stops the browser attaching this cookie to cross-origin POSTs at all.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'stormedge')
DASHBOARD_EMAIL    = os.getenv('DASHBOARD_EMAIL', 'donaldemmaogbame@gmail.com')
# A settings save kills the process to reload config (see _schedule_restart).
# That only works under a supervisor that brings it back — on Fly this is
# min_machines_running=1 + auto_start_machines=true (fly.toml). Running locally
# with `python app.py` there is nothing to restart it, so the save is refused
# rather than leaving the operator staring at a dead dashboard.
RESTART_SUPERVISED = os.getenv('RESTART_SUPERVISED', 'true').lower() == 'true'
from config import (DB_PATH, PAPER_MODE, DAILY_LOSS_LIMIT, STARTING_BANKROLL,
                    MAX_CONCURRENT_POSITIONS, MAX_TOTAL_EXPOSURE_FRACTION,
                    FIXED_POSITION_SIZE, HARD_MAX_POSITION_SIZE, MIN_POSITION_SIZE,
                    STOP_LOSS_PCT, ENABLE_STOP_LOSS, TAKE_PROFIT_PRICE)
DB_PATH = os.path.abspath(DB_PATH)
# Frozen snapshot of the paper-trading era, written once at live cutover.
# When the dashboard session toggles into archive view, every query reads this
# file (read-only) instead of the live DB — a museum exhibit, not a running bot.
ARCHIVE_DB_PATH = os.path.abspath(os.getenv(
    'ARCHIVE_DB_PATH', os.path.join(os.path.dirname(DB_PATH), 'paper_archive.db')))

from weather import STATIONS

MODEL_META = {
    'ecmwf_ifs025':  (0.40, 'global', 'ECMWF IFS 0.25°'),
    'gfs_global':    (0.30, 'global', 'GFS Global'),
    'icon_global':   (0.25, 'global', 'ICON Global'),
    'gem_global':    (0.15, 'global', 'GEM Global'),
    'jma_gsm':       (0.30, 'AP', 'JMA GSM'),
}


# ---- Settings registry ----
# key -> (type, min, max, label). Bounds are enforced server-side; the UI reads
# them from /api/settings so both sides agree without duplicating the numbers.
# Deliberately money+risk only: strategy gates and calibration constants are
# fitted from resolved-trade data and carry their provenance in config.py
# comments, so editing them from a web form would divorce value from evidence.
SETTING_SPECS = {
    'FIXED_POSITION_SIZE':        ('float', 1.0,     100.0, 'Stake per trade'),
    'HARD_MAX_POSITION_SIZE':     ('float', 1.0,     100.0, 'Per-trade ceiling'),
    'MAX_CONCURRENT_POSITIONS':   ('int',   1,       50,    'Max concurrent positions'),
    # NEGATIVE dollars. A positive value would satisfy `daily_pnl <= limit` on
    # the very first cycle and halt trading permanently (main.py check_circuit_breaker),
    # so the upper bound makes that unrepresentable rather than merely discouraged.
    'DAILY_LOSS_LIMIT':           ('float', -1000.0, -1.0,  'Daily loss limit'),
    'MAX_TOTAL_EXPOSURE_FRACTION':('float', 0.05,    1.0,   'Total exposure cap'),
    'ENABLE_STOP_LOSS':           ('bool',  None,    None,  'Stop loss'),
    'STOP_LOSS_PCT':              ('float', 0.05,    0.95,  'Stop loss level'),
    'TAKE_PROFIT_PRICE':          ('float', 0.50,    0.999, 'Take profit price'),
}

_TRUE_STRINGS = {'true', '1', 'yes', 'on'}


def _coerce_setting(key, raw):
    """JSON value or stored text -> correctly typed Python value."""
    vtype = SETTING_SPECS[key][0]
    if vtype == 'bool':
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in _TRUE_STRINGS
    if vtype == 'int':
        return int(float(raw))      # tolerate 8.0 from a JSON number
    return float(raw)


def _validate_setting(key, raw):
    """Range check one setting. Returns (typed_value, error_message)."""
    if key not in SETTING_SPECS:
        return None, f"unknown setting '{key}'"
    vtype, lo, hi, label = SETTING_SPECS[key]
    if vtype == 'bool':
        return _coerce_setting(key, raw), None
    try:
        value = _coerce_setting(key, raw)
    except (TypeError, ValueError):
        return None, f"{label}: '{raw}' is not a valid {vtype}"
    if value != value or value in (float('inf'), float('-inf')):  # NaN / inf
        return None, f"{label}: must be a finite number"
    if lo is not None and value < lo:
        return None, f"{label}: {value} is below the minimum {lo}"
    if hi is not None and value > hi:
        return None, f"{label}: {value} is above the maximum {hi}"
    return value, None


# ---- DB helpers ----

def _archive_available():
    return os.path.exists(ARCHIVE_DB_PATH)


def _viewing_archive():
    # Session flag set by /api/archive-view; only honoured while the archive
    # file actually exists so a stale session can never point at nothing.
    try:
        return bool(session.get('view_archive')) and _archive_available()
    except RuntimeError:  # outside request context (bot thread)
        return False


def _db():
    if _viewing_archive():
        # Read-only URI open: the archive is a frozen exhibit — nothing the
        # dashboard does may ever write to it.
        conn = sqlite3.connect(f'file:{ARCHIVE_DB_PATH}?mode=ro', uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql, params=()):
    try:
        with _db() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as e:
        # Never swallow silently: a locked/corrupt/full-disk DB previously rendered
        # the dashboard as a healthy-looking empty state with zero indication of
        # failure (during the /data 100%-full incident it showed "no trades").
        logging.error(f"Dashboard query failed: {e} | sql={sql[:120]}")
        return []


# ---- Auth ----

def require_auth(f):
    @wraps(f)
    def inner(*a, **kw):
        if not session.get('authed'):
            return jsonify(error='unauthorized'), 401
        return f(*a, **kw)
    return inner


# ---- Routes ----

@app.route('/')
def root():
    return send_from_directory('web', 'login.html')


@app.route('/dashboard')
def dash():
    if not session.get('authed'):
        return redirect('/')
    return send_from_directory('web', 'dashboard.html')


@app.post('/api/login')
def api_login():
    d = request.get_json(silent=True) or {}
    if d.get('email') == DASHBOARD_EMAIL and d.get('password') == DASHBOARD_PASSWORD:
        session['authed'] = True
        return jsonify(ok=True)
    return jsonify(error='Invalid credentials'), 401


@app.get('/api/logout')
def api_logout():
    session.clear()
    return redirect('/')


@app.post('/api/archive-view')
@require_auth
def api_archive_view():
    """Toggle this session between the live DB and the frozen paper-era archive."""
    if not _archive_available():
        return jsonify(error='no archive snapshot exists yet', archive_view=False), 404
    d = request.get_json(silent=True) or {}
    want = d.get('on')
    session['view_archive'] = (not session.get('view_archive')) if want is None else bool(want)
    return jsonify(ok=True, archive_view=bool(session.get('view_archive')))


def _total_deposited():
    """SEED + every DEPOSIT = total capital ever put in.

    Goes through _q() rather than db.get_total_deposited() so it honours
    archive view like every other dashboard figure. Falls back to the seed
    constant when the ledger has no SEED row (fresh/empty DB)."""
    rows = _q("SELECT COALESCE(SUM(amount), 0) AS t FROM bankroll "
              "WHERE event IN ('SEED','DEPOSIT')")
    total = rows[0]['t'] if rows else 0.0
    return total or STARTING_BANKROLL


# ---- Settings ----

def _restart_preconditions():
    """(ok, reason) — whether it is safe to kill the process right now.

    Refuses while any position is open. executor._submit_taker sends the CLOB
    order BEFORE open_position_atomic writes the DB row, so a kill inside that
    window strands shares on-chain with no position record. The window is
    narrow but it is real money, and with the book flat this costs nothing.
    """
    if _viewing_archive():
        return False, 'dashboard is in archive view — switch back to live first'
    rows = _q('SELECT COUNT(*) AS c FROM positions')
    open_n = rows[0]['c'] if rows else 0
    if open_n:
        return False, (f'{open_n} position(s) still open — sizing and exit settings '
                       f'must not change mid-trade')
    return True, None


def _schedule_restart(reason):
    """Exit shortly after the HTTP response flushes, so the client sees a 200
    instead of a connection reset and can show a "restarting" state.

    os._exit (not sys.exit) for the same reason as _start_bot below: this runs
    on a timer thread, where SystemExit would unwind only that thread and leave
    the process alive holding the OLD config — precisely the silent failure
    this mechanism exists to prevent. Exit code 0: intentional, not a crash.
    """
    def _die():
        logging.warning(f'Restarting process to apply settings: {reason}')
        os._exit(0)
    threading.Timer(1.5, _die).start()


def _live_settings():
    """What the bot is ACTUALLY running with — read from the config module, not
    the settings table. The two differ exactly when a save happened but the
    restart did not, and showing the running value is the honest one."""
    import config as _cfg
    return {key: getattr(_cfg, key) for key in SETTING_SPECS}


@app.get('/api/settings')
@require_auth
def api_settings_get():
    """Current effective settings, their bounds, and the bankroll context the
    UI needs for its live impact readouts."""
    from db import get_total_deposited
    cash_rows = _q('SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1')
    available_cash = cash_rows[0]['balance'] if cash_rows else STARTING_BANKROLL
    pos_rows = _q('SELECT size_usdc FROM positions')
    locked = sum(r['size_usdc'] for r in pos_rows)
    ok, reason = _restart_preconditions()
    return jsonify({
        'values': _live_settings(),
        'meta': {k: {'type': t, 'min': lo, 'max': hi, 'label': label}
                 for k, (t, lo, hi, label) in SETTING_SPECS.items()},
        'context': {
            'available_cash': available_cash,
            'locked_cash': locked,
            'total_equity': available_cash + locked,
            'open_positions': len(pos_rows),
            'min_position_size': MIN_POSITION_SIZE,
            'total_deposited': get_total_deposited(),
            'paper_mode': PAPER_MODE,
        },
        'can_restart': ok,
        'restart_blocked_reason': reason,
        'restart_supervised': RESTART_SUPERVISED,
        'archive_view': _viewing_archive(),
    })


@app.post('/api/settings')
@require_auth
def api_settings_post():
    """Persist money/risk settings, then restart to load them.

    The restart is required, not cosmetic: every module binds config values at
    import time (from config import X), so the running process holds frozen
    copies that no amount of DB writing can change.
    Order is validate -> check preconditions -> save -> restart, so we never
    leave the DB holding settings the process didn't pick up.
    """
    from db import save_settings, add_notification
    if _viewing_archive():
        return jsonify(error='cannot change settings while viewing the paper archive'), 409

    d = request.get_json(silent=True) or {}
    incoming = d.get('settings') or {}
    if not isinstance(incoming, dict) or not incoming:
        return jsonify(error='no settings supplied'), 400

    typed, field_errors = {}, {}
    for key, raw in incoming.items():
        value, err = _validate_setting(key, raw)
        if err:
            field_errors[key] = err
        else:
            typed[key] = value

    # Cross-field rules — need the live bankroll, so they live here rather than
    # in the per-field bounds above.
    live = _live_settings()

    def eff(key):
        return typed.get(key, live[key])

    size, ceiling = eff('FIXED_POSITION_SIZE'), eff('HARD_MAX_POSITION_SIZE')
    if size > ceiling:
        field_errors['HARD_MAX_POSITION_SIZE'] = (
            f'Ceiling ${ceiling:.2f} is below the ${size:.2f} stake — strategy.py takes '
            f'min() of the two, so every trade would silently clamp to ${ceiling:.2f}.')
    if size < MIN_POSITION_SIZE:
        field_errors['FIXED_POSITION_SIZE'] = (
            f'${size:.2f} is below the ${MIN_POSITION_SIZE:.2f} CLOB minimum — '
            f'live orders would not fill.')

    cash_rows = _q('SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1')
    available_cash = cash_rows[0]['balance'] if cash_rows else STARTING_BANKROLL
    pos_rows = _q('SELECT size_usdc FROM positions')
    total_equity = available_cash + sum(r['size_usdc'] for r in pos_rows)
    if size > available_cash:
        field_errors['FIXED_POSITION_SIZE'] = (
            f'${size:.2f} exceeds available cash ${available_cash:.2f} — every signal '
            f'would be skipped for insufficient funds.')
    exposure_cap = total_equity * eff('MAX_TOTAL_EXPOSURE_FRACTION')
    if size > exposure_cap:
        field_errors['MAX_TOTAL_EXPOSURE_FRACTION'] = (
            f'The exposure cap allows ${exposure_cap:.2f} in total, less than one '
            f'${size:.2f} position — no trade could ever open.')

    if field_errors:
        return jsonify(error='validation failed', field_errors=field_errors), 400

    ok, reason = _restart_preconditions()
    if not ok:
        return jsonify(error=reason, can_restart=False), 409
    if not RESTART_SUPERVISED:
        return jsonify(error='RESTART_SUPERVISED=false: nothing would bring this process '
                             'back up, so settings were NOT saved.'), 409

    changed = save_settings({k: v for k, v in typed.items() if v != live[k]})
    if not changed:
        return jsonify(ok=True, restarting=False, changed=[],
                       message='no changes — nothing to apply')
    add_notification('settings', f'Settings changed: {", ".join(changed)}. Restarting to apply.',
                     severity='warning')
    _schedule_restart(f'settings changed: {", ".join(changed)}')
    return jsonify(ok=True, restarting=True, changed=changed, restart_eta_seconds=30)


@app.post('/api/deposit')
@require_auth
def api_deposit():
    """Record a cash deposit in the bankroll ledger.

    Append-only and additive: does NOT touch the P&L baseline (realized P&L is
    computed from trades.pnl alone) and needs NO restart — bankroll is read
    live from the DB on every scan, never bound at import.
    """
    from db import record_deposit, get_total_deposited
    if _viewing_archive():
        return jsonify(error='cannot record a deposit while viewing the paper archive'), 409
    d = request.get_json(silent=True) or {}
    try:
        amount = round(float(d.get('amount')), 2)
    except (TypeError, ValueError):
        return jsonify(error='amount must be a number'), 400
    if amount <= 0:
        return jsonify(error='deposit amount must be positive'), 400
    # Catches a Naira-for-dollars mixup (₦200,000 typed instead of ~$125).
    if amount > 10000:
        return jsonify(error='deposits above $10,000 are blocked as a typo guard — '
                             'is this figure in dollars?'), 400
    if not d.get('confirm'):
        return jsonify(error='deposit requires confirm=true'), 400
    new_balance = record_deposit(amount, note=(d.get('note') or '')[:200])
    return jsonify(ok=True, amount=amount, new_balance=new_balance,
                   total_deposited=get_total_deposited())


@app.get('/api/data')
@require_auth
def api_data():
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')

    # ---- portfolio ----
    cash_rows = _q('SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1')
    available_cash = cash_rows[0]['balance'] if cash_rows else STARTING_BANKROLL

    # Fetch positions directly — no join against signals (44k+ rows).
    # bucket_low/high are extracted from the question string if not on the position.
    pos_rows = _q('SELECT * FROM positions')
    locked_cash = sum(r['size_usdc'] for r in pos_rows)
    total_equity = available_cash + locked_cash

    dpnl = _q(
        "SELECT COALESCE(SUM(pnl), 0) AS p FROM trades "
        "WHERE exit_time >= ? AND status='CLOSED'",
        (f'{today}T00:00:00',)
    )
    daily_pnl = dpnl[0]['p'] if dpnl else 0.0
    # Clamp to 1.0 — an uncapped ratio would exceed 100% (e.g. a live-mode fill
    # blowing past the limit in one move) and overflow any progress-bar/gauge
    # the dashboard renders from this value.
    circuit_used = min(1.0, max(0.0, daily_pnl / DAILY_LOSS_LIMIT)) if DAILY_LOSS_LIMIT != 0 else 0.0
    circuit_tripped = circuit_used >= 1.0

    portfolio = {
        'mode': 'PAPER' if PAPER_MODE else 'LIVE',
        # Archive view: the dashboard is reading the frozen paper-era snapshot,
        # not the running bot's DB. The frontend renders this unmistakably.
        'archive_view': _viewing_archive(),
        'archive_available': _archive_available(),
        'available_cash': available_cash,
        'locked_cash': locked_cash,
        'total_equity': total_equity,
        'daily_pnl': daily_pnl,
        'daily_loss_limit': DAILY_LOSS_LIMIT,
        'starting_bankroll': STARTING_BANKROLL,
        'exposure_pct': locked_cash / total_equity if total_equity else 0.0,
        'circuit_breaker_used': circuit_used,
        'circuit_tripped': circuit_tripped,
        'max_concurrent_positions': MAX_CONCURRENT_POSITIONS,
        'max_total_exposure_fraction': MAX_TOTAL_EXPOSURE_FRACTION,
        # Seed + every deposit = total capital put in. The return figure divides
        # by this, not starting_bankroll: a deposit adds cash without being
        # profit, so the old denominator would book a funding event as a gain.
        'total_deposited': _total_deposited(),
    }

    # ---- open positions (with live mid prices from CLOB) ----
    from scanner import get_realtime_price_status as _live_price
    # Bucket bounds actually used at entry, from the immutable markets table —
    # NOT a live re-parse of the question, which would show today's parser
    # output even for a position entered under an older (possibly buggy)
    # parser version, silently mislabeling what actually drove the trade.
    market_ids = [p['market_id'] for p in pos_rows if p.get('market_id')]
    bucket_by_market = {}
    if market_ids:
        placeholders = ','.join('?' for _ in market_ids)
        for r in _q(
            f'SELECT market_id, bucket_low, bucket_high FROM markets '
            f'WHERE market_id IN ({placeholders})',
            tuple(market_ids)
        ):
            bucket_by_market[r['market_id']] = (r['bucket_low'], r['bucket_high'])

    positions = []
    for p in pos_rows:
        city = p.get('city') or _extract_city(p.get('question') or '')
        bl, bh = bucket_by_market.get(p.get('market_id'), (None, None))
        if bl is None and bh is None:
            # Market predates the markets table (pre-migration) — best-effort
            # fallback only; this may not reflect the bucket actually used
            # at entry if the parser has since changed.
            from scanner import parse_bucket as _parse_bucket
            bl, bh = _parse_bucket(p.get('question') or '')
        bucket = f'{bl}–{bh}°F' if bl is not None and bh is not None else ''
        entry = p['entry_price'] or 0.5
        ask, bid, current, illiquid = 0.0, 0.0, entry, True
        # price_status: 'live' = real mid, 'illiquid' = ghost/empty book,
        # 'unavailable' = CLOB unreachable (network down) — distinct cases.
        price_status = 'unavailable'
        try:
            ask, bid, reachable = _live_price(p['token_id'])
            if not reachable:
                price_status = 'unavailable'
            elif ask > 0 and bid > 0:
                current = round((ask + bid) / 2.0, 4)
                # Illiquid: spread covers almost the entire range (ghost orderbook)
                illiquid = ask > 0.90 and bid < 0.10
                price_status = 'illiquid' if illiquid else 'live'
            elif ask > 0 or bid > 0:
                current = ask or bid
                illiquid = True
                price_status = 'illiquid'
            else:
                # Reachable but empty orderbook — past close, awaiting resolution.
                price_status = 'illiquid'
        except Exception:
            price_status = 'unavailable'
        # Resolution time: midnight UTC after the target date — by then the
        # day's official high/low temperature exists and the market can settle.
        resolves_at = None
        target_date = p.get('target_date')
        if target_date:
            try:
                day = datetime.strptime(target_date, '%Y-%m-%d').date()
                resolves_at = (day + timedelta(days=1)).isoformat() + 'T00:00:00+00:00'
            except ValueError:
                pass
        positions.append({
            'id': p['id'],
            'market_id': p['market_id'],
            'city': city or p['market_id'][:12],
            'question': p.get('question') or '',
            'side': p['side'],
            'entry_price': entry,
            'current_price': current,
            'illiquid': illiquid,
            'price_status': price_status,
            'size_usdc': p['size_usdc'],
            'entry_time': p['entry_time'],
            'resolves_at': resolves_at,
            'bucket': bucket,
        })

    # ---- all closed trades, paginated client-side ----
    # Use the city column stored directly on trades (populated since mid-June).
    # Avoids a full GROUP BY scan of the 44k-row signals table on every dashboard poll.
    trade_rows = _q(
        "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY exit_time DESC"
    )
    trades = []
    for t in trade_rows:
        city = t.get('city') or ''
        hold = _hold_hours(t.get('entry_time'), t.get('exit_time'))
        fill = t['fill_price'] or 0.5
        size = t['size_usdc'] or 1.0
        pnl = t['pnl'] or 0.0
        # Approximate exit price from fill + pnl
        shares = size / fill if fill else 1.0
        exit_price = max(0.01, min(0.99, fill + pnl / shares)) if shares else fill
        trades.append({
            'id': t['id'],
            'city': city or t['market_id'][:12],
            'side': t['side'],
            'question': '',
            'entry_price': fill,
            'exit_price': round(exit_price, 3),
            'size_usdc': size,
            'pnl': pnl,
            'pnl_pct': pnl / size * 100 if size else 0.0,
            'exit_reason': t.get('exit_reason') or 'Unknown',
            'hold_hours': hold,
            'closed_at': t['exit_time'] or now.isoformat(),
            'edge': t.get('edge') or 0.0,
        })

    # ---- performance stats (all periods) ----
    def _calc_stats(days):
        rows = _q(
            "SELECT pnl, edge, entry_time, exit_time FROM trades "
            "WHERE status='CLOSED' AND exit_time >= date('now', ?)",
            (f'-{days} days',)
        )
        total = len(rows)
        wins = sum(1 for t in rows if (t['pnl'] or 0) > 0)
        pnls = [t['pnl'] or 0.0 for t in rows]
        holds = [_hold_hours(t.get('entry_time'), t.get('exit_time')) for t in rows]
        return {
            'win_rate': wins / total if total else 0.0,
            'total_trades': total,
            'avg_edge': sum(t.get('edge') or 0 for t in rows) / total if total else 0.0,
            'realized_pnl': sum(pnls),
            'avg_hold_hours': sum(holds) / len(holds) if holds else 0.0,
            'best_trade': max(pnls) if pnls else 0.0,
            'worst_trade': min(pnls) if pnls else 0.0,
        }

    stats = {
        '30d': _calc_stats(30),
        '6m':  _calc_stats(180),
        '1y':  _calc_stats(365),
    }

    # ---- model accuracy ----
    ma_rows = _q(
        "SELECT model, COUNT(*) AS n, AVG(ABS(forecast_temp - actual_temp)) AS mae "
        "FROM model_accuracy WHERE target_date >= date('now', '-30 days') "
        "GROUP BY model ORDER BY mae"
    )
    ma_prev = {
        r['model']: r['mae'] for r in _q(
            "SELECT model, AVG(ABS(forecast_temp - actual_temp)) AS mae "
            "FROM model_accuracy "
            "WHERE target_date >= date('now', '-60 days') AND target_date < date('now', '-30 days') "
            "GROUP BY model"
        )
    }
    models = []
    for m in ma_rows:
        meta = MODEL_META.get(m['model'], (0.20, 'global', m['model']))
        weight, region, display_name = meta[0], meta[1], meta[2]
        prev = ma_prev.get(m['model'], m['mae'])
        models.append({
            'model': display_name,
            'region': region,
            'mae': round(m['mae'] or 0.0, 3),
            'n': m['n'],
            'weight': weight,
            'trend': round((m['mae'] or 0.0) - (prev or 0.0), 3),
        })
    if not models:
        models = [
            {'model': v[2], 'region': v[1], 'mae': 0.0, 'n': 0, 'weight': v[0], 'trend': 0}
            for k, v in MODEL_META.items()
        ]

    # Count how many filled trades each model appeared in (via raw_models JSON).
    # Restrict to signals whose market_id matches an actual trade — avoids scanning
    # 44k+ SKIP signals and is fast via the idx_signals_market index.
    import json as _json
    traded_market_ids = [t['market_id'] for t in trade_rows if t.get('market_id')]
    model_trade_counts = {}
    if traded_market_ids:
        placeholders = ','.join('?' * len(traded_market_ids))
        raw_model_rows = _q(
            f"SELECT raw_models FROM signals WHERE market_id IN ({placeholders})"
            " AND signal_type NOT LIKE 'SKIP%' AND raw_models IS NOT NULL",
            traded_market_ids,
        )
        for row in raw_model_rows:
            try:
                rm = _json.loads(row['raw_models'])
                for mk in rm.keys():
                    display_name = MODEL_META.get(mk, (0.20, 'global', mk))[2]
                    model_trade_counts[display_name] = model_trade_counts.get(display_name, 0) + 1
            except Exception:
                pass
    for m in models:
        m['trades_used'] = model_trade_counts.get(m['model'], 0)

    # ---- recently scanned signals (for Models tab) ----
    # Mirrors the gate order in strategy.evaluate_opportunity: agreement -> model
    # spread -> market spread -> forecast margin -> taken. "Mean gap" is how far the
    # ensemble mean forecast sits from the nearest bucket boundary being bet against
    # (the forecast-margin gate's own distance check), in °F.
    def _mean_gap(bucket_low, bucket_high, ensemble_mean):
        if ensemble_mean is None:
            return None
        if bucket_low is not None and bucket_high is not None:
            return min(abs(ensemble_mean - bucket_low), abs(ensemble_mean - bucket_high))
        if bucket_low is not None:
            return ensemble_mean - bucket_low
        if bucket_high is not None:
            return bucket_high - ensemble_mean
        return None

    # Classify from the skip-reason text itself (strategy.evaluate_opportunity's exact
    # message formats) rather than re-deriving from the numeric columns — the gates run
    # in order and short-circuit, so e.g. a low-agreement column value on an
    # "Insufficient edge" row never actually reached the agreement check.
    def _gate_outcome(signal_type):
        if signal_type and not signal_type.startswith('SKIP'):
            return 'Taken'
        text = signal_type or ''
        if 'YES entries are disabled' in text:
            return 'YES disabled'
        if 'agreement too low' in text:
            return 'Models disagreed'
        if 'spread too wide' in text and 'market spread' in text:
            return 'Market spread too wide'
        if 'spread too wide' in text:
            return 'Model spread too wide'
        if 'forecast too close to bucket edge' in text:
            return 'Too close to bucket edge'
        if 'raw model forecast points the other way' in text:
            return 'Direction mismatch'
        if 'Insufficient edge' in text:
            return 'Edge below threshold'
        return 'Other skip'

    # Show every signal from the latest scan cycle, not an arbitrary row count.
    # One scan writes hundreds of signals within the same minute (see MAX(timestamp)),
    # so window on that instead of a fixed LIMIT — otherwise a big scan gets truncated
    # to whichever markets happened to sort first.
    latest_sig_ts_row = _q('SELECT MAX(timestamp) AS mx FROM signals')
    latest_sig_ts = latest_sig_ts_row[0]['mx'] if latest_sig_ts_row else None
    sig_rows = []
    if latest_sig_ts:
        cutoff = (datetime.fromisoformat(latest_sig_ts) - timedelta(minutes=5)).isoformat()
        sig_rows = _q(
            "SELECT timestamp, market_id, city, target_date, bucket_low, bucket_high, "
            "model_prob, yes_price, no_price, edge, confidence AS agreement, model_spread, "
            "ensemble_std, raw_models, signal_type, market_spread_frac FROM signals "
            "WHERE timestamp >= ? ORDER BY edge DESC",
            (cutoff,)
        )
    recent_signals = []
    for s in sig_rows:
        raw_models = {}
        if s.get('raw_models'):
            try:
                raw_models = _json.loads(s['raw_models'])
            except Exception:
                pass
        temps = list(raw_models.values())
        ensemble_mean = sum(temps) / len(temps) if temps else None
        recent_signals.append({
            'ts': s['timestamp'],
            'market_id': s['market_id'],
            'city': s['city'],
            'target_date': s['target_date'],
            'bucket_low': s['bucket_low'],
            'bucket_high': s['bucket_high'],
            'model_prob': s['model_prob'],
            'yes_price': s['yes_price'],
            'no_price': s['no_price'],
            'edge': s['edge'],
            'agreement': s['agreement'],
            'model_spread': s['model_spread'],
            'ensemble_std': s['ensemble_std'],
            'market_spread_frac': s['market_spread_frac'],
            'raw_models': raw_models,
            'mean_gap': _mean_gap(s['bucket_low'], s['bucket_high'], ensemble_mean),
            'gate_outcome': _gate_outcome(s['signal_type']),
            'reason': s['signal_type'],
        })

    # ---- scan log ----
    scan_rows = _q(
        "SELECT id, timestamp, market_id, question, skip_reason, hours_to_res, volume "
        "FROM scan_log ORDER BY id DESC LIMIT 500"
    )
    skip_counts = {}
    recent_skips = []
    for s in scan_rows:
        reason = s.get('skip_reason')
        if not reason:
            continue
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
        if len(recent_skips) < 8:
            city = _extract_city(s.get('question') or '')
            bucket = _extract_bucket(s.get('question') or '')
            recent_skips.append({
                'ts': s['timestamp'] or now.isoformat(),
                'city': city,
                'bucket': bucket,
                'reason': reason,
            })

    skip_buckets = sorted(
        [{'reason': k, 'count': v} for k, v in skip_counts.items()],
        key=lambda x: -x['count']
    )[:7]

    # scan_log only records candidates rejected during discovery (station/bucket/volume/
    # expiry pre-filters in scanner.scan_markets). Once discovery is passing more markets
    # through cleanly, scan_log can go quiet for hours while the bot is still actively
    # scanning and evaluating every cycle — those evaluations land in `signals` instead
    # (written by strategy.evaluate_opportunity). Take the max of both so "last scan"
    # reflects real bot activity, not just the discovery-rejection table.
    latest_signal_row = _q('SELECT MAX(timestamp) AS mx FROM signals')
    latest_signal_ts = latest_signal_row[0]['mx'] if latest_signal_row else None
    scan_log_ts = scan_rows[0]['timestamp'] if scan_rows else None
    last_scan_ts = max(filter(None, [scan_log_ts, latest_signal_ts]), default=now.isoformat())
    markets_seen = len(scan_rows)
    candidates = sum(1 for s in scan_rows if not s.get('skip_reason'))

    filled_today = _q(
        "SELECT COUNT(*) AS c FROM trades "
        "WHERE entry_time >= ? AND status IN ('OPEN', 'CLOSED')",
        (f'{today}T00:00:00',)
    )
    filled = filled_today[0]['c'] if filled_today else 0

    scan_log = {
        'last_scan_at': last_scan_ts,
        'duration_ms': 2412,
        'markets_seen': markets_seen,
        'candidates': max(candidates, filled),
        'filled': filled,
        'shadow_passed': 0,
        'skip_buckets': skip_buckets,
        'recent_skips': recent_skips,
    }

    # ---- cities (from weather.STATIONS) ----
    seen = set()
    cities = []
    for k, v in STATIONS.items():
        # Deduplicate aliases (e.g. "NYC" and "New York" share coords)
        coord_key = (round(v['lat'], 1), round(v['lon'], 1))
        if coord_key in seen:
            continue
        seen.add(coord_key)
        cities.append({'key': k, 'name': k, 'lat': v['lat'], 'lon': v['lon']})

    # ---- city activity ----
    city_activity = {}
    for p in pos_rows:
        city = p.get('city') or _extract_city(p.get('question') or '')
        if city:
            city_activity[city] = {
                'state': 'active',
                'position': {
                    'side': p['side'],
                    'entry_price': p['entry_price'],
                    'size_usdc': p['size_usdc'],
                },
            }
    for s in recent_skips:
        city = s['city']
        if city and city not in city_activity:
            city_activity[city] = {'state': 'scanned', 'skip': s}

    sig_today = _q(
        "SELECT DISTINCT city FROM signals WHERE timestamp >= ? AND city IS NOT NULL",
        (f'{today}T00:00:00',)
    )
    for row in sig_today:
        city = row['city']
        if city and city not in city_activity:
            city_activity[city] = {'state': 'signal'}

    # ---- calibration (Brier score over resolved trades) ----
    calib_rows = _q(
        "SELECT brier, won, model_prob_at_entry, side FROM resolutions "
        "WHERE brier IS NOT NULL ORDER BY id DESC LIMIT 500"
    )
    calibration = _build_calibration(calib_rows)

    return jsonify({
        'now': now.isoformat(),
        'portfolio': portfolio,
        'positions': positions,
        'trades': trades,
        'stats': stats,
        'models': models,
        'recentSignals': recent_signals,
        'scanLog': scan_log,
        'cities': cities,
        'cityActivity': city_activity,
        'calibration': calibration,
    })


@app.get('/api/notifications')
@require_auth
def api_notifications():
    """Recent dashboard notifications: errors, daily summaries, circuit-breaker
    trips. Newest first. Optional ?limit= (default 100, max 500)."""
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
    except (TypeError, ValueError):
        limit = 100
    rows = _q(
        "SELECT id, timestamp, kind, severity, message FROM notifications "
        "ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    unseen_errors = _q(
        "SELECT COUNT(*) AS c FROM notifications WHERE severity='error'"
    )
    return jsonify({
        'notifications': rows,
        'count': len(rows),
        'error_count': unseen_errors[0]['c'] if unseen_errors else 0,
    })


def _build_calibration(rows):
    """Compute Brier score and a reliability table from resolution rows.
    Brier = mean squared error between model probability and outcome (0 or 1).
    - 0.0 = perfectly calibrated
    - 0.25 = no skill (always 50%)
    - >0.25 = worse than chance
    Reliability buckets: group predictions into deciles, compare predicted vs. observed."""
    if not rows:
        return {'n': 0}

    briers = [r['brier'] for r in rows if r['brier'] is not None]
    wins = sum(1 for r in rows if r['won'])
    n = len(rows)
    mean_brier = sum(briers) / len(briers) if briers else None

    # Reliability buckets in 0.1 bins of predicted-for-side probability
    bins = [[] for _ in range(10)]
    for r in rows:
        prob = r['model_prob_at_entry']
        if prob is None:
            continue
        # For NO side, the bet probability is 1 - model_prob_at_entry
        p_side = prob if r['side'] == 'YES' else (1.0 - prob)
        idx = min(9, max(0, int(p_side * 10)))
        bins[idx].append((p_side, 1 if r['won'] else 0))

    reliability = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        avg_predicted = sum(b[0] for b in bucket) / len(bucket)
        observed_freq = sum(b[1] for b in bucket) / len(bucket)
        reliability.append({
            'bin': f'{i*10}-{(i+1)*10}%',
            'avg_predicted': round(avg_predicted, 3),
            'observed_freq': round(observed_freq, 3),
            'n': len(bucket),
        })

    return {
        'n': n,
        'win_rate': round(wins / n, 3) if n else None,
        'brier': round(mean_brier, 4) if mean_brier is not None else None,
        'brier_no_skill': 0.25,
        'reliability': reliability,
    }


# ---- Static file fallback ----

@app.route('/<path:filename>')
def static_fallback(filename):
    return send_from_directory('web', filename)


# ---- Helpers ----

def _extract_city(text):
    if not text:
        return ''
    tl = text.lower()
    for k in sorted(STATIONS.keys(), key=len, reverse=True):
        if k.lower() in tl:
            return k
    return ''


def _extract_bucket(text):
    if not text:
        return ''
    m = re.search(r'(\d+)\s*[–—-]\s*(\d+)\s*°\s*([CFcf])', text)
    if m:
        return f"{m.group(1)}–{m.group(2)}°{m.group(3).upper()}"
    m = re.search(r'(?:above|over)\s+(\d+)\s*°\s*([CFcf])', text, re.I)
    if m:
        return f">{m.group(1)}°{m.group(2).upper()}"
    m = re.search(r'(?:below|under)\s+(\d+)\s*°\s*([CFcf])', text, re.I)
    if m:
        return f"<{m.group(1)}°{m.group(2).upper()}"
    return ''


def _hold_hours(entry_str, exit_str):
    try:
        et = datetime.fromisoformat(str(entry_str).replace('Z', '+00:00'))
        xt = datetime.fromisoformat(str(exit_str).replace('Z', '+00:00'))
        return (xt - et).total_seconds() / 3600
    except Exception:
        return 0.0


def _start_bot():
    """Run the shared bot loop from main.py in this background thread.

    The loop implementation lives only in main.py (run_bot) — app.py no longer
    keeps its own copy, so scan/monitor/resolution/summary/purge logic can't
    drift between the standalone and dashboard entrypoints.
    """
    try:
        import main as _main
        _main.run_bot(in_thread=True)
        logging.error("Bot loop exited unexpectedly — terminating process for supervisor restart.")
    except Exception as e:
        logging.error(f"Bot thread fatal error: {e}", exc_info=True)
    # A dead bot thread behind a live Flask server is a zombie: the machine looks
    # healthy (port answers) while nothing trades or monitors positions holding
    # real money. Kill the whole process so Fly restarts the machine.
    os._exit(1)


@app.route('/healthz')
def healthz():
    """Unauthenticated liveness probe for the fly.io health check: bot thread
    heartbeat must be fresh (scan/monitor cycles run every few minutes)."""
    try:
        import main as _main
        beat = _main.last_cycle_at
        if beat is None:
            # Startup grace: thread may still be booting.
            return jsonify({"status": "starting"}), 200
        age = (datetime.now(timezone.utc) - beat).total_seconds()
        if age > 3 * 60 * 15:  # 3 missed 15-min windows worth of silence
            return jsonify({"status": "stale", "last_cycle_age_s": int(age)}), 503
        return jsonify({"status": "ok", "last_cycle_age_s": int(age)}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 503


if __name__ == '__main__':
    port = int(os.getenv('PORT', 7777))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'

    bot_thread = threading.Thread(target=_start_bot, daemon=True, name='bot')
    bot_thread.start()
    logging.info('Bot started in background thread.')

    print(f'  stormedge → http://localhost:{port}')
    print(f'  password:    {DASHBOARD_PASSWORD}')
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
