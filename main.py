import time
import signal
import gc
import logging
import json
from datetime import datetime, timezone
from db import (
    init_db, fetch_query, get_portfolio_state, get_daily_pnl, execute_query,
    purge_old_signals, purge_old_scan_log, purge_old_notifications,
)
from scanner import scan_markets, verify_parser_fixtures
from strategy import evaluate_opportunity
from executor import Executor
from alerts import send_daily_summary, send_error_alert, send_circuit_breaker_alert
from config import SCAN_INTERVAL_MINUTES, MONITOR_INTERVAL_MINUTES, DAILY_LOSS_LIMIT
from weather import log_model_accuracy, get_station_coords, prefetch_signal_engines
from metar import resolved_extreme_f
from utils import get_session
import schedule

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage()
        }
        if record.exc_info:
            log_record["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(handler)

logging.getLogger("urllib3").setLevel(logging.WARNING)

executor = None
running = True

# Tracks circuit-breaker state across scan cycles so the dashboard notification
# fires once on the transition into tripped — not every 10-minute cycle.
_circuit_tripped = False

def handle_sigterm(*args):
    global running
    logging.info("SIGTERM received, initiating graceful shutdown (not opening new positions).")
    running = False

def check_circuit_breaker():
    global _circuit_tripped
    daily_pnl = get_daily_pnl()
    if daily_pnl <= DAILY_LOSS_LIMIT:
        logging.warning(f"Circuit breaker tripped. Daily loss ${daily_pnl:.2f} hit limit ${DAILY_LOSS_LIMIT:.2f}. No new trades until midnight UTC.")
        # Notify only on the edge (untripped -> tripped) to avoid a feed flood.
        if not _circuit_tripped:
            send_circuit_breaker_alert(daily_pnl, DAILY_LOSS_LIMIT)
            _circuit_tripped = True
        return True
    # PnL recovered above the limit (e.g. new UTC day) — re-arm the notification.
    _circuit_tripped = False
    return False

def check_resolutions():
    """Log model accuracy for every (city, target_date) we've traded once that date
    has passed. Decoupled from trade closure — runs on any trade where
    resolution_logged=0 and target_date <= today. Both open and closed trades qualify;
    Polymarket resolution and model-accuracy logging are independent."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        unlogged = fetch_query(
            "SELECT DISTINCT t.id, t.market_id, t.is_high, t.city, t.target_date "
            "FROM trades t WHERE t.resolution_logged=0 AND t.target_date IS NOT NULL "
            "AND t.target_date <= ?",
            (today,)
        )
        if not unlogged:
            return

        for t in unlogged:
            market_id = t["market_id"]
            city = t.get("city")
            target_date = t.get("target_date")
            is_high_val = t.get("is_high")

            # Get the latest raw_models snapshot for this market
            sig = fetch_query(
                "SELECT raw_models FROM signals WHERE market_id=? AND raw_models IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (market_id,)
            )
            if not sig or not city or not target_date:
                execute_query("UPDATE trades SET resolution_logged=1 WHERE id=?", (t["id"],))
                continue

            try:
                raw_models = json.loads(sig[0]["raw_models"] or "{}")
            except Exception:
                raw_models = {}

            _, coords = get_station_coords(city)
            if not coords:
                execute_query("UPDATE trades SET resolution_logged=1 WHERE id=?", (t["id"],))
                continue

            # is_high is now stored on the trade; fall back to high if missing.
            is_high = bool(is_high_val) if is_high_val is not None else True

            try:
                # Verify against the METAR feed — the SAME source Polymarket resolves
                # against (Wunderground = airport METAR). Learning per-model bias
                # against ERA5 taught the model to hit the wrong ruler; the actual is
                # rounded to whole °C to match resolution precision.
                actual_temp = resolved_extreme_f(city, target_date, is_high)
                if actual_temp is not None:
                    for model_name, forecast_temp in raw_models.items():
                        log_model_accuracy(city, target_date, model_name, forecast_temp, actual_temp)
                    execute_query("UPDATE trades SET resolution_logged=1 WHERE id=?", (t["id"],))
                    logging.info(
                        f"Model accuracy logged (METAR) for {city} {target_date}: actual={actual_temp:.1f}°F"
                    )
                else:
                    logging.debug(
                        f"METAR not yet published for {city} {target_date} — will retry next cycle"
                    )
            except Exception as e:
                logging.error(f"Error fetching historical resolution for {city} {target_date}: {e}")
    except Exception as e:
        logging.error(f"Error in check_resolutions: {e}", exc_info=True)

def run_scan_cycle():
    if not running:
        return

    if check_circuit_breaker():
        return

    try:
        portfolio_state = get_portfolio_state()
        opportunities = scan_markets()

        weather_cache = prefetch_signal_engines(opportunities)

        traded = 0
        skipped = 0
        for opp in opportunities:
            if not running:
                break
            engine_res = weather_cache.get((opp.city, opp.date, opp.is_high))
            signal_data = evaluate_opportunity(opp, portfolio_state, engine_res=engine_res)
            if signal_data and signal_data["signal"]:
                executor.execute_trade(signal_data)
                portfolio_state = get_portfolio_state()
                traded += 1
            else:
                skipped += 1

        logging.info(
            f"Scan done — {len(opportunities)} candidates | {traded} traded | {skipped} skipped | "
            f"cash=${portfolio_state['available_cash']:.2f} locked=${portfolio_state['locked_cash']:.2f}"
        )
    except Exception as e:
        logging.error(f"Error in scan cycle: {e}", exc_info=True)
        send_error_alert(e)
    finally:
        gc.collect()

def run_monitor_cycle():
    try:
        open_count = executor.get_open_positions_count()
        # 1. Settle any positions whose markets have resolved on Polymarket
        executor.check_resolved_positions()
        # 2. Check exit triggers (stop-loss, edge decay) on whatever remains open
        executor.check_exits()
        still_open = executor.get_open_positions_count()
        closed = open_count - still_open
        if closed > 0 or open_count > 0:
            logging.info(f"Monitor — {open_count} open position(s), {closed} exit(s) triggered")
    except Exception as e:
        logging.error(f"Error in monitor cycle: {e}", exc_info=True)
        send_error_alert(e)
    finally:
        gc.collect()

def _daily_purge():
    try:
        purge_old_signals()
        purge_old_scan_log()
        from config import NOTIFICATION_RETENTION_DAYS
        purge_old_notifications(NOTIFICATION_RETENTION_DAYS)
        logging.info("Daily DB purge complete.")
    except Exception as e:
        logging.error(f"Error in daily purge: {e}", exc_info=True)


def daily_summary():
    try:
        portfolio_state = get_portfolio_state()
        open_pos_count = executor.get_open_positions_count()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades_today_query = fetch_query(
            "SELECT COUNT(*) as c FROM trades WHERE entry_time >= ?",
            (f"{today}T00:00:00",)
        )
        trades_today = trades_today_query[0]["c"] if trades_today_query else 0

        win_rate_query = fetch_query(
            "SELECT COUNT(*) as total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
            "FROM trades WHERE status='CLOSED' AND exit_time >= date('now', '-30 days')"
        )
        total = win_rate_query[0]["total"] if win_rate_query else 0
        wins = win_rate_query[0]["wins"] or 0 if win_rate_query else 0
        win_rate = wins / total if total > 0 else 0.0

        pnl_query = fetch_query("SELECT SUM(pnl) as tpnl FROM trades WHERE status='CLOSED'")
        total_pnl = pnl_query[0]["tpnl"] if pnl_query and pnl_query[0]["tpnl"] is not None else 0.0

        # Brier score over resolved trades (lower = better calibrated; 0.25 = no skill)
        brier_query = fetch_query(
            "SELECT AVG(brier) as avg_brier, COUNT(*) as n FROM resolutions WHERE brier IS NOT NULL"
        )
        if brier_query and brier_query[0]["n"]:
            avg_brier = brier_query[0]["avg_brier"]
            n_resolved = brier_query[0]["n"]
            logging.info(
                f"CALIBRATION | n={n_resolved} resolved trades | Brier={avg_brier:.4f} "
                f"(no-skill=0.25, perfect=0.0)"
            )

        send_daily_summary(portfolio_state["total_equity"], open_pos_count, trades_today, win_rate, total_pnl)
    except Exception as e:
        logging.error(f"Error in daily summary: {e}", exc_info=True)

def _print_startup_summary():
    from config import (
        PAPER_MODE, STARTING_BANKROLL, EDGE_THRESHOLD, MIN_MODEL_AGREEMENT,
        MAX_MODEL_SPREAD, KELLY_CAP, HARD_MAX_POSITION_SIZE, MIN_POSITION_SIZE,
        MAX_CONCURRENT_POSITIONS, DAILY_LOSS_LIMIT, MAX_HOURS_TO_RESOLUTION,
        MIN_VOLUME, MARKET_DISCOVERY_MAX_PAGES, MARKET_DISCOVERY_LIMIT,
        SHADOW_MIN_AGREEMENT, SHADOW_MAX_SPREAD, SHADOW_MAX_SIZE_USDC,
        ENABLE_SHADOW_EXPLORATION, TAKER_FEE_RATE, SLIPPAGE_FRACTION,
    )
    portfolio = get_portfolio_state()
    open_pos = fetch_query("SELECT COUNT(*) as c FROM positions")[0]["c"]

    mode = "PAPER" if PAPER_MODE else "LIVE"
    shadow_label = f"exploration ON (max ${SHADOW_MAX_SIZE_USDC:.2f})" if ENABLE_SHADOW_EXPLORATION else "log only"
    lines = [
        "",
        "=" * 52,
        f"  Polymarket Weather Bot  [{mode} MODE]",
        "=" * 52,
        f"  Bankroll     : ${portfolio['total_equity']:.2f}  (cash ${portfolio['available_cash']:.2f}  locked ${portfolio['locked_cash']:.2f})",
        f"  Open pos     : {open_pos} / {MAX_CONCURRENT_POSITIONS}  |  Daily loss limit: ${DAILY_LOSS_LIMIT:.2f}",
        f"  Edge thresh  : {EDGE_THRESHOLD:.0%} (net of taker fee {TAKER_FEE_RATE:.0%}·p·(1-p) + {SLIPPAGE_FRACTION:.1%} slippage)  |  Kelly cap: {KELLY_CAP:.0%}  |  Max size: ${HARD_MAX_POSITION_SIZE:.2f}",
        f"  Strict gates : agreement ≥ {MIN_MODEL_AGREEMENT:.0%}  |  spread < {MAX_MODEL_SPREAD}°F",
        f"  Shadow gates : agreement ≥ {SHADOW_MIN_AGREEMENT:.0%}  |  spread < {SHADOW_MAX_SPREAD}°F  |  {shadow_label}",
        f"  Market filter: vol ≥ ${MIN_VOLUME:,.0f}  |  ≤ {MAX_HOURS_TO_RESOLUTION:.0f}h to resolution",
        f"  Discovery    : {MARKET_DISCOVERY_MAX_PAGES} pages × {MARKET_DISCOVERY_LIMIT} events  (tag_id=84, weather)",
        f"  Schedule     : scan every {SCAN_INTERVAL_MINUTES}m  |  monitor every {MONITOR_INTERVAL_MINUTES}m",
        "=" * 52,
        "",
    ]
    for line in lines:
        logging.info(line)


def run_bot(in_thread=False):
    """Run the full trading bot: init, prime cycles, schedule jobs, then loop.

    This is the single bot implementation — used both by main() (standalone
    `python main.py`) and by app.py's background thread, so the loop logic
    lives in exactly one place.

    in_thread=True  — called from a daemon thread (e.g. app.py). Signal handlers
                      are skipped (only the main thread may register them) and
                      the schedule library's own logger is left as-is.
    in_thread=False — standalone process: registers SIGTERM/SIGINT for graceful
                      shutdown.
    """
    global executor

    # Suppress noisy sub-loggers
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("urllib3.util.retry").setLevel(logging.ERROR)
    logging.getLogger("schedule").setLevel(logging.WARNING)

    verify_parser_fixtures()
    init_db()
    executor = Executor()
    _print_startup_summary()

    run_scan_cycle()
    run_monitor_cycle()
    check_resolutions()

    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(run_scan_cycle)
    schedule.every(MONITOR_INTERVAL_MINUTES).minutes.do(run_monitor_cycle)
    schedule.every(60).minutes.do(check_resolutions)
    schedule.every().day.at("08:00").do(daily_summary)
    schedule.every().day.at("03:00").do(_daily_purge)

    while True:
        schedule.run_pending()
        time.sleep(1)
        if not running and executor.get_open_positions_count() == 0:
            logging.info("Graceful shutdown complete.")
            break


def main():
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    run_bot(in_thread=False)


if __name__ == "__main__":
    main()
