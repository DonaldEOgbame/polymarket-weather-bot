import traceback
import logging

from db import add_notification


def send_trade_entry(market_name, entry_price, model_prob, edge, size):
    logging.info(
        f"TRADE ENTRY | {market_name} | price=${entry_price:.3f} "
        f"prob={model_prob:.1%} edge={edge:.1%} size=${size:.2f}"
    )


def send_trade_exit(market_name, pnl_dollars, pnl_pct, duration_hours, reason):
    logging.info(
        f"TRADE EXIT | {market_name} | {reason} | "
        f"pnl=${pnl_dollars:.2f} ({pnl_pct:.1%}) held={duration_hours:.1f}h"
    )


def send_daily_summary(bankroll, open_pos_count, trades_today, win_rate, total_pnl):
    msg = (
        f"Bankroll ${bankroll:.2f} | {open_pos_count} open | "
        f"{trades_today} trades today | 30d win rate {win_rate:.1%} | "
        f"total PnL ${total_pnl:.2f}"
    )
    logging.info(f"DAILY SUMMARY | {msg}")
    add_notification("daily_summary", msg, severity="info")


def send_model_alert(city, model_count, required):
    msg = (
        f"{city}: only {model_count} forecast model(s) available "
        f"(required {required}). Forecast quality degraded — check Open-Meteo API."
    )
    logging.critical(f"MODEL ALERT | {msg}")
    add_notification("error", msg, severity="warning")


def send_circuit_breaker_alert(daily_pnl, limit):
    msg = (
        f"Circuit breaker tripped — daily loss ${daily_pnl:.2f} hit limit "
        f"${limit:.2f}. No new trades until midnight UTC."
    )
    logging.warning(f"CIRCUIT BREAKER | {msg}")
    add_notification("circuit_breaker", msg, severity="warning")


def send_error_alert(exception: Exception):
    tb = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
    logging.error(f"BOT ERROR\n{tb}")
    # Notification carries a one-line summary; the full traceback stays in logs.
    add_notification("error", f"{type(exception).__name__}: {exception}", severity="error")
