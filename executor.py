import json
import logging
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from db import execute_query, fetch_query, update_bankroll, get_open_position, close_position_atomic
from alerts import send_trade_entry, send_trade_exit, send_model_alert
from scanner import get_realtime_price, get_market_resolution
from config import (
    PAPER_MODE, POLYMARKET_PK, CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    MAX_CONCURRENT_POSITIONS, STOP_LOSS_PCT, EXIT_EDGE_FLOOR, CLOB_BASE_URL,
    MIN_MODEL_COUNT,
)


class Executor:
    def __init__(self):
        self.client = None
        if not PAPER_MODE:
            self.client = ClobClient(CLOB_BASE_URL, key=POLYMARKET_PK, chain_id=137)
            self.client.set_api_creds(self.client.create_api_credential(
                CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE
            ))
        self.reconcile_positions()

    def reconcile_positions(self):
        positions = fetch_query("SELECT * FROM positions")
        if not positions:
            logging.info("Startup reconciliation: no open positions.")
            return

        logging.warning(
            f"Startup reconciliation: found {len(positions)} open position(s) from previous run. "
            f"Market IDs: {[p['market_id'] for p in positions]}"
        )

        for pos in positions:
            settled = self._try_settle_position(pos, source="reconcile")
            if settled:
                continue

            ask, bid = get_realtime_price(pos["token_id"])
            if ask <= 0 and bid <= 0:
                logging.warning(
                    f"Reconcile: {pos['market_id']} ({pos['side']}) has no orderbook and resolution status "
                    f"is unclear (API may be down). Leaving open for next monitor cycle."
                )
            else:
                logging.info(
                    f"Reconcile: {pos['market_id']} ({pos['side']}) is still active. "
                    f"Entry: ${pos['entry_price']:.3f} | Ask: ${ask:.3f} | Bid: ${bid:.3f}"
                )

    def check_resolved_positions(self):
        """Poll Polymarket for resolution status of every open position. Settle any
        that have resolved. Called every monitor cycle so winning trades close at $1.00
        and losers at $0.00 without waiting for edge decay."""
        positions = fetch_query("SELECT * FROM positions")
        settled_count = 0
        for pos in positions:
            if self._try_settle_position(pos, source="monitor"):
                settled_count += 1
        if settled_count:
            logging.info(f"Resolution check: {settled_count} position(s) settled this cycle")
        return settled_count

    def _try_settle_position(self, pos, source="monitor"):
        """If Polymarket reports this position's market as resolved, close it with
        the correct PnL and write a resolution row. Returns True if settled."""
        market_id = pos["market_id"]
        side = pos["side"]
        entry_price = pos["entry_price"]
        size_usdc = pos["size_usdc"]

        resolution = get_market_resolution(market_id)
        if not (resolution and resolution["resolved"]):
            return False

        outcome = resolution["outcome"]
        logging.info(f"{source.capitalize()}: {market_id} ({side}) RESOLVED on Polymarket. Outcome: {outcome}")

        if outcome is None:
            pnl = -size_usdc
            exit_reason = "RESOLVED_UNKNOWN_OUTCOME"
            won = False
        elif outcome == side:
            shares = size_usdc / entry_price if entry_price > 0 else 0
            pnl = shares * 1.0 - size_usdc
            exit_reason = f"RESOLVED_WIN ({outcome})"
            won = True
        else:
            pnl = -size_usdc
            exit_reason = f"RESOLVED_LOSS ({outcome})"
            won = False

        logging.info(
            f"{source.capitalize()} settlement: {market_id} ({side}) | "
            f"entry=${entry_price:.3f} size=${size_usdc:.2f} | PnL=${pnl:.2f} | {exit_reason}"
        )

        self._write_resolution_row(pos, outcome, won, pnl)
        self._close_position(pos, pnl_dollars=pnl, exit_reason=exit_reason)
        return True

    def _write_resolution_row(self, pos, outcome, won, pnl):
        """Record settlement + Brier score in resolutions table. The Brier component
        is per-side: brier = (won - model_prob_for_chosen_side)^2."""
        try:
            trade = fetch_query(
                "SELECT model_prob FROM trades WHERE market_id=? AND side=? AND status='OPEN' "
                "ORDER BY id DESC LIMIT 1",
                (pos["market_id"], pos["side"])
            )
            model_prob_entry = trade[0]["model_prob"] if trade else None
            # model_prob is the probability our model assigned to the bucket (YES).
            # For NO trades, the prob we bet on is 1 - model_prob.
            if model_prob_entry is None:
                prob_for_side = None
                brier = None
            else:
                prob_for_side = model_prob_entry if pos["side"] == "YES" else (1.0 - model_prob_entry)
                brier = (float(won) - prob_for_side) ** 2

            execute_query(
                "INSERT INTO resolutions (market_id, resolved_at, outcome, "
                "model_prob_at_entry, pnl, side, won, brier, city, target_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pos["market_id"],
                    datetime.now(timezone.utc).isoformat(),
                    outcome,
                    model_prob_entry,
                    pnl,
                    pos["side"],
                    1 if won else 0,
                    brier,
                    pos.get("city"),
                    pos.get("target_date"),
                )
            )
            if brier is not None:
                logging.info(
                    f"Resolution logged: {pos['market_id']} won={won} "
                    f"model_prob_for_side={prob_for_side:.3f} brier={brier:.4f}"
                )
        except Exception as e:
            logging.error(f"Failed to write resolution row for {pos['market_id']}: {e}", exc_info=True)

    def get_open_positions_count(self):
        res = fetch_query("SELECT COUNT(*) as count FROM positions")
        return res[0]["count"] if res else 0

    def execute_trade(self, signal_data):
        opp = signal_data["opp"]

        if get_open_position(opp.market_id):
            logging.info(f"Already holding position in {opp.market_id} — skipping")
            return

        if self.get_open_positions_count() >= MAX_CONCURRENT_POSITIONS:
            logging.info(f"Max {MAX_CONCURRENT_POSITIONS} concurrent positions reached, skipping entry.")
            return

        # Alert if model count was low for this signal (degraded confidence)
        model_count = signal_data.get("model_count", MIN_MODEL_COUNT)
        if model_count < MIN_MODEL_COUNT:
            send_model_alert(opp.city, model_count, MIN_MODEL_COUNT)

        side = signal_data["side"]
        size = signal_data["size_usdc"]
        price = round(min(signal_data["price"] + 0.01, 0.99), 2)
        shares = round(size / price, 2)

        logging.info(
            f"Executing {'PAPER ' if PAPER_MODE else ''}trade: "
            f"BUY {shares} shares of {opp.market_id} {side} @ ${price:.3f} "
            f"(size=${size:.2f}, edge={signal_data['edge']:.3f}, prob={signal_data['model_prob']:.3f})"
        )

        if not PAPER_MODE:
            order_args = OrderArgs(
                price=price, size=shares, side="BUY", token_id=signal_data["token_id"]
            )
            try:
                resp = self.client.create_and_post_order(order_args)
                logging.info(f"Order response: {resp}")
            except Exception as e:
                logging.error(f"Failed to place order: {e}")
                return

        now_iso = datetime.now(timezone.utc).isoformat()
        execute_query(
            "INSERT INTO positions (market_id, token_id, side, entry_price, size_usdc, "
            "entry_time, question, is_high, city, target_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (opp.market_id, signal_data["token_id"], side, price, size,
             now_iso, opp.question, 1 if opp.is_high else 0, opp.city, opp.date)
        )
        trade_id = execute_query(
            "INSERT INTO trades (market_id, side, size_usdc, fill_price, model_prob, edge, "
            "status, entry_time, is_high, city, target_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (opp.market_id, side, size, price, signal_data["model_prob"],
             signal_data["edge"], "OPEN", now_iso,
             1 if opp.is_high else 0, opp.city, opp.date)
        )
        update_bankroll("TRADE_ENTRY", -size, trade_id)
        send_trade_entry(opp.question, price, signal_data["model_prob"], signal_data["edge"], size)

    def get_live_prices(self):
        """Return {market_id: current_mid_price} for all open positions."""
        positions = fetch_query("SELECT market_id, token_id FROM positions")
        prices = {}
        for p in positions:
            ask, bid = get_realtime_price(p["token_id"])
            if ask > 0 and bid > 0:
                prices[p["market_id"]] = (ask + bid) / 2.0
            elif ask > 0 or bid > 0:
                prices[p["market_id"]] = ask or bid
        return prices

    def check_exits(self):
        positions = fetch_query("SELECT * FROM positions")
        for pos in positions:
            self._check_exit_for_position(pos)

    def _check_exit_for_position(self, pos):
        entry_time = datetime.fromisoformat(pos["entry_time"])
        now = datetime.now(timezone.utc)
        hold_minutes = (now - entry_time).total_seconds() / 60.0
        if hold_minutes < 30:
            return

        ask_price, bid_price = get_realtime_price(pos["token_id"])

        if ask_price > 0 and bid_price > 0:
            current_price = (ask_price + bid_price) / 2.0
        else:
            current_price = ask_price or bid_price

        if current_price <= 0.0:
            return

        entry_price = pos["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
        pnl_dollars = (current_price - entry_price) * (pos["size_usdc"] / entry_price)

        exit_reason = None

        if pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = f"Stop Loss ({pnl_pct:.1%})"
        else:
            signals = fetch_query(
                "SELECT model_prob, target_date FROM signals WHERE market_id=? ORDER BY id DESC LIMIT 1",
                (pos["market_id"],)
            )
            if signals:
                latest_prob = signals[0]["model_prob"]
                if pos["side"] == "YES":
                    current_edge = latest_prob - current_price
                else:
                    current_edge = (1.0 - latest_prob) - current_price

                # Time-adaptive exit floor: raise threshold in the final 4 hours before
                # resolution when volatility spikes and late-market chop can whipsaw exits.
                target_date_str = signals[0].get("target_date")
                adaptive_floor = self._adaptive_exit_floor(target_date_str, now)

                if current_edge < adaptive_floor:
                    exit_reason = (
                        f"Edge decayed ({current_edge:.3f} < {adaptive_floor:.3f}"
                        + (" [late-market]" if adaptive_floor > EXIT_EDGE_FLOOR else "") + ")"
                    )

        if exit_reason:
            self._close_position(pos, pnl_dollars, exit_reason)

    def _adaptive_exit_floor(self, target_date_str, now):
        """Scale EXIT_EDGE_FLOOR upward as resolution approaches.
        Final 2h: 2x floor. Final 4h: 1.5x floor. Otherwise: base floor."""
        if not target_date_str:
            return EXIT_EDGE_FLOOR
        try:
            from utils import parse_utc_datetime
            target_dt = parse_utc_datetime(target_date_str + "T23:59:00+00:00")
            hours_left = (target_dt - now).total_seconds() / 3600.0
            if hours_left <= 2:
                return EXIT_EDGE_FLOOR * 2.0
            if hours_left <= 4:
                return EXIT_EDGE_FLOOR * 1.5
        except Exception:
            pass
        return EXIT_EDGE_FLOOR

    def _close_position(self, pos, pnl_dollars, exit_reason):
        logging.info(
            f"{'PAPER ' if PAPER_MODE else ''}EXIT: {pos['market_id']} ({pos['side']}) — "
            f"{exit_reason} | PnL: ${pnl_dollars:.2f}"
        )

        skip_clob_exit = exit_reason == "EXPIRED_ON_RESTART" or exit_reason.startswith("RESOLVED_")
        if not PAPER_MODE and not skip_clob_exit:
            _, bid_price = get_realtime_price(pos["token_id"])
            if bid_price > 0:
                shares = round(pos["size_usdc"] / pos["entry_price"], 2)
                order_args = OrderArgs(
                    price=bid_price, size=shares, side="SELL", token_id=pos["token_id"]
                )
                try:
                    self.client.create_and_post_order(order_args)
                except Exception as e:
                    logging.error(f"Failed to place exit order: {e}")
                    return

        closed = close_position_atomic(
            pos_id=pos["id"],
            market_id=pos["market_id"],
            side=pos["side"],
            pnl_dollars=pnl_dollars,
            size_usdc=pos["size_usdc"],
            exit_reason=exit_reason,
        )
        if not closed:
            logging.warning(f"Position {pos['id']} already closed by another thread — skipping duplicate close")
            return

        entry_time = datetime.fromisoformat(pos["entry_time"])
        duration_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600.0
        pnl_pct = pnl_dollars / pos["size_usdc"] if pos["size_usdc"] > 0 else 0
        market_label = pos.get("question") or pos["market_id"]
        send_trade_exit(market_label, pnl_dollars, pnl_pct, duration_hours, exit_reason)
