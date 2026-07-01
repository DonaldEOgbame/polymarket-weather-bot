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

DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'stormedge')
DASHBOARD_EMAIL    = os.getenv('DASHBOARD_EMAIL', 'donaldemmaogbame@gmail.com')
from config import DB_PATH, PAPER_MODE, DAILY_LOSS_LIMIT, STARTING_BANKROLL
DB_PATH = os.path.abspath(DB_PATH)

from weather import STATIONS

MODEL_META = {
    'ecmwf_ifs025':  (0.40, 'global', 'ECMWF IFS 0.25°'),
    'gfs_global':    (0.30, 'global', 'GFS Global'),
    'icon_global':   (0.25, 'global', 'ICON Global'),
    'gem_global':    (0.15, 'global', 'GEM Global'),
    'jma_gsm':       (0.30, 'AP', 'JMA GSM'),
}


# ---- DB helpers ----

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql, params=()):
    try:
        with _db() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
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
    circuit_used = max(0.0, daily_pnl / DAILY_LOSS_LIMIT) if DAILY_LOSS_LIMIT != 0 else 0.0
    circuit_tripped = circuit_used >= 1.0

    portfolio = {
        'mode': 'PAPER' if PAPER_MODE else 'LIVE',
        'available_cash': available_cash,
        'locked_cash': locked_cash,
        'total_equity': total_equity,
        'daily_pnl': daily_pnl,
        'daily_loss_limit': DAILY_LOSS_LIMIT,
        'starting_bankroll': STARTING_BANKROLL,
        'exposure_pct': locked_cash / total_equity if total_equity else 0.0,
        'circuit_breaker_used': circuit_used,
        'circuit_tripped': circuit_tripped,
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

    # ---- equity curve ----
    eq_rows = _q('SELECT timestamp, balance FROM bankroll ORDER BY id')
    equity = [{'t': r['timestamp'], 'balance': r['balance']} for r in eq_rows]
    if not equity:
        equity = [
            {'t': now.isoformat(), 'balance': STARTING_BANKROLL},
            {'t': now.isoformat(), 'balance': total_equity},
        ]

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

    last_scan_ts = scan_rows[0]['timestamp'] if scan_rows else now.isoformat()
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
        'equity': equity,
        'stats': stats,
        'models': models,
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
    except Exception as e:
        logging.error(f"Bot thread fatal error: {e}", exc_info=True)


if __name__ == '__main__':
    port = int(os.getenv('PORT', 7777))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'

    bot_thread = threading.Thread(target=_start_bot, daemon=True, name='bot')
    bot_thread.start()
    logging.info('Bot started in background thread.')

    print(f'  stormedge → http://localhost:{port}')
    print(f'  password:    {DASHBOARD_PASSWORD}')
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)
