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
# "Keep me signed in" makes the session cookie permanent, so it survives a
# browser restart instead of dying with the tab. Flask writes the expiry into
# the signed cookie itself, so the lifetime is enforced server-side on read —
# an edited cookie can't extend it. Without the checkbox the session stays a
# browser-session cookie and disappears when the browser closes.
app.permanent_session_lifetime = timedelta(days=30)

DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'stormedge')
DASHBOARD_EMAIL    = os.getenv('DASHBOARD_EMAIL', 'donaldemmaogbame@gmail.com')
# Settings apply LIVE: the POST persists to the settings table and then swaps
# the new values into config's runtime store (config.setting), which the bot
# thread reads at every decision point. No restart, no downtime. Managed keys
# are the money/risk knobs only; everything else still requires a deploy.
import config as _config
from config import (DB_PATH, STARTING_BANKROLL, MIN_POSITION_SIZE,
                    MANAGED_SETTINGS)
DB_PATH = os.path.abspath(DB_PATH)

from weather import STATIONS

# Placeholder entry price for the settings panel's upside readout, used only
# until the current mode has a fill to average. Set from the 59-trade history in
# reports/history-2026-07-31 (mean fill $0.696; live $0.671, paper $0.711, range
# $0.41-$0.89). The previous $0.45 sat below every fill ever taken and inflated
# the quoted upside roughly eightfold on a fresh era.
ASSUMED_ENTRY_PRICE = 0.70

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
    'MAX_CONCURRENT_POSITIONS':   ('int',   1,       50,    'Max concurrent positions'),
    # The daily loss limit is DYNAMIC: expressed as a budget of full-stake
    # losses, so the dollar threshold (-(stake × this)) scales automatically
    # when the stake changes. Positive by construction — the old fixed-dollar
    # knob could be typo'd positive, which would have halted trading forever.
    'DAILY_LOSS_STAKES':          ('float', 0.5,     20.0,  'Daily loss budget (stakes)'),
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

def _mode():
    """Which book the dashboard is reading: 'paper' or 'live'.

    Every money query is scoped by this. Paper and live rows live side by side
    in the same tables, so an unscoped SUM would report simulated fills as real
    P&L — the exact contamination the mode column exists to prevent."""
    return 'paper' if _config.paper_mode() else 'live'

def _db():
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
    # An already-signed-in visitor goes straight through. Without this, "Keep me
    # signed in" LOOKS broken even when it works perfectly: the cookie is valid
    # for 30 days and /dashboard accepts it, but returning to the bare hostname
    # rendered the sign-in form anyway, which is indistinguishable from having
    # been logged out. Only /dashboard ever consulted the session.
    if session.get('authed'):
        return redirect('/dashboard')
    # The sign-in art carries a "<mode> MODE ENABLED" strip. Substituting it
    # here (rather than fetching it) keeps the page honest without exposing an
    # unauthenticated endpoint that reports what the bot is doing.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'login.html')
    with open(path, encoding='utf-8') as fh:
        html = fh.read().replace('{{MODE}}', 'PAPER' if _config.paper_mode() else 'LIVE')
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/dashboard')
def dash():
    if not session.get('authed'):
        return redirect('/')
    return send_from_directory('web', 'dashboard.html')


@app.post('/api/login')
def api_login():
    d = request.get_json(silent=True) or {}
    if d.get('email') == DASHBOARD_EMAIL and d.get('password') == DASHBOARD_PASSWORD:
        # Re-issue a clean session on every login: a fresh id stops a
        # fixated one being carried into the newly authenticated session.
        session.clear()
        session['authed'] = True
        # Opt-in only — default False keeps the old browser-session behaviour
        # for anyone who leaves the box unchecked.
        session.permanent = bool(d.get('remember'))
        return jsonify(ok=True)
    return jsonify(error='Invalid credentials'), 401


# POST, not GET. As a GET link this was destroying sessions on its own: any
# link prefetcher — Chrome's "Preload pages for faster browsing", an extension,
# a link scanner — that follows the href signs the user out, and SameSite=Lax
# still attaches the cookie to a top-level cross-site GET, so a link from
# anywhere else logs them out too. Both present as "it keeps forgetting me".
@app.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify(ok=True)

def _total_deposited():
    """Seed + every DEPOSIT = total capital ever put in.

    Both seed spellings count: db._seed_bankroll writes 'SEED' and older
    ledgers opened with 'LIVE_SEED', so matching only 'SEED' would drop the
    original stake from the denominator and overstate returns."""
    rows = _q("SELECT COALESCE(SUM(amount), 0) AS t FROM bankroll "
              "WHERE event IN ('SEED','LIVE_SEED','DEPOSIT') AND mode = ?", (_mode(),))
    total = rows[0]['t'] if rows else 0.0
    return total or STARTING_BANKROLL


def _total_withdrawn():
    """Sum of cash taken OUT (WITHDRAWAL rows are negative; returned positive).

    Needed for honest P&L once withdrawals exist: profit is
    equity + withdrawn - deposited. Without the withdrawn term, taking money
    off the table would read as a trading loss of the same size."""
    rows = _q("SELECT COALESCE(SUM(amount), 0) AS t FROM bankroll "
              "WHERE event='WITHDRAWAL' AND mode = ?", (_mode(),))
    return abs(rows[0]['t']) if rows else 0.0


# ---- Settings ----

def _live_settings():
    """What the bot is ACTUALLY running with, straight from config's runtime
    store — the same store every strategy/executor decision reads, so what this
    returns is by construction what the next trade will use."""
    return {key: _config.setting(key) for key in SETTING_SPECS}


@app.get('/api/settings')
@require_auth
def api_settings_get():
    """Current effective settings, their bounds, and the bankroll context the
    UI needs for its live impact readouts."""
    from db import get_total_deposited
    cash_rows = _q('SELECT balance FROM bankroll WHERE mode = ? ORDER BY id DESC LIMIT 1', (_mode(),))
    available_cash = cash_rows[0]['balance'] if cash_rows else STARTING_BANKROLL
    pos_rows = _q('SELECT size_usdc FROM positions WHERE mode = ?', (_mode(),))
    locked = sum(r['size_usdc'] for r in pos_rows)
    # Typical entry price, so the settings panel can seed the take-profit upside
    # readout. fill_price is written once at entry (db.record_trade) and exits
    # update the same row, so this really is an average of entries, open ones
    # included. Scoped by mode, so it is empty on every fresh era, not just the
    # first one — hence avg_entry_n, which lets the UI say whether the number is
    # measured or assumed instead of presenting the placeholder as fact. The user
    # can override the figure outright in the panel.
    entry_rows = _q(
        'SELECT AVG(fill_price) AS avg_entry, COUNT(*) AS n FROM trades '
        'WHERE fill_price IS NOT NULL AND fill_price > 0 AND mode = ?', (_mode(),)
    )
    avg_entry_n = entry_rows[0]['n'] if entry_rows else 0
    avg_entry = (entry_rows[0]['avg_entry'] if entry_rows else None) or ASSUMED_ENTRY_PRICE
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
            'avg_entry_price': avg_entry,
            'avg_entry_n': avg_entry_n,
            'total_deposited': get_total_deposited(),
            'paper_mode': _config.paper_mode(),
            'daily_loss_limit': _config.daily_loss_limit(),
        },
    })


@app.post('/api/settings')
@require_auth
def api_settings_post():
    """Persist money/risk settings and apply them LIVE — no restart.

    Order is validate -> persist -> swap into config's runtime store. The bot
    thread reads that store (config.setting) at every decision point, so the
    change governs the very next entry/exit check. Persisting first means a
    crash between the two steps loses nothing: the next boot seeds the store
    from the settings table.
    """
    from db import save_settings, add_notification

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

    size = eff('FIXED_POSITION_SIZE')
    if size < MIN_POSITION_SIZE:
        field_errors['FIXED_POSITION_SIZE'] = (
            f'${size:.2f} is below the ${MIN_POSITION_SIZE:.2f} CLOB minimum — '
            f'live orders would not fill.')

    cash_rows = _q('SELECT balance FROM bankroll WHERE mode = ? ORDER BY id DESC LIMIT 1', (_mode(),))
    available_cash = cash_rows[0]['balance'] if cash_rows else STARTING_BANKROLL
    pos_rows = _q('SELECT size_usdc FROM positions WHERE mode = ?', (_mode(),))
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

    changed = save_settings({k: v for k, v in typed.items() if v != live[k]})
    if not changed:
        return jsonify(ok=True, changed=[], values=_live_settings(),
                       message='no changes — nothing to apply')
    # Swap into the live store AFTER the DB write: if the write had failed we
    # would not want memory claiming values disk never recorded.
    _config.apply_runtime_overrides({k: typed[k] for k in changed})
    add_notification('settings',
                     f'Settings changed: {", ".join(changed)}. Applied immediately.',
                     severity='info')
    logging.info(f'Settings applied live: {", ".join(changed)}')
    return jsonify(ok=True, changed=changed, values=_live_settings(),
                   daily_loss_limit=_config.daily_loss_limit())


@app.get('/api/live-preflight')
@require_auth
def api_live_preflight():
    """Everything that must be true before real money may move. Read-only."""
    from check_live_readiness import preflight
    try:
        result = preflight()
    except Exception as e:
        logging.error(f"Preflight failed: {e}", exc_info=True)
        return jsonify(ok=False, checks=[{
            'id': 'preflight', 'label': 'Readiness check', 'ok': False,
            'blocking': True, 'detail': f'{type(e).__name__}: {e}'}]), 200
    open_positions = len(_q('SELECT id FROM positions WHERE mode = ?', (_mode(),)))
    # A paper position is a row with no on-chain counterpart. Going live while
    # holding one would have the executor manage it as if it were real: it would
    # try to SELL shares the wallet has never owned.
    result['checks'].append({
        'id': 'flat_book', 'label': 'No open paper positions',
        'ok': open_positions == 0, 'blocking': True,
        'detail': 'book is flat' if open_positions == 0 else
                  f'{open_positions} position(s) open — let them settle first, '
                  f'or close them from the Desk',
    })
    result['ok'] = all(c['ok'] for c in result['checks'] if c.get('blocking'))
    result['paper_mode'] = _config.paper_mode()
    return jsonify(result)


@app.post('/api/trading-mode')
@require_auth
def api_trading_mode():
    """Switch between simulated and real trading, live, with no restart.

    Deliberately NOT part of the bulk settings save: this is the only control in
    the app that starts spending real money, so it has its own endpoint, its own
    explicit confirm, and its own gate.

    paper -> live  requires every blocking preflight check to pass.
    live  -> paper is allowed unless real positions are open: dropping to paper
             would leave the executor managing them as simulations and it would
             never place the real exit orders.
    """
    from db import add_notification, save_settings, ensure_bankroll_seeded

    d = request.get_json(silent=True) or {}
    if 'paper' not in d:
        return jsonify(error='send {"paper": true|false, "confirm": true}'), 400
    want_paper = bool(d['paper'])
    if not d.get('confirm'):
        return jsonify(error='changing trading mode requires confirm=true'), 400

    current_paper = _config.paper_mode()
    if want_paper == current_paper:
        return jsonify(ok=True, paper_mode=current_paper, changed=False,
                       message=f'already in {"paper" if current_paper else "live"} mode')

    open_positions = len(_q('SELECT id FROM positions WHERE mode = ?', (_mode(),)))

    if not want_paper:
        # ---- paper -> live: the hard gate ----
        from check_live_readiness import preflight
        try:
            result = preflight()
        except Exception as e:
            logging.error(f"Preflight failed during mode switch: {e}", exc_info=True)
            return jsonify(error=f'readiness check failed: {type(e).__name__}: {e}'), 409
        blocked = [c for c in result['checks'] if c.get('blocking') and not c['ok']]
        if open_positions:
            blocked.append({'label': 'No open paper positions',
                            'detail': f'{open_positions} position(s) open'})
        if blocked:
            return jsonify(
                error='not ready for live trading',
                blocked=[{'label': c['label'], 'detail': c['detail']} for c in blocked],
            ), 409
    elif open_positions:
        # ---- live -> paper with real money on the table ----
        return jsonify(
            error=f'{open_positions} live position(s) are open. Switching to paper would '
                  f'stop the bot placing their real exit orders — close them from the Desk '
                  f'or let them settle first.'), 409

    # Persist first, then swap into the live store — the same order as the bulk
    # settings save, so memory can never claim a mode disk never recorded.
    # Persisting is what makes the switch survive a deploy; without it every
    # restart would silently drop a live bot back to paper.
    save_settings({'PAPER_MODE': want_paper})
    _config.apply_runtime_overrides({'PAPER_MODE': want_paper})

    # Open the target book's ledger if this is the first time it has been used.
    # Paper opens at STARTING_BANKROLL so a demo has something to trade with;
    # live opens at $0 and the wallet sync books real cash when it lands —
    # inventing a live balance would have the bot size orders it cannot pay for.
    # An existing ledger is left exactly as it was, which is what makes
    # switching back resume where you left off rather than starting over.
    seeded = ensure_bankroll_seeded()

    # Build the CLOB client NOW, while someone is watching, rather than letting
    # the first real trade discover that it cannot be built.
    client_note = None
    if not want_paper:
        try:
            import main as _main
            if _main.executor is not None and _main.executor._ensure_client() is None:
                client_note = ('CLOB client could not be built — orders will not be '
                               'placed. Check the bot log.')
        except Exception as e:
            client_note = f'CLOB client init raised: {type(e).__name__}: {e}'

    mode = 'paper' if want_paper else 'live'
    add_notification('mode', f'Trading mode switched to {mode.upper()}.',
                     severity='warning' if not want_paper else 'info')
    logging.warning(f'TRADING MODE -> {mode.upper()} (via dashboard)')
    opened = (f' Opened a fresh {mode} ledger at {seeded:,.2f}.'
              if seeded is not None else f' Your {mode} ledger resumes where it left off.')
    return jsonify(ok=True, paper_mode=want_paper, changed=True,
                   warning=client_note, seeded=seeded,
                   message=f'Now trading in {mode} mode. This applies to the very next '
                           f'decision the bot makes.' + opened)

@app.post('/api/deposit')
@require_auth
def api_deposit():
    """Record a cash deposit in the bankroll ledger.

    Append-only and additive: does NOT touch the P&L baseline (realized P&L is
    computed from trades.pnl alone) and needs NO restart — bankroll is read
    live from the DB on every scan, never bound at import.
    """
    from db import record_deposit, get_total_deposited
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


@app.post('/api/close-position')
@require_auth
def api_close_position():
    """Manually close one open position at the current market bid.

    Irreversible (it sells on-chain in live mode), so it requires confirm=true
    the way /api/deposit does. The actual sell runs through
    Executor.close_position_manual, which holds the same per-position exit lock
    the monitor thread uses — the button cannot race a bot-initiated exit into
    a double sell.
    """
    import main as _main
    from db import add_notification
    d = request.get_json(silent=True) or {}
    try:
        pos_id = int(d.get('position_id'))
    except (TypeError, ValueError):
        return jsonify(error='position_id must be an integer'), 400
    if not d.get('confirm'):
        return jsonify(error='closing a position requires confirm=true'), 400

    executor = getattr(_main, 'executor', None)
    if executor is None:
        # Bot thread still booting: no CLOB client, so a sell would be impossible.
        return jsonify(error='bot is still starting up — try again in a moment'), 503

    try:
        result = executor.close_position_manual(pos_id, note=(d.get('note') or '')[:100] or None)
    except Exception as e:
        logging.error(f"Manual close failed for position {pos_id}: {e}", exc_info=True)
        return jsonify(error=f'close failed: {e}'), 500

    status_codes = {'not_found': 404, 'busy': 409, 'no_fill': 502}
    code = 200 if result.get('ok') else status_codes.get(result.get('status'), 400)
    if result.get('ok'):
        add_notification('manual_close',
                         f"Manual close: {result.get('question') or result.get('market_id')} — "
                         f"{result.get('message')}",
                         severity='info')
    return jsonify(**result), code


@app.get('/api/data')
@require_auth
def api_data():
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')

    # ---- portfolio ----
    cash_rows = _q('SELECT balance FROM bankroll WHERE mode = ? ORDER BY id DESC LIMIT 1', (_mode(),))
    available_cash = cash_rows[0]['balance'] if cash_rows else STARTING_BANKROLL

    # Fetch positions directly — no join against signals (44k+ rows).
    # bucket_low/high are extracted from the question string if not on the position.
    pos_rows = _q('SELECT * FROM positions WHERE mode = ?', (_mode(),))
    locked_cash = sum(r['size_usdc'] for r in pos_rows)
    total_equity = available_cash + locked_cash

    dpnl = _q(
        "SELECT COALESCE(SUM(pnl), 0) AS p FROM trades "
        "WHERE exit_time >= ? AND status='CLOSED' AND mode = ?",
        (f'{today}T00:00:00', _mode())
    )
    daily_pnl = dpnl[0]['p'] if dpnl else 0.0
    # Clamp to 1.0 — an uncapped ratio would exceed 100% (e.g. a live-mode fill
    # blowing past the limit in one move) and overflow any progress-bar/gauge
    # the dashboard renders from this value.
    # Limit is DERIVED from the current stake and loss budget (config.daily_loss_limit)
    # so the meter rescales the moment either is changed in Settings.
    _dll = _config.daily_loss_limit()
    circuit_used = min(1.0, max(0.0, daily_pnl / _dll)) if _dll != 0 else 0.0
    circuit_tripped = circuit_used >= 1.0

    portfolio = {
        'mode': 'PAPER' if _config.paper_mode() else 'LIVE',
        # Archive view: the dashboard is reading the frozen paper-era snapshot,
        # not the running bot's DB. The frontend renders this unmistakably.
        'available_cash': available_cash,
        'locked_cash': locked_cash,
        'total_equity': total_equity,
        'daily_pnl': daily_pnl,
        'daily_loss_limit': _dll,
        'starting_bankroll': STARTING_BANKROLL,
        'exposure_pct': locked_cash / total_equity if total_equity else 0.0,
        'circuit_breaker_used': circuit_used,
        'circuit_tripped': circuit_tripped,
        'max_concurrent_positions': _config.setting('MAX_CONCURRENT_POSITIONS'),
        'max_total_exposure_fraction': _config.setting('MAX_TOTAL_EXPOSURE_FRACTION'),
        # Seed + every deposit = total capital put in. The return figure divides
        # by this, not starting_bankroll: a deposit adds cash without being
        # profit, so the old denominator would book a funding event as a gain.
        'total_deposited': _total_deposited(),
        # Cash taken out. P&L = equity + withdrawn - deposited; without this a
        # withdrawal would read as a trading loss of the same size.
        'total_withdrawn': _total_withdrawn(),
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
        "SELECT * FROM trades WHERE status = 'CLOSED' AND mode = ? "
        "ORDER BY exit_time DESC", (_mode(),)
    )
    # The Archive tab shows the market question under the city. It is not stored on
    # trades, so pull it from the immutable markets table in one keyed lookup
    # rather than per-row (the list is unbounded).
    question_by_market = {
        r['market_id']: r['question']
        for r in _q('SELECT market_id, question FROM markets')
    }
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
            'question': question_by_market.get(t.get('market_id')) or '',
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
            "WHERE status='CLOSED' AND exit_time >= date('now', ?) AND mode = ?",
            (f'-{days} days', _mode())
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
        # is_high IS NULL marks legacy rows whose direction could not be
        # recovered — the ones that carried conflicting actuals. Excluded so
        # the panel is not averaging a max against a min.
        "AND is_high IS NOT NULL "
        "GROUP BY model ORDER BY mae"
    )
    ma_prev = {
        r['model']: r['mae'] for r in _q(
            "SELECT model, AVG(ABS(forecast_temp - actual_temp)) AS mae "
            "FROM model_accuracy "
            "WHERE target_date >= date('now', '-60 days') AND target_date < date('now', '-30 days') "
            "AND is_high IS NOT NULL "
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
        "WHERE entry_time >= ? AND status IN ('OPEN', 'CLOSED') AND mode = ?",
        (f'{today}T00:00:00', _mode())
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
    # login.html carries a {{MODE}} placeholder that only the / route fills in;
    # serving the raw file would render the braces to the user.
    if filename == 'login.html':
        return redirect('/')
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


@app.get('/api/backup/status')
@require_auth
def api_backup_status():
    """What the off-box backup situation actually is, not what it is assumed to be."""
    from backup import backup_status
    return jsonify(backup_status())


@app.post('/api/backup/run')
@require_auth
def api_backup_run():
    """Take a snapshot now. Synchronous — the caller wants to know it worked."""
    from backup import run_backup
    res = run_backup()
    return jsonify(res), (200 if res["ok"] else 500)


@app.get('/api/backup/latest')
@require_auth
def api_backup_latest():
    """Stream the newest verified snapshot.

    This is the zero-configuration off-box path: a cron on a laptop that curls
    this endpoint with a session cookie IS the off-box destination, with no
    bucket, no credentials and no SDK. Authenticated, because the file contains
    the entire trade ledger.

    Takes a fresh snapshot when none exists yet, so the first pull after a
    deploy does not 404 and get read as "backups are broken"."""
    from backup import latest_local_snapshot, run_backup
    path = latest_local_snapshot()
    if not path:
        res = run_backup()
        if not res["ok"]:
            return jsonify(error=res["error"]), 500
        path = res["path"]
    directory, name = os.path.dirname(path), os.path.basename(path)
    return send_from_directory(directory, name, as_attachment=True,
                               mimetype='application/gzip')


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
