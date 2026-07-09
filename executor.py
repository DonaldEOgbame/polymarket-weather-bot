import json
import logging
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from db import execute_query, fetch_query, update_bankroll, get_open_position, close_position_atomic
from alerts import send_trade_entry, send_trade_exit, send_model_alert
from scanner import get_realtime_price, get_market_resolution, get_gamma_mid_price
from zoneinfo import ZoneInfo
from weather import get_signal_engine, get_bucket_probability, _norm_cdf
from metar import get_station, fetch_day_extremes
from config import (
    PAPER_MODE, POLYMARKET_PK, CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    MAX_CONCURRENT_POSITIONS, STOP_LOSS_PCT, ENABLE_STOP_LOSS, EXIT_EDGE_FLOOR, CLOB_BASE_URL,
    MIN_MODEL_COUNT, TAKER_FEE_RATE,
    HOLD_WINNERS_TO_RESOLUTION, THESIS_BREAK_PROB_DELTA, TAKE_PROFIT_PRICE, SUSTAINED_LOSS_POLLS,
    SUSTAINED_LOSS_MIN_DROP, REENTRY_COOLDOWN_HOURS,
    ENABLE_SUSTAINED_LOSS_GUARD, ENABLE_THESIS_BREAK_EXIT,
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
        # Tracks consecutive below-entry mid-price polls per position id.
        # Reset on price recovery. Used by the sustained-loss guard in _check_exit_for_position.
        self._loss_streak: dict = {}

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

        target_date = pos.get("target_date")
        if target_date:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if target_date > today:
                return False

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

    def _read_fill(self, resp, order_id, fallback_price):
        """Determine the ACTUAL matched size (shares) and average fill price for a
        just-submitted order. The POST /order response schema is not contractually
        stable, so the order record (get_order) is treated as the source of truth.
        Returns (filled_shares, avg_price). filled_shares == 0 means nothing filled.

        NOTE: verify these field names against the raw response logged below on the
        FIRST real fill — adjust if Polymarket's schema differs in this client version.
        """
        shares, price = 0.0, None
        if order_id:
            try:
                o = self.client.get_order(order_id)
                if isinstance(o, dict):
                    sm = float(o.get("size_matched") or 0)
                    if sm > 0:
                        shares = sm
                        price = float(o.get("price") or 0) or fallback_price
            except Exception as e:
                logging.error(f"get_order({order_id}) failed during fill confirmation: {e}")
        return shares, price

    def _submit_taker(self, token_id, side, amount):
        """Place a Fill-And-Kill MARKET order (taker). For BUY, `amount` is USDC to
        spend (Polymarket market-order min $1); for SELL, `amount` is shares. The
        client walks the book to price it, so it either takes immediately or is
        killed — never rests as a phantom open order. Returns {shares, price,
        fee_bps} on a real fill, or None if nothing filled. Live mode only."""
        try:
            fee_bps = self.client.get_fee_rate_bps(token_id)
        except Exception:
            fee_bps = None
        try:
            signed = self.client.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount, side=side,
                                order_type=OrderType.FAK)
            )
            resp = self.client.post_order(signed, OrderType.FAK)
        except Exception as e:
            logging.error(f"Market order failed ({side} amount={amount} tok={token_id}): {e}")
            return None
        # Log the raw response verbatim — this is how we confirm the schema on the first real fill.
        logging.info(f"RAW order response [{side} {token_id}]: {resp}")
        order_id = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
        filled, avg = self._read_fill(resp, order_id, None)
        if filled <= 0 or not avg:
            logging.warning(f"{side} market order did not fill; booking nothing. resp={resp}")
            return None
        return {"shares": filled, "price": avg, "fee_bps": fee_bps}

    def execute_trade(self, signal_data):
        opp = signal_data["opp"]

        if get_open_position(opp.market_id):
            logging.info(f"Already holding position in {opp.market_id} — skipping")
            return

        # Re-entry cooldown: don't re-open a market we recently EXITED. Without this the
        # bot churns — a position force-closed on noise gets re-bought on the next scan,
        # paying spread+fee each round-trip (the Guangzhou market took 9 entries this way).
        if REENTRY_COOLDOWN_HOURS > 0:
            last = fetch_query(
                "SELECT exit_time FROM trades WHERE market_id=? AND exit_time IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (opp.market_id,),
            )
            if last and last[0]["exit_time"]:
                try:
                    exited = datetime.fromisoformat(last[0]["exit_time"])
                    if exited.tzinfo is None:
                        exited = exited.replace(tzinfo=timezone.utc)
                    hrs = (datetime.now(timezone.utc) - exited).total_seconds() / 3600.0
                    if hrs < REENTRY_COOLDOWN_HOURS:
                        logging.info(
                            f"Re-entry cooldown active for {opp.market_id}: exited "
                            f"{hrs:.1f}h ago (< {REENTRY_COOLDOWN_HOURS}h) — skipping"
                        )
                        return
                except (ValueError, TypeError):
                    pass  # unparseable timestamp — don't block entry on it

        if self.get_open_positions_count() >= MAX_CONCURRENT_POSITIONS:
            logging.info(f"Max {MAX_CONCURRENT_POSITIONS} concurrent positions reached, skipping entry.")
            return

        # Alert if model count was low for this signal (degraded confidence)
        model_count = signal_data.get("model_count", MIN_MODEL_COUNT)
        if model_count < MIN_MODEL_COUNT:
            send_model_alert(opp.city, model_count, MIN_MODEL_COUNT)

        side = signal_data["side"]
        size = signal_data["size_usdc"]
        # Paper assumes a fill at the quote + 1¢. Live crosses the real ask and
        # records whatever ACTUALLY fills (price + size), so the ledger and the
        # measured cost reflect real execution, not an assumption.
        price = round(min(signal_data["price"] + 0.01, 0.99), 2)
        shares = round(size / price, 2)

        if not PAPER_MODE:
            logging.info(
                f"Executing LIVE trade: BUY ${size:.2f} of {opp.market_id} {side} "
                f"(target=${signal_data['price']:.2f}, edge={signal_data['edge']:.3f})"
            )
            fill = self._submit_taker(signal_data["token_id"], "BUY", size)  # amount = USDC
            if not fill:
                return  # nothing filled → no phantom position
            price = round(fill["price"], 4)
            shares = fill["shares"]
            size = round(shares * price, 2)                  # actual USDC deployed
            slip = price - signal_data["price"]
            logging.info(
                f"FILLED {opp.market_id} {side}: {shares} sh @ ${price:.4f} "
                f"= ${size:.2f} | slippage vs target {slip:+.4f} | fee_bps={fill['fee_bps']}"
            )
        else:
            logging.info(
                f"Executing PAPER trade: BUY {shares} shares of {opp.market_id} {side} @ ${price:.3f} "
                f"(size=${size:.2f}, edge={signal_data['edge']:.3f}, prob={signal_data['model_prob']:.3f})"
            )

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

    @staticmethod
    def _target_date_passed(target_date, now):
        """True if this position's weather target date is in the past (UTC).
        Once it has passed, the outcome is fixed and only resolution ($1/$0)
        should close the position — a paper market-exit at a stale/thin quote
        would fabricate a fill (see _check_exit_for_position)."""
        if not target_date:
            return False
        return target_date < now.strftime("%Y-%m-%d")

    def _check_exit_for_position(self, pos):
        entry_time = datetime.fromisoformat(pos["entry_time"])
        now = datetime.now(timezone.utc)
        hold_minutes = (now - entry_time).total_seconds() / 60.0
        if hold_minutes < 30:
            return

        # Once the target date has passed the temperature is already realized and
        # the market is converging to $1/$0. Do NOT run the paper edge-decay /
        # stop-loss market-exit path here: on a resolving book the only resting
        # quotes are extreme (~0.999) with no real depth, and booking a fill there
        # fabricated the 5 "edge decayed @ 0.999" exits in the historical DB —
        # phantom fills at a price never once observed with size (max NO in 44,879
        # logged signals was 0.81). Leave it for check_resolved_positions() to
        # settle at the true resolution value instead.
        if self._target_date_passed(pos.get("target_date"), now):
            logging.debug(
                f"Exit check skipped for {pos['market_id']} ({pos['side']}): target "
                f"date {pos.get('target_date')} passed — holding for resolution settlement."
            )
            return

        ask_price, bid_price = get_realtime_price(pos["token_id"])

        if ask_price > 0 and bid_price > 0:
            current_price = (ask_price + bid_price) / 2.0
        else:
            current_price = ask_price or bid_price

        used_gamma_fallback = False
        if current_price <= 0.0:
            # CLOB book unreadable (empty/thin book, rate limit, network hiccup).
            # Previously this just returned — silently skipping the exit check
            # entirely, which meant a position sitting at a real, extreme price
            # (e.g. 99%+) could sit un-exited indefinitely if its order book
            # happened to be empty at read time. Fall back to Gamma's last-known
            # price so the edge-decay decision below still runs; this is NOT
            # treated as a real fillable bid (see exit_fill below).
            gamma_price = get_gamma_mid_price(pos["market_id"], pos["side"])
            if gamma_price is None or gamma_price <= 0.0:
                return
            current_price = gamma_price
            used_gamma_fallback = True

        if used_gamma_fallback:
            logging.warning(
                f"CLOB book unreadable for {pos['market_id']} ({pos['side']}) — "
                f"using Gamma fallback price ${current_price:.4f} for exit check"
            )

        entry_price = pos["entry_price"]
        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
        # Book the exit at a realistic taker fill: sell into the BID (which captures
        # the spread) minus the Polymarket taker fee — not the optimistic mid. The
        # mid (current_price) is still used for the edge-decay decision below.
        # Resolution exits settle exactly ($1/$0) and never reach this path; live
        # mode overrides pnl from the actual fill in _close_position. If we're on
        # the Gamma fallback, bid_price is 0 (no real book), so exit_fill correctly
        # falls through to current_price (the Gamma estimate) rather than a
        # fabricated bid — this is an estimate, not a guaranteed fill price.
        shares = pos["size_usdc"] / entry_price if entry_price > 0 else 0
        exit_fill = bid_price if bid_price > 0 else current_price
        exit_fee = TAKER_FEE_RATE * exit_fill * (1.0 - exit_fill) * shares
        pnl_dollars = (exit_fill - entry_price) * shares - exit_fee

        exit_reason = None

        # --- Sustained-loss guard (independent of edge formula) ---
        # DISABLED by default (ENABLE_SUSTAINED_LOSS_GUARD=false). Backtest on the first 22
        # trades showed even a 10% floor would exit 4 eventual winners for every 1 real loss
        # avoided — same-day weather books wobble 15-25% intraday then recover. Kept intact
        # behind the flag to re-enable once a larger sample justifies it. When on: track how
        # many consecutive polls the mid sat ≥SUSTAINED_LOSS_MIN_DROP below entry, then exit.
        if not hasattr(self, '_loss_streak'):
            self._loss_streak = {}  # safety: Executor.__new__ skips __init__ in tests
        pos_key = pos.get("id", pos.get("market_id"))
        if ENABLE_SUSTAINED_LOSS_GUARD and pnl_pct <= -SUSTAINED_LOSS_MIN_DROP:
            self._loss_streak[pos_key] = self._loss_streak.get(pos_key, 0) + 1
        else:
            self._loss_streak.pop(pos_key, None)
        streak = self._loss_streak.get(pos_key, 0)
        if ENABLE_SUSTAINED_LOSS_GUARD and streak >= SUSTAINED_LOSS_POLLS:
            exit_reason = (
                f"Sustained loss ({streak} polls below entry, "
                f"mid=${current_price:.3f} vs entry=${entry_price:.3f}, pnl={pnl_pct:.1%})"
            )
        elif ENABLE_STOP_LOSS and pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = f"Stop Loss ({pnl_pct:.1%})"
        elif exit_fill >= TAKE_PROFIT_PRICE:
            exit_reason = f"Take Profit (Price {exit_fill:.2f} >= {TAKE_PROFIT_PRICE:.2f})"
        else:
            signals = fetch_query(
                "SELECT bucket_low, bucket_high, target_date, model_prob FROM signals "
                "WHERE market_id=? ORDER BY id DESC LIMIT 1",
                (pos["market_id"],)
            )
            if signals:
                # Re-run the ensemble live rather than trusting the cached model_prob
                # from signals — that value is frozen at whichever scan last touched
                # this market and does NOT track new forecast data arriving after entry.
                # A stale prob makes current_edge look permanently favorable even after
                # the market (and the weather) has genuinely moved against the position,
                # so the edge-decay exit silently stops firing exactly when it matters most.
                latest_prob = signals[0]["model_prob"]  # fallback if live refresh fails
                engine_res = get_signal_engine(
                    pos["city"], pos["target_date"], bool(pos["is_high"])
                )
                if engine_res:
                    fresh_prob = get_bucket_probability(
                        engine_res, signals[0]["bucket_low"], signals[0]["bucket_high"]
                    )
                    latest_prob = fresh_prob

                    # Real-time observations check (incorporate METAR on the target day)
                    icao, tz = get_station(pos["city"])
                    if tz:
                        station_today = now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
                        if pos["target_date"] == station_today:
                            obs_max_c, obs_min_c = fetch_day_extremes(icao, tz, pos["target_date"])
                            is_high = bool(pos["is_high"])
                            obs_val_c = obs_max_c if is_high else obs_min_c
                            if obs_val_c is not None:
                                obs_val_f = round(obs_val_c) * 9.0 / 5.0 + 32.0
                                lb = signals[0]["bucket_low"]
                                ub = signals[0]["bucket_high"]
                                lb_pad = (lb - 0.5) if lb is not None else -1000.0
                                ub_pad = (ub + 0.5) if ub is not None else 1000.0
                                
                                mean = engine_res["ensemble_mean"]
                                std = max(engine_res["ensemble_std"], 0.5)
                                
                                if is_high:
                                    if obs_val_f > ub_pad:
                                        latest_prob = 0.0
                                    elif lb_pad < obs_val_f <= ub_pad:
                                        latest_prob = _norm_cdf(ub_pad, loc=mean, scale=std)
                                        latest_prob = max(0.0, min(1.0, float(latest_prob)))
                                else:
                                    if obs_val_f < lb_pad:
                                        latest_prob = 0.0
                                    elif lb_pad <= obs_val_f <= ub_pad:
                                        latest_prob = 1.0 - _norm_cdf(lb_pad, loc=mean, scale=std)
                                        latest_prob = max(0.0, min(1.0, float(latest_prob)))
                                
                                logging.info(
                                    f"Intraday METAR check for {pos['market_id']} ({pos['city']}): "
                                    f"Observed={obs_val_f:.1f}°F | Forecast Mean={mean:.1f}°F Std={std:.1f}°F | "
                                    f"Updated YES Prob={latest_prob:.4f} (was {fresh_prob:.4f})"
                                )

                if pos["side"] == "YES":
                    current_edge = latest_prob - current_price
                else:
                    current_edge = (1.0 - latest_prob) - current_price

                # Time-adaptive exit floor: raise threshold in the final 4 hours before
                # resolution when volatility spikes and late-market chop can whipsaw exits.
                target_date_str = signals[0]["target_date"]
                adaptive_floor = self._adaptive_exit_floor(target_date_str, now)

                if ENABLE_THESIS_BREAK_EXIT and current_edge < adaptive_floor:
                    # Edge fell below the floor — but WHY? Two opposite causes, only one
                    # worth selling on:
                    #   thesis broken  -> the model's probability for OUR side got worse
                    #                     vs entry (new forecast disagrees), or we're in a
                    #                     real loss. Exit.
                    #   just converged -> price moved TOWARD us (bet winning) while the
                    #                     forecast still supports it. Holding to $1/$0
                    #                     settlement pays far more than scalping now.
                    # DISABLED by default: backtest showed the thesis-break fired on 4
                    # eventual winners (intraday forecast swings) for every 1 real loss cut.
                    thesis_broken = self._thesis_broken(pos, latest_prob, current_price, entry_price)
                    if thesis_broken or not HOLD_WINNERS_TO_RESOLUTION:
                        exit_reason = (
                            f"Edge decayed ({current_edge:.3f} < {adaptive_floor:.3f}"
                            + (" [late-market]" if adaptive_floor > EXIT_EDGE_FLOOR else "")
                            + (" [thesis broken]" if thesis_broken else "") + ")"
                        )
                    else:
                        logging.info(
                            f"HOLD {pos['market_id']} ({pos['side']}): edge {current_edge:.3f} "
                            f"below floor but thesis intact (price converged in our favour) — "
                            f"holding for resolution instead of scalping."
                        )

        if exit_reason:
            self._close_position(pos, pnl_dollars, exit_reason)

    def _thesis_broken(self, pos, latest_prob, current_price, entry_price):
        """Decide whether an edge-decay trigger reflects a genuinely broken thesis
        (sell) rather than the price simply converging in our favour (hold).

        Returns True — exit — when EITHER:
          * the position is in a real loss (current mid below entry), OR
          * the model's probability FOR OUR SIDE has deteriorated by more than
            THESIS_BREAK_PROB_DELTA versus entry (the forecast now disagrees with the bet).

        Returns False when the forecast still supports the bet and we're not losing — the
        edge only shrank because the market moved toward us, so we hold for settlement.

        `latest_prob` is the fresh model P(bucket)=P(YES). Our-side prob is that for YES,
        1-that for NO. The entry P(YES) is read from the trade row; if it's missing we
        can't compare, so we conservatively treat the thesis as broken (exit)."""
        # Real loss? Only a MATERIAL drawdown counts — a 1-2¢ dip below entry is book noise,
        # not a broken thesis, and treating it as one dumped winning NO positions for pennies
        # (NY id10 booked −$1.40 on a market that settled NO=+$1.19; the Guangzhou churn).
        # Uses the same floor as the sustained-loss guard so the two agree on "real loss".
        drawdown = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        if drawdown <= -SUSTAINED_LOSS_MIN_DROP:
            return True

        entry = fetch_query(
            "SELECT model_prob FROM trades WHERE market_id=? AND side=? AND status='OPEN' "
            "ORDER BY id DESC LIMIT 1",
            (pos["market_id"], pos["side"]),
        )
        entry_yes_prob = entry[0]["model_prob"] if entry and entry[0]["model_prob"] is not None else None
        if entry_yes_prob is None:
            return True  # can't compare — fail safe to the old behaviour (exit)

        if pos["side"] == "YES":
            entry_side_prob = entry_yes_prob
            now_side_prob = latest_prob
        else:  # NO bet: our side wins if the bucket is MISSED
            entry_side_prob = 1.0 - entry_yes_prob
            now_side_prob = 1.0 - latest_prob

        # Thesis broken if our side's model probability dropped materially since entry.
        return now_side_prob < entry_side_prob - THESIS_BREAK_PROB_DELTA

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
            shares = round(pos["size_usdc"] / pos["entry_price"], 2)
            fill = self._submit_taker(pos["token_id"], "SELL", shares)   # amount = shares
            if not fill:
                logging.warning(f"Exit SELL did not fill for {pos['market_id']}; leaving open for retry.")
                return
            # Recompute realized PnL from the ACTUAL exit fill price, not the mid estimate.
            exit_price = fill["price"]
            pnl_dollars = (exit_price - pos["entry_price"]) * (pos["size_usdc"] / pos["entry_price"])
            logging.info(
                f"EXIT FILLED {pos['market_id']} ({pos['side']}): {fill['shares']} sh @ ${exit_price:.4f} "
                f"| realized PnL ${pnl_dollars:.2f} | fee_bps={fill['fee_bps']}"
            )

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
