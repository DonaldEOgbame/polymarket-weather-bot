import json
import logging
import math
import threading
from datetime import datetime, timezone
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    MarketOrderArgsV2, OrderArgsV2, OrderType, ApiCreds, BalanceAllowanceParams,
    AssetType,
)
from db import (execute_query, fetch_query, get_open_position, close_position_atomic,
                open_position_atomic, reduce_position_atomic, get_position_by_id,
                update_bankroll, get_current_bankroll, add_notification, current_mode,
                log_position_trail, resolve_arm)
from alerts import send_trade_entry, send_trade_exit, send_model_alert
from scanner import (get_realtime_price, get_realtime_price_status, get_market_resolution,
                     get_gamma_mid_price, get_orderbook_depth_usd, get_orderbook_top_size,
                     get_wallet_token_sizes, get_wallet_sells, estimate_fill,
                     estimate_sale)
from zoneinfo import ZoneInfo
from utils import parse_utc_datetime
from weather import get_signal_engine, get_bucket_probability, _norm_cdf
from metar import get_station, fetch_day_extremes, round_half_away, final_extreme_f
from risk import check_correlation_limits
from config import (
    POLYMARKET_PK, CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
    EXIT_EDGE_FLOOR, CLOB_BASE_URL,
    MIN_MODEL_COUNT, TAKER_FEE_RATE,
    HOLD_WINNERS_TO_RESOLUTION, THESIS_BREAK_PROB_DELTA, SUSTAINED_LOSS_POLLS,
    SUSTAINED_LOSS_MIN_DROP, REENTRY_COOLDOWN_HOURS,
    ENABLE_SUSTAINED_LOSS_GUARD, ENABLE_THESIS_BREAK_EXIT,
    POLYMARKET_FUNDER, POLYMARKET_SIG_TYPE, EXTERNAL_CLOSE_SYNC_MIN_AGE_MIN,
    ONE_TRADE_PER_CITY_DATE, MAX_TRADES_PER_DAY,
    MAX_ENTRY_PRICE, MIN_ENTRY_PRICE, MAX_SUBMIT_DRIFT,
    USE_MARKETABLE_LIMIT, MAX_FILL_SLIPPAGE_ALERT,
    ENABLE_PHYSICS_EXIT_GATE, EXIT_MAX_SLIPPAGE_FRAC,
    ENABLE_POST_DATE_SALVAGE, SALVAGE_MIN_DEPTH_MULTIPLE,
    setting, paper_mode,
)
from intraday import settlement_state, LOCKED_LOSS, UNKNOWN
# MAX_CONCURRENT_POSITIONS / ENABLE_STOP_LOSS / STOP_LOSS_PCT / TAKE_PROFIT_PRICE
# are dashboard-tunable at runtime and read via config.setting() at each
# decision point — a settings change applies to the next entry/exit check
# without a restart, including exits on ALREADY-OPEN positions (intended:
# tightening the stop loss should protect the positions you hold now).


# Guards lazy creation of an Executor's per-position exit-lock table when the
# instance was built without __init__ (see Executor._exit_lock).
_LOCK_TABLE_INIT = threading.Lock()


def get_wallet_collateral(client=None):
    """Real USDC collateral in the wallet right now, or None if unreadable.

    Builds a throwaway client when none is supplied (used by the dashboard's
    new-era endpoint, which runs in the Flask thread and has no Executor).
    update_balance_allowance first: the CLOB's balance view is a CACHE that
    does not track on-chain deposits by itself. Every failure returns None —
    'unknown', never 'zero', because callers book money moves off this number."""
    try:
        if client is None:
            if paper_mode() or not POLYMARKET_PK:
                return None
            kwargs = {"key": POLYMARKET_PK, "chain_id": 137}
            if POLYMARKET_SIG_TYPE:
                kwargs["signature_type"] = POLYMARKET_SIG_TYPE
                kwargs["funder"] = POLYMARKET_FUNDER
            client = ClobClient(CLOB_BASE_URL, **kwargs)
            client.set_api_creds(client.create_or_derive_api_key())
        client.update_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=POLYMARKET_SIG_TYPE))
        raw = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=POLYMARKET_SIG_TYPE))
        return int(raw["balance"]) / 1e6
    except Exception as e:
        logging.warning(f"Wallet collateral read failed: {type(e).__name__}: {e}")
        return None


class PositionObservation:
    """One monitor-cycle snapshot of one open position, and the row it persists.

    Built ONCE per position per cycle, BEFORE the exit check runs. That ordering
    is the whole design: constructing it reads the order book, which populates
    the 30-second price cache, so every get_realtime_price() call inside the exit
    path this cycle returns the identical numbers that get stored. The trail is
    therefore a record of what the bot saw, not a re-derivation of it — a
    re-derivation would drift the moment either side changed.

    Rules are recorded two ways, and the distinction is deliberate:
      * `fired`     — did the condition HOLD, given these prices. Computed here
                      for the cheap rules whether or not the exit path reached
                      them, because "would a 40% stop have fired on this cycle"
                      must be answerable for every cycle, including the ones
                      where an earlier rule exited first or an early return
                      skipped the check.
      * `evaluated` — did the live exit path actually TEST this rule this cycle.
    Reading `fired` as "the bot acted" would be wrong; that is `exit_rule_fired`
    on the parent row.

    stop_loss is recorded TWICE, on mid and on bid. That is the unresolved
    question this table was built for: the three live positions that "slid to
    zero over hours" were observed on bid, and on a book where entry allows up
    to 15% spread, a bid-based drawdown can trip without the market repricing at
    all — the same pathology documented in the SUSTAINED_LOSS_MIN_DROP comment.
    Storing one derived number would decide the question by accident.
    """

    def __init__(self, pos, now, executor=None):
        self.pos = pos
        self.now = now
        self.executor = executor
        self.rules = []
        self.exit_rule_fired = None

        token_id = pos.get("token_id")
        ask, bid, reachable = (0.0, 0.0, False)
        if token_id:
            try:
                ask, bid, reachable = get_realtime_price_status(token_id)
            except Exception as e:
                logging.warning(f"position trail: book read failed for {token_id}: {e}")

        self.best_ask = ask if ask > 0 else None
        self.best_bid = bid if bid > 0 else None
        if ask > 0 and bid > 0:
            self.mid = (ask + bid) / 2.0
            self.price_source = "clob"
        elif ask > 0 or bid > 0:
            self.mid = ask or bid
            self.price_source = "clob_one_sided"
        else:
            self.mid = None
            self.price_source = "unreadable"

        # Same Gamma fallback the exit path uses, recorded as such. A Gamma
        # price is a last-known mark with no book behind it; conflating it with
        # a real quote is how the phantom-$0.999 exits were booked.
        if self.mid is None:
            try:
                gamma = get_gamma_mid_price(pos.get("market_id"), pos.get("side"))
            except Exception:
                gamma = None
            if gamma and gamma > 0:
                self.mid = gamma
                self.price_source = "gamma_fallback"

        self.spread_fraction = None
        if self.best_ask and self.best_bid and self.mid:
            self.spread_fraction = (self.best_ask - self.best_bid) / self.mid

        self.ask_depth_usd, self.bid_depth_usd = (None, None)
        self.ask_top_size, self.bid_top_size = (None, None)
        if token_id and reachable:
            # Both come out of the cache the read above just populated — no
            # second network round trip.
            try:
                self.ask_depth_usd, self.bid_depth_usd = get_orderbook_depth_usd(token_id)
                self.ask_top_size, self.bid_top_size = get_orderbook_top_size(token_id)
            except Exception:
                pass

        self.entry_price = pos.get("entry_price") or 0.0
        self.stake = pos.get("size_usdc") or 0.0
        self.shares = pos.get("shares") or (
            self.stake / self.entry_price if self.entry_price > 0 else 0.0)

        # Denormalised so drawdown is computable from one row, no join. Both are
        # gross of the exit fee: a fee-net figure would bake TAKER_FEE_RATE into
        # stored history and silently rewrite it if that constant is ever re-fit.
        self.pnl_mid, self.pnl_frac_mid = self._pnl(self.mid)
        self.pnl_bid, self.pnl_frac_bid = self._pnl(self.best_bid)

        self.hold_minutes = None
        try:
            entry_time = datetime.fromisoformat(pos["entry_time"])
            self.hold_minutes = (now - entry_time).total_seconds() / 60.0
        except Exception:
            pass

        self.hours_to_resolution = None
        try:
            target_dt = parse_utc_datetime(pos["target_date"] + "T23:59:00+00:00")
            self.hours_to_resolution = (target_dt - now).total_seconds() / 3600.0
        except Exception:
            pass

        self.trade_id = None
        try:
            rows = fetch_query(
                "SELECT id FROM trades WHERE market_id=? AND side=? AND status='OPEN' "
                "AND mode=? ORDER BY id DESC LIMIT 1",
                (pos.get("market_id"), pos.get("side"), current_mode()))
            if rows:
                self.trade_id = rows[0]["id"]
        except Exception:
            pass

    def _pnl(self, price):
        """Unrealised P&L at `price`, in dollars and as a fraction of stake.

        The fraction is pnl/stake, which for a $0-$1 instrument is identically
        (price - entry)/entry — the same number the stop-loss compares against."""
        if price is None or self.entry_price <= 0:
            return None, None
        dollars = (price - self.entry_price) * self.shares
        return dollars, (price - self.entry_price) / self.entry_price

    def record_rule(self, rule, fired, observed=None, threshold=None, basis=None,
                    enabled=True, evaluated=True, detail=None):
        """Note that the live exit path tested `rule` this cycle.

        Upsert, not append: take_profit is tested twice per cycle — once on the
        fast path ahead of the 30-minute hold, once in the main block against
        exit_fill — and two rows for one (rule, basis) would silently double any
        `SELECT ... WHERE rule=?` count. Last write wins because the later test
        is the more complete one; the earlier record survives only when the path
        returned before reaching the second."""
        self.rules = [r for r in self.rules
                      if not (r["rule"] == rule and r["basis"] == basis)]
        self.rules.append({
            "rule": rule, "basis": basis, "enabled": enabled,
            "evaluated": evaluated, "observed": observed, "threshold": threshold,
            "fired": fired, "detail": detail,
        })

    def _fill_unevaluated_rules(self):
        """Compute every rule the exit path did NOT reach this cycle.

        Without this the trail would go quiet in exactly the circumstances that
        matter — the pre-30-minute window, the post-target-date hold, an
        unreadable book — which is the same shape of blindness the table exists
        to end. The cheap rules are recomputed from these prices; thesis_break
        is not, because it needs a fresh ensemble run, so it is recorded as
        unevaluated with the reason rather than guessed at.
        """
        seen = {(r["rule"], r["basis"]) for r in self.rules}

        sl_pct = setting("STOP_LOSS_PCT")
        sl_on = setting("ENABLE_STOP_LOSS")
        for basis, frac in (("mid", self.pnl_frac_mid), ("bid", self.pnl_frac_bid)):
            if ("stop_loss", basis) in seen:
                continue
            self.record_rule(
                "stop_loss", basis=basis, enabled=sl_on, evaluated=False,
                observed=frac, threshold=-sl_pct,
                fired=None if frac is None else frac <= -sl_pct,
                detail="not reached by the exit path this cycle" if frac is not None
                       else "no price on this basis")

        if ("take_profit", "bid") not in seen:
            tp = setting("TAKE_PROFIT_PRICE")
            self.record_rule(
                "take_profit", basis="bid", enabled=True, evaluated=False,
                observed=self.best_bid, threshold=tp,
                fired=None if self.best_bid is None else self.best_bid >= tp,
                detail="not reached by the exit path this cycle")

        if ("sustained_loss", "mid") not in seen:
            streak = 0
            if self.executor is not None:
                key = self.pos.get("id", self.pos.get("market_id"))
                streak = getattr(self.executor, "_loss_streak", {}).get(key, 0)
            self.record_rule(
                "sustained_loss", basis="mid", enabled=ENABLE_SUSTAINED_LOSS_GUARD,
                evaluated=False, observed=streak, threshold=SUSTAINED_LOSS_POLLS,
                fired=None,
                detail=f"streak not advanced this cycle; min_drop={SUSTAINED_LOSS_MIN_DROP}")

        if ("thesis_break", None) not in seen:
            self.record_rule(
                "thesis_break", basis=None, enabled=ENABLE_THESIS_BREAK_EXIT,
                evaluated=False, observed=None, threshold=EXIT_EDGE_FLOOR, fired=None,
                detail="requires a live ensemble re-run; not performed this cycle")

    def persist(self):
        """Write the row. Never raises — logging must not affect trading."""
        try:
            self._fill_unevaluated_rules()
            return log_position_trail({
                "timestamp": self.now.isoformat(),
                "mode": current_mode(),
                "position_id": self.pos.get("id"),
                "trade_id": self.trade_id,
                "market_id": self.pos.get("market_id"),
                "token_id": self.pos.get("token_id"),
                "city": self.pos.get("city"),
                "target_date": self.pos.get("target_date"),
                "side": self.pos.get("side"),
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "mid": self.mid,
                "spread_fraction": self.spread_fraction,
                "bid_top_size": self.bid_top_size,
                "ask_top_size": self.ask_top_size,
                "bid_depth_usd": self.bid_depth_usd,
                "ask_depth_usd": self.ask_depth_usd,
                "price_source": self.price_source,
                "entry_price": self.entry_price,
                "stake_usdc": self.stake,
                "shares": self.shares,
                "unrealized_pnl_mid": self.pnl_mid,
                "unrealized_pnl_frac_mid": self.pnl_frac_mid,
                "unrealized_pnl_bid": self.pnl_bid,
                "unrealized_pnl_frac_bid": self.pnl_frac_bid,
                "hours_to_resolution": self.hours_to_resolution,
                "hold_minutes": self.hold_minutes,
                "exit_fired": 1 if self.exit_rule_fired else 0,
                "exit_rule_fired": self.exit_rule_fired,
            }, self.rules)
        except Exception as e:
            logging.error(f"position trail persist failed: {e}", exc_info=True)
            return None


class _UnloggedObservation:
    """Stand-in for callers of _check_exit_for_position outside the monitor loop
    (tests, ad-hoc scripts). Accepts rule records and drops them: persistence
    belongs to check_exits, which is the only path that observes the book first
    and so the only one whose row would mean anything."""
    exit_rule_fired = None

    def record_rule(self, *args, **kwargs):
        pass


def submit_time_basis(quoted_price, decision_walked, fresh_fill,
                      floor=None, drift=None):
    """The limit-price basis for a live entry, priced off the submit-time book.

    Returns (basis, None) to send, or (None, reason) to stand down this cycle.
    Stand-downs are cheap: the signal re-fires next scan if it still holds, and
    an armed market's arm is untouched (only a BOOKED position consumes it).

    - Unreadable book: don't send. An order priced off nothing is a market
      order with extra steps.
    - Fresh fill below the entry floor: the market slipped back under the
      confirmation bar between decision and submit — the floor gate would
      refuse this exact fill, so the submit path must too.
    - Fresh fill more than MAX_SUBMIT_DRIFT above the decision fill: the edge
      was computed at the decision fill and drift comes straight out of it;
      let the next cycle re-decide at the new price rather than chase.
    """
    floor = MIN_ENTRY_PRICE if floor is None else floor
    drift = MAX_SUBMIT_DRIFT if drift is None else drift
    if fresh_fill is None:
        return None, "book unreadable at submit"
    if fresh_fill < floor:
        return None, (f"submit-time fill {fresh_fill:.3f} is below the "
                      f"{floor:.2f} entry floor")
    if decision_walked is not None and fresh_fill > decision_walked + drift:
        return None, (f"submit-time fill {fresh_fill:.3f} drifted "
                      f"{fresh_fill - decision_walked:.3f} above the decision "
                      f"fill {decision_walked:.3f} (cap {drift:.2f})")
    return max(quoted_price, fresh_fill), None


class Executor:
    def __init__(self):
        self.client = None
        self._ensure_client()
        # Wallet-cash sync: last CLOB balance awaiting a second confirming read.
        self._pending_wallet_bal = None
        self.reconcile_positions()
        # First cash-sync pass at boot — NOT inside reconcile_positions, which
        # early-returns when the book is flat, and a flat book waiting for
        # funding is exactly when this matters. The two-read guard means boot
        # alone never books; the first monitor cycle confirms and books.
        try:
            self.sync_wallet_cash(source="boot")
        except Exception as e:
            logging.error(f"Boot wallet-cash sync failed (non-fatal): {e}", exc_info=True)
        # Tracks consecutive below-entry mid-price polls per position id.
        # Reset on price recovery. Used by the sustained-loss guard in _check_exit_for_position.
        self._loss_streak: dict = {}
        # Per-position exit locks. The dashboard's manual-close button runs on a
        # Flask request thread while the monitor thread may be inside check_exits
        # on the SAME position — two concurrent SELLs for one position means the
        # second is rejected by the CLOB and the DB row is left inconsistent.
        # close_position_atomic guards the DB write, but not the on-chain sell
        # that precedes it, so the lock has to wrap the whole exit.
        self._exit_locks: dict = {}
        self._exit_locks_guard = threading.Lock()

    def _ensure_client(self):
        """Build the CLOB client if live trading needs one and none exists yet.

        Lazy rather than boot-only because PAPER_MODE is now switchable at
        runtime: a process that booted in paper mode has no client, and the
        moment the dashboard flips it live the very next order would have
        nothing to submit through. Called at boot, on every live-mode order
        path, and by the mode switch itself so a failure surfaces THERE — while
        someone is watching — instead of on the first real trade.

        Returns the client, or None in paper mode / when credentials are absent.
        Raises nothing: a failure leaves self.client None and is logged, and the
        live-mode call sites already treat a missing client as "cannot trade".
        """
        if paper_mode():
            return self.client
        if self.client is not None:
            return self.client
        if not POLYMARKET_PK:
            logging.error("Live mode with no POLYMARKET_PK — cannot build a CLOB client.")
            return None
        try:
            # signature_type/funder are REQUIRED for accounts created through the
            # Polymarket website (funds live in a proxy/deposit wallet, not the raw
            # EOA of POLYMARKET_PK) — without them every order is rejected for
            # balance. Type 3 (POLY_1271 deposit wallet) is the current default for
            # website-created accounts and needs the V2 client's order signing.
            client_kwargs = {"key": POLYMARKET_PK, "chain_id": 137}
            if POLYMARKET_SIG_TYPE:
                client_kwargs["signature_type"] = POLYMARKET_SIG_TYPE
                client_kwargs["funder"] = POLYMARKET_FUNDER
            client = ClobClient(CLOB_BASE_URL, **client_kwargs)
            if CLOB_API_KEY and CLOB_SECRET and CLOB_PASS_PHRASE:
                client.set_api_creds(ApiCreds(
                    api_key=CLOB_API_KEY, api_secret=CLOB_SECRET,
                    api_passphrase=CLOB_PASS_PHRASE,
                ))
            else:
                # Derive L2 creds from the private key when none are supplied.
                client.set_api_creds(client.create_or_derive_api_key())
            self.client = client
        except Exception as e:
            logging.error(f"CLOB client init failed: {type(e).__name__}: {e}", exc_info=True)
            return None
        # The CLOB's balance view is a CACHE that does not track on-chain
        # deposits by itself — refresh it now so the first cycle after going
        # live sees the real collateral instead of a stale zero.
        try:
            self.client.update_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=POLYMARKET_SIG_TYPE,
            ))
        except Exception as e:
            logging.warning(f"Balance-cache refresh failed (non-fatal): {e}")
        return self.client

    def _exit_lock(self, pos_id):
        """Get-or-create the exit lock for one position id.

        Tolerates an instance built via __new__ (tests, reconciliation helpers)
        where __init__ never ran and the lock table doesn't exist yet.
        """
        guard = getattr(self, "_exit_locks_guard", None)
        if guard is None:
            # Lazy init under a process-wide lock — two threads racing here would
            # otherwise each install a different guard and defeat the mutual
            # exclusion this whole mechanism exists to provide.
            with _LOCK_TABLE_INIT:
                guard = getattr(self, "_exit_locks_guard", None)
                if guard is None:
                    self._exit_locks = {}
                    guard = self._exit_locks_guard = threading.Lock()
        with guard:
            lock = self._exit_locks.get(pos_id)
            if lock is None:
                lock = threading.Lock()
                self._exit_locks[pos_id] = lock
            return lock

    def reconcile_positions(self):
        positions = fetch_query("SELECT * FROM positions WHERE mode=?", (current_mode(),))
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

        # Catch positions sold manually on Polymarket while the bot was down —
        # the resolution check above can't see those (the market is still live,
        # only the shares are gone).
        try:
            self.sync_external_closes(source="reconcile")
        except Exception as e:
            logging.error(f"Startup external-close sync failed (non-fatal): {e}", exc_info=True)

    def check_resolved_positions(self):
        """Poll Polymarket for resolution status of every open position. Settle any
        that have resolved. Called every monitor cycle so winning trades close at $1.00
        and losers at $0.00 without waiting for edge decay."""
        positions = fetch_query("SELECT * FROM positions WHERE mode=?", (current_mode(),))
        settled_count = 0
        for pos in positions:
            if self._try_settle_position(pos, source="monitor"):
                settled_count += 1
        if settled_count:
            logging.info(f"Resolution check: {settled_count} position(s) settled this cycle")
        return settled_count

    def sync_external_closes(self, source="monitor"):
        """Detect positions closed manually on Polymarket (outside the bot) and
        book them at the price actually received.

        The bot's DB is not the wallet: a manual sale on the website leaves the
        positions row open until resolution, and resolution then credits $1/$0
        instead of the real sale proceeds (first live case: Guangzhou NO sold
        manually at $0.87 — resolution would have booked $1.00, overstating PnL
        by ~$0.45). Each cycle this compares every DB position against the
        wallet's actual token balance and, when shares are missing, corroborates
        against the wallet's SELL fills since entry before booking anything.

        Evidence rules (both required to close — either alone is not enough):
          * balance gone/reduced per the Data-API positions endpoint, AND
          * SELL fills for that token since entry per the trades endpoint.
        A missing balance with NO sell fills is left open loudly: that pattern
        is post-resolution redemption or API indexing lag, and the resolution
        path (_try_settle_position) is the correct closer for it. Any API
        failure means "unknown", never "sold".

        Returns the number of positions closed or reduced."""
        if paper_mode() or not POLYMARKET_FUNDER:
            return 0
        positions = fetch_query("SELECT * FROM positions WHERE mode=?", (current_mode(),))
        if not positions:
            return 0
        wallet = get_wallet_token_sizes(POLYMARKET_FUNDER)
        if wallet is None:
            return 0  # endpoint unreadable — unknown, not "empty"

        synced = 0
        now = datetime.now(timezone.utc)
        for pos in positions:
            try:
                synced += self._sync_one_external_close(pos, wallet, now, source)
            except Exception as e:
                logging.error(
                    f"External-close sync failed for {pos['market_id']} ({pos['side']}): {e}",
                    exc_info=True,
                )
        if synced:
            logging.info(f"External-close sync ({source}): {synced} position(s) reconciled")
        return synced

    def sync_wallet_cash(self, source="monitor"):
        """Detect deposits and withdrawals made on Polymarket OUTSIDE the bot and
        book them in the ledger automatically — no manual command, no restart.

        Why this exists: on 2026-07-28 the user withdrew the entire balance on
        the website and the ledger kept believing $20.91 while the wallet held
        $0.00. The bot is meant to be in sync with Polymarket; cash is the one
        thing external-close sync did not cover. This also makes RE-FUNDING
        automatic: money lands in the wallet → the next cycle books a DEPOSIT →
        the bot starts sizing against the real bankroll.

        Guards, because this books money off an API number:
          * two consecutive cycles must agree (within $0.50) before anything is
            booked — a single flaky read can never move the ledger;
          * a failed read is 'unknown', never 'zero';
          * differences under $1 are ignored (fee dust, rounding);
          * withdrawals are only booked while NO positions are open — right
            after a settlement the wallet can briefly lag the ledger while
            redemption clears, and that must not be booked as a withdrawal.
            Deposits have no such ambiguity and are booked any time.
        """
        if paper_mode() or self._ensure_client() is None:
            return 0
        bal = get_wallet_collateral(self.client)
        if bal is None:
            self._pending_wallet_bal = None   # unknown — start over next cycle
            return 0
        ledger = get_current_bankroll()
        diff = round(bal - ledger, 2)
        if abs(diff) < 1.00:
            self._pending_wallet_bal = None
            return 0

        prev = self._pending_wallet_bal
        if prev is None or abs(prev - bal) > 0.50:
            # First sighting (or the number moved): remember it and wait for a
            # confirming read next cycle before touching the ledger.
            self._pending_wallet_bal = bal
            logging.info(
                f"Wallet/ledger divergence: wallet ${bal:.2f} vs ledger ${ledger:.2f} "
                f"({diff:+.2f}) — awaiting confirming read before booking ({source})")
            return 0
        self._pending_wallet_bal = None

        if diff > 0:
            new_bal = update_bankroll("DEPOSIT", diff)
            logging.info(f"AUTO-SYNC deposit: wallet ${bal:.2f} > ledger ${ledger:.2f} — "
                         f"booked +${diff:.2f}, balance now ${new_bal:.2f}")
            add_notification('deposit',
                             f'Deposit detected on Polymarket: +${diff:.2f}. '
                             f'Available cash now ${new_bal:.2f}.', severity='info')
            return 1

        open_n = fetch_query("SELECT COUNT(*) AS c FROM positions WHERE mode=?", (current_mode(),))[0]["c"]
        if open_n:
            logging.warning(
                f"Wallet ${bal:.2f} below ledger ${ledger:.2f} with {open_n} open "
                f"position(s) — could be redemption lag, NOT booking a withdrawal")
            return 0
        new_bal = update_bankroll("WITHDRAWAL", diff)   # diff is negative
        logging.info(f"AUTO-SYNC withdrawal: wallet ${bal:.2f} < ledger ${ledger:.2f} — "
                     f"booked {diff:.2f}, balance now ${new_bal:.2f}")
        add_notification('withdrawal',
                         f'Withdrawal detected on Polymarket: -${abs(diff):.2f}. '
                         f'Available cash now ${new_bal:.2f}.', severity='warning')
        return 1

    def _sync_one_external_close(self, pos, wallet, now, source):
        """Reconcile one DB position against the wallet. Returns 1 if the
        position was closed/reduced, else 0."""
        entry_dt = parse_utc_datetime(pos["entry_time"])
        age_min = (now - entry_dt).total_seconds() / 60.0
        if age_min < EXTERNAL_CLOSE_SYNC_MIN_AGE_MIN:
            return 0  # Data-API may not have indexed the entry yet

        held = pos["shares"] if pos.get("shares") else (
            pos["size_usdc"] / pos["entry_price"] if pos["entry_price"] > 0 else 0)
        if held <= 0:
            return 0
        onchain = wallet.get(str(pos["token_id"]), 0.0)
        # Dust tolerance: manual sales round to 0.01 sh, leaving crumbs (the
        # live Guangzhou sale left 0.0098 of 3.3898 sh). Within tolerance of
        # the full size = still held, nothing to do.
        dust = max(held * 0.01, 0.05)
        if onchain >= held - dust:
            return 0

        sells = get_wallet_sells(
            POLYMARKET_FUNDER, pos["market_id"], pos["token_id"], entry_dt.timestamp())
        if sells is None:
            return 0  # trades endpoint unreadable — retry next cycle
        sold = sum(s for _, s in sells)
        if sold <= 0:
            logging.warning(
                f"External-close sync: {pos['market_id']} ({pos['side']}) wallet holds "
                f"{onchain:.4f} of {held:.4f} sh but NO sell fills found since entry — "
                f"leaving open (likely post-resolution redemption or Data-API lag; "
                f"resolution settlement will close it)."
            )
            return 0

        proceeds = sum(p * s for p, s in sells)
        vwap = proceeds / sold
        # Same taker-fee model as the bot's own live exits; the Data-API price
        # is the raw fill price, fees are charged on top.
        fees = sum(TAKER_FEE_RATE * p * (1.0 - p) * s for p, s in sells)

        if onchain <= dust:
            # Fully sold (modulo dust). PnL = what the sale actually returned
            # minus what the position cost.
            pnl = (proceeds - fees) - pos["size_usdc"]
            self._close_position(
                pos, pnl_dollars=pnl,
                exit_reason=f"EXTERNAL_CLOSE ({sold:.2f} sh @ ${vwap:.3f} manual sale)",
            )
            return 1

        # Partial manual sale: shrink the position by what actually left the
        # wallet, book the realized chunk, keep the rest under management.
        sold_eff = min(sold, held - onchain)
        frac = sold_eff / sold
        part_proceeds = (proceeds - fees) * frac
        cost_freed = sold_eff * pos["entry_price"]
        reduced = reduce_position_atomic(
            pos_id=pos["id"], market_id=pos["market_id"], side=pos["side"],
            sold_shares=sold_eff, entry_cost_freed=cost_freed,
            proceeds=part_proceeds, pnl_delta=part_proceeds - cost_freed,
        )
        if reduced:
            logging.warning(
                f"External PARTIAL close: {pos['market_id']} ({pos['side']}) — "
                f"{sold_eff:.2f} of {held:.2f} sh sold manually @ ~${vwap:.3f}; "
                f"position reduced to {onchain:.2f} sh."
            )
        return 1 if reduced else 0

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
            # Dedup: settle_closed_trade may have already written a METAR-based
            # resolution row for this (market_id, side) before Polymarket reported
            # the market resolved. Without this guard the two writers produce a
            # duplicate row that inflates the Brier/win-rate denominators.
            already = fetch_query(
                "SELECT 1 FROM resolutions WHERE market_id=? AND side=? LIMIT 1",
                (pos["market_id"], pos["side"]),
            )
            if already:
                return
            trade = fetch_query(
                "SELECT model_prob FROM trades WHERE market_id=? AND side=? AND status='OPEN' "
                "AND mode=? "
                "ORDER BY id DESC LIMIT 1",
                (pos["market_id"], pos["side"], current_mode())
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

    def settle_closed_trade(self, trade):
        """Write a resolutions row for a trade whose position was ALREADY closed
        early (e.g. take-profit / stop) and therefore never went through
        _try_settle_position. Uses the METAR-resolved daily extreme — the same
        ruler Polymarket settles on — to determine the true win/loss, so an early
        exit no longer hides the real outcome (this is why all 18 take-profit
        trades had no resolution row and the Brier stats were blind to them).

        Idempotent: skips if a resolution row already exists for this trade's
        (market_id, side). Returns True if a row was written."""
        market_id, side = trade["market_id"], trade["side"]
        city, target_date = trade.get("city"), trade.get("target_date")
        is_high = bool(trade["is_high"]) if trade.get("is_high") is not None else True
        try:
            existing = fetch_query(
                "SELECT 1 FROM resolutions WHERE market_id=? AND side=? LIMIT 1",
                (market_id, side),
            )
            if existing:
                return False
            actual_f = final_extreme_f(city, target_date, is_high)
            if actual_f is None:
                return False  # local day not over / METAR not published — retry next cycle

            m = fetch_query(
                "SELECT bucket_low, bucket_high FROM markets WHERE market_id=?",
                (market_id,),
            )
            if not m:
                return False
            lb, ub = m[0]["bucket_low"], m[0]["bucket_high"]
            lo = (lb - 0.5) if lb is not None else -1e9
            hi = (ub + 0.5) if ub is not None else 1e9
            landed_in_bucket = lo <= actual_f <= hi
            outcome = "YES" if landed_in_bucket else "NO"
            won = (outcome == side)

            model_prob_entry = trade.get("model_prob")
            if model_prob_entry is None:
                prob_for_side, brier = None, None
            else:
                prob_for_side = model_prob_entry if side == "YES" else (1.0 - model_prob_entry)
                brier = (float(won) - prob_for_side) ** 2

            # True settled PnL from the entry price and actual outcome — NOT the
            # early-exit pnl already on the trade row (that measures the scalp,
            # this measures the bet). Recorded separately in resolutions.
            shares = trade["size_usdc"] / trade["fill_price"] if trade["fill_price"] else 0
            settled_pnl = (shares - trade["size_usdc"]) if won else -trade["size_usdc"]

            execute_query(
                "INSERT INTO resolutions (market_id, resolved_at, outcome, actual_value, "
                "model_prob_at_entry, pnl, side, won, brier, city, target_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, datetime.now(timezone.utc).isoformat(), outcome, actual_f,
                 model_prob_entry, settled_pnl, side, 1 if won else 0, brier, city, target_date),
            )
            logging.info(
                f"Closed-trade settlement: {market_id} ({side}) actual={actual_f:.1f}°F "
                f"outcome={outcome} won={won} settled_pnl=${settled_pnl:+.2f}"
                + (f" brier={brier:.4f}" if brier is not None else "")
            )
            return True
        except Exception as e:
            logging.error(f"settle_closed_trade failed for {market_id}: {e}", exc_info=True)
            return False

    def settle_unscored_trades(self, limit=200):
        """Backfill resolutions for any closed trade that has no resolution row,
        independent of trades.resolution_logged.

        check_resolutions() cannot do this on its own because resolution_logged
        is a single flag serving two jobs — "model accuracy recorded" and "done
        with this trade". It gets set as soon as the accuracy write succeeds (or
        when raw_models/coords are missing), and from then on the trade is never
        revisited. Anything that made settle_closed_trade return False on that
        one pass is therefore permanent: a missing markets row, or simply having
        closed before settle_closed_trade existed. That is why 16 of 32
        take-profit trades in the 2026-07-31 export carry no resolution row at
        all — a bookkeeping gap, not a trading error.

        This roughly doubles the calibration sample without placing a new bet,
        which matters when every constant in config.py is fitted on 27 trades.
        Idempotent (settle_closed_trade no-ops when a row exists) and safe to run
        on every cycle. Returns the number of rows written."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            rows = fetch_query(
                "SELECT t.id, t.market_id, t.side, t.is_high, t.city, t.target_date, "
                "       t.model_prob, t.size_usdc, t.fill_price "
                "FROM trades t "
                "LEFT JOIN resolutions r "
                "       ON r.market_id = t.market_id AND r.side = t.side "
                "WHERE r.market_id IS NULL "
                "  AND t.target_date IS NOT NULL AND t.target_date <= ? "
                "  AND t.status != 'open' "
                "ORDER BY t.target_date DESC LIMIT ?",
                (today, limit),
            )
        except Exception as e:
            logging.error(f"settle_unscored_trades query failed: {e}", exc_info=True)
            return 0

        written = 0
        for t in rows:
            try:
                if self.settle_closed_trade(dict(t)):
                    written += 1
            except Exception as e:
                logging.error(f"settle_unscored_trades: {t['market_id']}: {e}")
        if written:
            logging.info(
                f"Backfilled {written} settlement row(s) for previously unscored "
                f"closed trades ({len(rows)} candidates)"
            )
        return written

    def get_open_positions_count(self):
        res = fetch_query("SELECT COUNT(*) as count FROM positions WHERE mode=?", (current_mode(),))
        return res[0]["count"] if res else 0

    @staticmethod
    def _fill_fee_rate(fill):
        """Per-leg taker fee rate for a fill. get_fee_rate_bps reports the
        market's ROUND-TRIP bps (1000 on live weather markets) but the exchange
        charges HALF that per leg — verified against the wallet's actual USDC
        deltas across all 16 live fills 2026-07-18..24: every leg was charged
        exactly 0.05*p*(1-p)*shares, never 0.10. Using the bps raw doubled every
        booked fee and drifted the ledger $0.27 below the real wallet in 6 days
        (same bug the one-off FEE_RECONCILE hand-corrected on the first Wuhan
        entry — that fixed the ledger once but left the code doubling)."""
        if fill.get("fee_bps"):
            return fill["fee_bps"] / 10000.0 / 2.0
        return TAKER_FEE_RATE

    def _read_fill(self, resp, order_id, fallback_price):
        """Determine the ACTUAL matched size (shares) and average fill price for a
        just-submitted order. The POST /order response schema is not contractually
        stable, so the order record (get_order) is treated as the source of truth.
        Returns (filled_shares, avg_price). filled_shares == 0 means nothing filled.

        NOTE: verify these field names against the raw response logged below on the
        FIRST real fill — adjust if Polymarket's schema differs in this client version.
        """
        import time as _time
        shares, price = 0.0, None
        if not order_id:
            return shares, price
        # The order record can lag matching by a moment; retry briefly before
        # concluding "nothing filled" — a premature zero here books a real fill
        # as nothing and leaves an untracked live position.
        for attempt in range(3):
            try:
                o = self.client.get_order(order_id)
                if isinstance(o, dict):
                    sm = float(o.get("size_matched") or 0)
                    if sm > 0:
                        shares = sm
                        # Prefer the volume-weighted price of the order's actual
                        # trades: o["price"] is the marketable LIMIT price (the
                        # worst-case book walk), not what actually filled.
                        # NOTE (verified live 2026-07-18): associate_trades is a
                        # list of trade-ID STRINGS in the current API, so this
                        # branch only fires if the schema ever returns objects;
                        # the isinstance guard keeps it from crashing on IDs.
                        trades = [t for t in (o.get("associate_trades") or o.get("trades") or [])
                                  if isinstance(t, dict)]
                        try:
                            tot_sz = sum(float(t.get("size") or 0) for t in trades)
                            tot_val = sum(float(t.get("size") or 0) * float(t.get("price") or 0)
                                          for t in trades)
                            if tot_sz > 0 and tot_val > 0:
                                price = tot_val / tot_sz
                        except Exception:
                            pass
                        if not price:
                            price = float(o.get("price") or 0) or fallback_price
                        break
            except Exception as e:
                logging.error(f"get_order({order_id}) failed during fill confirmation: {e}")
            if attempt < 2:
                _time.sleep(1.0)
        return shares, price

    def _verify_fill(self, opp, signal_data, quoted, limit_sent, filled_price,
                     shares, size):
        """Check what was actually paid against what the decision assumed.

        The Austin fill sat in the ledger looking ordinary: the trade row records
        fill_price and nothing else, so a 34-cent slippage and a 0-cent slippage
        are indistinguishable after the fact without re-deriving the quote from
        the replay log. Every other trade in the book filled AT the quote, so
        this is near-silent in normal operation and loud exactly once.

        Recomputes the edge at the price actually paid. Post-fill edge is the
        only number that describes the bet that now exists — a +0.128 edge on a
        0.64 quote is a -0.117 edge at a 0.9818 fill, and the position will still
        most likely win, which is precisely why nothing else would catch it."""
        slippage = filled_price - quoted
        prob = signal_data.get("model_prob")
        # NO side pays `filled_price` to receive 1.00 when the bucket misses.
        fair = (1.0 - prob) if signal_data.get("side") == "NO" else prob
        post_fill_edge = (fair - filled_price) if fair is not None else None
        depth = signal_data.get("usable_depth_usd")
        pct_of_depth = (100.0 * size / depth) if depth else None

        logging.info(
            f"FILL_AUDIT {opp.market_id} {signal_data.get('side')} | "
            f"quoted={quoted:.4f} limit_sent={limit_sent} filled={filled_price:.4f} "
            f"slippage={slippage:+.4f} | shares={shares} size=${size:.2f} | "
            f"depth_at_decision="
            f"{('$%.2f' % depth) if depth else 'unknown'} "
            f"size_pct_of_depth={('%.1f%%' % pct_of_depth) if pct_of_depth else 'n/a'} | "
            f"edge_at_decision={signal_data.get('edge'):+.4f} "
            f"edge_at_fill={('%+.4f' % post_fill_edge) if post_fill_edge is not None else 'n/a'}"
        )

        if filled_price > MAX_ENTRY_PRICE + 1e-9:
            # Should be structurally impossible now. If it fires, some path is
            # still sending an order the cap does not constrain.
            logging.error(
                f"CAP BREACH {opp.market_id}: filled at {filled_price:.4f}, above "
                f"MAX_ENTRY_PRICE {MAX_ENTRY_PRICE:.2f}. Another code path is "
                f"bypassing the entry cap — investigate before trading further.")
            add_notification(
                "execution",
                f"Fill at {filled_price:.4f} breached the {MAX_ENTRY_PRICE:.2f} "
                f"entry cap on {opp.city} — a path is bypassing the limit price.",
                "error")

        if post_fill_edge is not None and post_fill_edge < 0:
            logging.error(
                f"NEGATIVE EDGE AT FILL {opp.market_id} ({opp.city}): paid "
                f"{filled_price:.4f} for a token worth {fair:.4f}. The decision "
                f"claimed {signal_data.get('edge'):+.4f}; execution made it "
                f"{post_fill_edge:+.4f}. This position is expected to LOSE "
                f"${abs(post_fill_edge) * shares:.2f} even though it will "
                f"probably still resolve in our favour.")
            add_notification(
                "execution",
                f"{opp.city}: filled at {filled_price:.4f} vs fair {fair:.4f} — "
                f"negative edge at fill ({post_fill_edge:+.4f}). Likely to win, "
                f"still a losing bet.",
                "error")

        if abs(slippage) > MAX_FILL_SLIPPAGE_ALERT:
            add_notification(
                "execution",
                f"{opp.city}: filled {slippage:+.4f} from the {quoted:.4f} quote "
                f"(limit {limit_sent}), ${size:.2f} into "
                f"{('$%.2f' % depth) if depth else 'unknown'} of usable depth.",
                "warning")

    def _submit_marketable_limit(self, token_id, side, amount, limit_price,
                                 fallback_price=None):
        """Cross the book with a LIMIT order that cannot fill above `limit_price`.

        A market order on a $0-$1 instrument has no floor on execution quality:
        it walks until the size is filled at whatever the book charges. On
        2026-08-06 that turned a $6 order against $26.49 of ask depth into a
        0.9818 fill on a 0.64 quote — 34 cents of slippage on a 12.8-cent edge.

        FAK (fill-and-kill) so the unfillable remainder is cancelled rather than
        resting as a phantom open order. A PARTIAL fill is the desired outcome
        when the book is thinner than expected: $3.50 filled at an acceptable
        price beats $6.00 filled at any price, and the shortfall is logged.

        `amount` is USDC for BUY. The CLOB wants (price, size-in-shares) for a
        limit order, so the size is derived at the limit price — that is the
        WORST price it can pay, so it can never overspend the intended stake."""
        if self._ensure_client() is None:
            logging.error(
                f"No CLOB client — refusing to {side} {token_id}. "
                f"Live mode needs POLYMARKET_PK and working CLOB credentials.")
            return None
        try:
            fee_bps = self.client.get_fee_rate_bps(token_id)
        except Exception:
            fee_bps = None

        limit_price = round(float(limit_price), 4)
        shares = round(amount / limit_price, 2) if side == "BUY" else round(amount, 2)
        if shares <= 0:
            logging.warning(f"{side} size rounds to zero at limit {limit_price}; not sending")
            return None
        try:
            if side == "BUY":
                # A price-CAPPED market order, not a (price, size) limit order.
                # The CLOB validates a buy's maker amount (USDC) to exact cents,
                # and price*shares from a rounded share count almost never is:
                # 15.38 sh x 0.65 = $9.9970 -> HTTP 400 "invalid amounts".
                # Every marketable-limit entry from Phase 0.3 (2026-08-06) to
                # 2026-08-10 was rejected this way — Wellington's 3 cycles and
                # Dallas's 9 included. MarketOrderArgsV2.price is the worst
                # acceptable fill (create_market_order uses it verbatim), so
                # this keeps the binding cap while sending amount as exact
                # cents, which always validates.
                signed = self.client.create_market_order(
                    MarketOrderArgsV2(token_id=token_id, amount=round(amount, 2),
                                      side=side, price=limit_price,
                                      order_type=OrderType.FAK)
                )
            else:
                signed = self.client.create_order(
                    OrderArgsV2(token_id=token_id, price=limit_price, size=shares,
                                side=side)
                )
            resp = self.client.post_order(signed, OrderType.FAK)
        except Exception as e:
            logging.error(
                f"Limit order failed ({side} size={shares} @ {limit_price} "
                f"tok={token_id}): {e}")
            return None
        logging.info(f"RAW order response [{side} {token_id} @ limit {limit_price}]: {resp}")

        order_id = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
        filled, avg = 0.0, None
        if isinstance(resp, dict) and resp.get("status") == "matched":
            try:
                mk = float(resp.get("makingAmount") or 0)
                tk = float(resp.get("takingAmount") or 0)
                if mk > 0 and tk > 0:
                    filled, avg = (tk, mk / tk) if side == "BUY" else (mk, tk / mk)
            except (TypeError, ValueError):
                pass
        if filled <= 0:
            filled, avg = self._read_fill(resp, order_id, fallback_price or limit_price)
        if filled <= 0:
            logging.info(
                f"{side} limit order at {limit_price} did not fill — the book had "
                f"nothing at or better than the cap. Nothing booked. resp={resp}")
            return None
        if not avg:
            # A real fill whose price cannot be read must never be discarded, but
            # it also must not be booked below what was actually paid. The limit
            # is the worst possible price, so it is the safe assumption.
            logging.critical(
                f"{side} order {order_id} matched {filled} shares but no price "
                f"could be read — booking at the limit {limit_price}. RECONCILE "
                f"MANUALLY. resp={resp}")
            avg = limit_price
        return {"shares": filled, "price": avg, "fee_bps": fee_bps,
                "limit_price": limit_price, "requested_usd": amount}

    def _submit_taker(self, token_id, side, amount, fallback_price=None):
        """Place a Fill-And-Kill MARKET order (taker). For BUY, `amount` is USDC to
        spend (Polymarket market-order min $1); for SELL, `amount` is shares. The
        client walks the book to price it, so it either takes immediately or is
        killed — never rests as a phantom open order. Returns {shares, price,
        fee_bps} on a real fill, or None if nothing filled. Live mode only."""
        # A process that booted in paper mode has no client until the dashboard
        # switches it live; build it here rather than raising AttributeError on
        # the first real order.
        if self._ensure_client() is None:
            logging.error(
                f"No CLOB client — refusing to {side} {token_id}. "
                f"Live mode needs POLYMARKET_PK and working CLOB credentials.")
            return None
        try:
            fee_bps = self.client.get_fee_rate_bps(token_id)
        except Exception:
            fee_bps = None
        try:
            signed = self.client.create_market_order(
                MarketOrderArgsV2(token_id=token_id, amount=amount, side=side,
                                  order_type=OrderType.FAK)
            )
            resp = self.client.post_order(signed, OrderType.FAK)
        except Exception as e:
            logging.error(f"Market order failed ({side} amount={amount} tok={token_id}): {e}")
            return None
        # Log the raw response verbatim — this is how we confirm the schema on the first real fill.
        logging.info(f"RAW order response [{side} {token_id}]: {resp}")
        order_id = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
        # Primary fill source: the POST response's matched amounts. Verified on the
        # 2026-07-18 live $1 round-trip — makingAmount/takingAmount are the exact
        # matched totals (BUY: making=USDC, taking=shares; SELL: reversed), so their
        # ratio IS the volume-weighted fill price, with no order-record lag.
        filled, avg = 0.0, None
        if isinstance(resp, dict) and resp.get("status") == "matched":
            try:
                mk = float(resp.get("makingAmount") or 0)
                tk = float(resp.get("takingAmount") or 0)
                if mk > 0 and tk > 0:
                    filled, avg = (tk, mk / tk) if side == "BUY" else (mk, tk / mk)
            except (TypeError, ValueError):
                pass
        if filled <= 0:
            filled, avg = self._read_fill(resp, order_id, fallback_price)
        if filled > 0 and not avg:
            # A REAL fill with an unreadable price must never be discarded — that
            # books "nothing filled" while USDC/shares actually moved, leaving an
            # untracked live position that the next scan doubles into. Book it at
            # the caller's target price and flag loudly for manual reconciliation.
            logging.critical(
                f"{side} order {order_id} matched {filled} shares but no price could be read "
                f"and no fallback was provided — booking at 0.5 placeholder. RECONCILE MANUALLY. "
                f"resp={resp}"
            )
            avg = 0.5
        if filled <= 0:
            logging.warning(f"{side} market order did not fill; booking nothing. resp={resp}")
            return None
        return {"shares": filled, "price": avg, "fee_bps": fee_bps}

    def execute_trade(self, signal_data):
        opp = signal_data["opp"]

        if getattr(self, "_entry_recording_broken", False):
            logging.warning(
                f"Entries halted (a fill went unrecorded earlier this process) — "
                f"skipping {opp.city} {opp.date}. Restart after adopting the "
                f"orphan position to re-arm.")
            return

        if get_open_position(opp.market_id):
            logging.info(f"Already holding position in {opp.market_id} — skipping")
            return

        # Daily entry cap (owner decision 2026-08-12): at most MAX_TRADES_PER_DAY
        # NEW entries per UTC day, bounding worst-case daily exposure under the
        # take-everything rule set. Counted here, at the single serialized entry
        # point, so a burst of qualifying markets in one scan cannot overshoot.
        if MAX_TRADES_PER_DAY > 0:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = fetch_query(
                "SELECT COUNT(*) AS c FROM trades WHERE mode=? AND entry_time >= ?",
                (current_mode(), today),
            )
            n_today = rows[0]["c"] if rows else 0
            if n_today >= MAX_TRADES_PER_DAY:
                logging.info(
                    f"Daily trade cap reached ({n_today}/{MAX_TRADES_PER_DAY} "
                    f"entries today) — skipping {opp.city} {opp.date}")
                return

        # One trade per city per target day. Sibling buckets on the same city/date all
        # settle on the SAME realized temperature, so a second entry there is stacked
        # exposure to one weather outcome, not a new bet (two HK entries 07-26, two
        # Shenzhen 07-27, two Sao Paulo 07-29 all did this live). Any prior trade for
        # the pair blocks — open or closed — so a stop-out can't be re-entered through
        # a different bucket of the same event either.
        if ONE_TRADE_PER_CITY_DATE and opp.city and opp.date:
            prior = fetch_query(
                "SELECT id FROM trades WHERE city=? AND target_date=? AND mode=? LIMIT 1",
                (opp.city, opp.date, current_mode()),
            )
            if prior:
                logging.info(
                    f"City/date already traded ({opp.city} {opp.date}, trade "
                    f"{prior[0]['id']}) — one trade per city per day, skipping"
                )
                return

        # Re-entry cooldown: don't re-open a market we recently EXITED. Without this the
        # bot churns — a position force-closed on noise gets re-bought on the next scan,
        # paying spread+fee each round-trip (the Guangzhou market took 9 entries this way).
        if REENTRY_COOLDOWN_HOURS > 0:
            last = fetch_query(
                "SELECT exit_time FROM trades WHERE market_id=? AND exit_time IS NOT NULL "
                "AND mode=? ORDER BY id DESC LIMIT 1",
                (opp.market_id, current_mode()),
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

        max_concurrent = setting("MAX_CONCURRENT_POSITIONS")
        if self.get_open_positions_count() >= max_concurrent:
            logging.info(f"Max {max_concurrent} concurrent positions reached, skipping entry.")
            return

        # Correlated-exposure caps. Checked LAST of the portfolio gates, so a
        # trade refused here has already passed everything cheaper and the log
        # line is unambiguously about correlation. See risk.py: the count-based
        # limits above cannot see that Dallas and Austin on one target date are
        # one bet on one ridge.
        direction = signal_data.get("risk_direction")
        allowed, why, detail = check_correlation_limits(
            fetch_query("SELECT city, target_date, size_usdc, risk_direction "
                        "FROM positions WHERE mode=?", (current_mode(),)),
            opp.city, opp.date, signal_data["size_usdc"], direction)
        if not allowed:
            logging.info(
                f"CORRELATION_BLOCK | {opp.city} {opp.date} [{direction}] | {why} | "
                f"group={detail['group']} "
                f"group_exposure=${detail['group_exposure']:.2f}/${detail['group_cap']:.2f} "
                f"direction_exposure=${detail['direction_exposure']:.2f}/"
                f"${detail['direction_cap']:.2f}"
            )
            add_notification("correlation", why, "info")
            return
        if detail["positions_with_unknown_direction"]:
            # Excluded positions weaken the direction cap silently otherwise.
            logging.warning(
                f"CORRELATION | {detail['positions_with_unknown_direction']} open "
                f"position(s) on {opp.date} have no risk_direction and are not "
                f"counted toward the ${detail['direction_cap']:.2f} same-direction cap"
            )

        # Alert if model count was low for this signal (degraded confidence)
        model_count = signal_data.get("model_count", MIN_MODEL_COUNT)
        if model_count < MIN_MODEL_COUNT:
            send_model_alert(opp.city, model_count, MIN_MODEL_COUNT)

        side = signal_data["side"]
        size = signal_data["size_usdc"]
        # Paper assumes a fill at the limit computed below (walked book + 1¢).
        # Live crosses the real ask and records whatever ACTUALLY fills
        # (price + size), so the ledger and the measured cost reflect real
        # execution, not an assumption.
        quoted_price = signal_data["price"]
        # The limit the order is actually sent with, from the WALKED ask VWAP
        # the gates approved, not the mid. The quote is the book mid, and on a
        # wide-spread book mid+1c sits below the best ask, so the FAK kills
        # against the exact book the gates just accepted — Wellington
        # 2026-08-08 qualified on three consecutive cycles (0.66-0.695, armed
        # waiver) and filled nothing while NO ran to 0.87. max(quote, walked)
        # guards the other staleness direction, +1c is the drift allowance
        # between scan and submit, and MAX_ENTRY_PRICE is policy and must win.
        # Before 2026-08-06 this line capped at 0.99 — the DISABLED sentinel —
        # so the configured 0.80 cap could not constrain what was paid, and a
        # market order filled at 0.9818.
        walked = signal_data.get("walked_vwap")
        if not paper_mode():
            # The decision's walked VWAP came from the book cache warmed at
            # scan START — minutes old by the time this line runs, and maker
            # bots re-quote in seconds. Dallas 2026-08-10 03:49-05:15 UTC:
            # nine BUY_NO decisions priced off a stale 0.65 walk, and (per the
            # exchange's FAK semantics) nine "no orders found to match"
            # rejections as the sub-limit ask kept vanishing before submit.
            # Walk the book AS IT IS NOW; the floor and drift checks below
            # keep a moved market honest instead of chased.
            fresh = estimate_fill(signal_data["token_id"], size, MAX_ENTRY_PRICE,
                                  force=True)
            fresh_fill = fresh.get("vwap") if fresh else None
            basis, skip = submit_time_basis(quoted_price, walked, fresh_fill)
            if basis is None:
                logging.info(f"SUBMIT_REPRICE | {opp.city} {opp.date} | {skip} "
                             f"— not sending this cycle (any arm stays alive)")
                return
            limit_basis = basis
        else:
            limit_basis = max(quoted_price, walked) if walked is not None else quoted_price
        # Ceil to the tick, not round(): a VWAP basis lands between ticks and
        # banker's rounding (0.705 -> 0.70) hands the allowance straight back.
        # A limit is a cap, so rounding it UP never overpays the book.
        price = min(math.ceil(round((limit_basis + 0.01) * 100, 6)) / 100,
                    MAX_ENTRY_PRICE)
        shares = round(size / price, 2)
        entry_fee = 0.0  # paper mode: fee is modeled inside transaction_cost, not the ledger

        if not paper_mode():
            # THE WALLET IS THE LEDGER OF LAST RESORT (Moscow 2026-08-13): when
            # /data filled overnight, four consecutive scans each bought this
            # market — the CLOB fill succeeded, the DB record failed, and the
            # next scan saw "no open position" and bought again. The DB cannot
            # protect against its own write failures, so before ANY live buy,
            # ask the chain: if the wallet already holds this token, a previous
            # record was lost — adopt manually, never re-buy. Fails OPEN on an
            # unreadable Data-API (its flakes are common; the loss-of-record
            # event is rare) but BLOCKS on positive holdings.
            try:
                from scanner import get_wallet_token_sizes
                held = (get_wallet_token_sizes(POLYMARKET_FUNDER) or {}).get(
                    str(signal_data["token_id"]), 0.0) if POLYMARKET_FUNDER else 0.0
            except Exception as e:
                logging.error(f"wallet-holdings precheck failed for {opp.market_id}: {e}")
                held = 0.0
            if held >= 1.0:
                msg = (f"{opp.city} {opp.date}: wallet already holds {held:.2f} of this "
                       f"token but no position is recorded — a previous entry's DB "
                       f"record was lost. NOT re-buying; adopt the position manually.")
                logging.critical(f"UNRECORDED_POSITION | {msg}")
                try:
                    add_notification("execution", "error", msg)
                except Exception:
                    pass
                return

            logging.info(
                f"Executing LIVE trade: BUY ${size:.2f} of {opp.market_id} {side} "
                f"(quote=${quoted_price:.4f}, limit=${price:.4f}, "
                f"edge={signal_data['edge']:.3f})"
            )
            if USE_MARKETABLE_LIMIT:
                fill = self._submit_marketable_limit(
                    signal_data["token_id"], "BUY", size, limit_price=price,
                    fallback_price=price)
            else:
                fill = self._submit_taker(signal_data["token_id"], "BUY", size,
                                          fallback_price=price)  # amount = USDC
            if not fill:
                return  # nothing filled → no phantom position
            price = round(fill["price"], 4)
            shares = fill["shares"]
            size = round(shares * price, 2)                  # actual USDC deployed
            entry_fee = self._fill_fee_rate(fill) * price * (1.0 - price) * shares
            self._verify_fill(opp, signal_data, quoted_price,
                              fill.get("limit_price"), price, shares, size)
        else:
            logging.info(
                f"Executing PAPER trade: BUY {shares} shares of {opp.market_id} {side} @ ${price:.3f} "
                f"(size=${size:.2f}, edge={signal_data['edge']:.3f}, prob={signal_data['model_prob']:.3f})"
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            open_position_atomic(
                market_id=opp.market_id, token_id=signal_data["token_id"], side=side,
                price=price, size=size, now_iso=now_iso, question=opp.question,
                is_high=opp.is_high, city=opp.city, target_date=opp.date,
                model_prob=signal_data["model_prob"], edge=signal_data["edge"],
                shares=shares, entry_fee=entry_fee, risk_direction=direction,
            )
        except Exception as e:
            # A live fill exists that the ledger could not record (this is how
            # the disk-full night produced 4x Moscow). Entering again would
            # compound it, and there is no safe automatic recovery while writes
            # are failing — halt NEW entries for this process lifetime and
            # scream. Exits/monitoring continue; a restart (which implies a
            # human looked) re-arms entries.
            self._entry_recording_broken = True
            logging.critical(
                f"ENTRY RECORDING FAILED after a live fill | {opp.city} {opp.date} "
                f"| ${size:.2f} @ {price:.4f} filled but not recorded: {e} — "
                f"halting all new entries until restart; adopt the position manually.")
            try:
                add_notification(
                    "execution", "error",
                    f"{opp.city} {opp.date}: fill ${size:.2f} @ {price:.4f} NOT "
                    f"RECORDED ({e}). New entries halted until restart.")
            except Exception:
                pass
            return
        send_trade_entry(opp.question, price, signal_data["model_prob"], signal_data["edge"], size)
        # Consume any armed re-entry waiver AFTER the position books, not at
        # signal time: a FAK that fills nothing must leave the arm alive so the
        # waiver still applies on the next cycle. Best-effort — the position is
        # already open, so an arm-store error must not unwind or alarm the trade.
        try:
            resolve_arm(opp.market_id, "entered", f"position opened @ {price:.3f}")
        except Exception as e:
            logging.error(f"failed to consume arm for {opp.market_id}: {e}")

    def get_live_prices(self):
        """Return {market_id: current_mid_price} for all open positions."""
        positions = fetch_query("SELECT market_id, token_id FROM positions WHERE mode=?", (current_mode(),))
        prices = {}
        for p in positions:
            ask, bid = get_realtime_price(p["token_id"])
            if ask > 0 and bid > 0:
                prices[p["market_id"]] = (ask + bid) / 2.0
            elif ask > 0 or bid > 0:
                prices[p["market_id"]] = ask or bid
        return prices

    def check_exits(self):
        positions = fetch_query("SELECT * FROM positions WHERE mode=?", (current_mode(),))
        for pos in positions:
            # Skip rather than block: if the dashboard is mid manual-close on this
            # position, the monitor has nothing useful to add and shouldn't stall
            # the whole cycle waiting on a CLOB round-trip.
            lock = self._exit_lock(pos["id"])
            if not lock.acquire(blocking=False):
                logging.info(f"Position {pos['id']} is being closed elsewhere — skipping exit check this cycle")
                continue
            try:
                if not get_position_by_id(pos["id"]):
                    continue  # closed while we waited for the lock
                # Observe BEFORE deciding, persist in `finally`. Both halves are
                # load-bearing: building the observation first warms the price
                # cache so the exit path below sees the same book this row
                # records, and the finally means a cycle that exits, throws, or
                # returns early still leaves a row. A trail with holes at the
                # interesting moments is the failure this replaces.
                obs = PositionObservation(pos, datetime.now(timezone.utc), executor=self)
                try:
                    self._check_exit_for_position(pos, obs)
                finally:
                    obs.persist()
            finally:
                lock.release()

    def close_position_manual(self, pos_id, note=None):
        """Close one position on demand from the dashboard.

        Returns a status dict rather than None-on-everything like
        _close_position: a human pressing a button needs to be told the
        difference between "sold", "partially sold" and "the bid wasn't there,
        it's still open" — outcomes the monitor loop is happy to treat alike
        because it simply retries next cycle.

        Reuses _close_position for the actual sell so manual exits get the same
        real-bid pricing, taker-fee accounting, partial-fill handling and
        exit-depth logging as automatic ones.
        """
        lock = self._exit_lock(pos_id)
        # Non-blocking: if the monitor thread is already exiting this position,
        # say so instead of queueing a second sell behind it.
        if not lock.acquire(blocking=False):
            return {"ok": False, "status": "busy",
                    "message": "This position is already being closed by the bot — try again in a moment."}
        try:
            pos = get_position_by_id(pos_id)
            if not pos:
                return {"ok": False, "status": "not_found",
                        "message": "That position is already closed."}

            shares_before = pos["shares"] or 0
            reason = "MANUAL_CLOSE" + (f" ({note})" if note else "")
            self._close_position(pos, pnl_dollars=None, exit_reason=reason)

            after = get_position_by_id(pos_id)
            if after is None:
                # Read back the realized PnL the close actually booked (trades has
                # no exit_price column — pnl is the ledger's record of the exit).
                trades = fetch_query(
                    "SELECT pnl FROM trades WHERE market_id=? AND side=? AND status='CLOSED' "
                    "AND mode=? "
                    "ORDER BY id DESC LIMIT 1",
                    (pos["market_id"], pos["side"], current_mode()))
                pnl = trades[0]["pnl"] if trades else None
                return {"ok": True, "status": "closed",
                        "market_id": pos["market_id"],
                        "question": pos.get("question"),
                        "pnl": pnl,
                        "message": (f"Position closed. Realized P&L ${pnl:+.2f}."
                                    if pnl is not None else "Position closed.")}

            # Still open: either a partial fill (shares reduced) or no fill at all.
            shares_after = after["shares"] or 0
            if shares_after < shares_before - 0.01:
                return {"ok": True, "status": "partial",
                        "market_id": pos["market_id"],
                        "question": pos.get("question"),
                        "shares_sold": round(shares_before - shares_after, 2),
                        "shares_remaining": round(shares_after, 2),
                        "message": (f"Partial fill: sold {shares_before - shares_after:.2f} of "
                                    f"{shares_before:.2f} shares. The rest is still open and "
                                    f"will be retried automatically.")}
            return {"ok": False, "status": "no_fill",
                    "market_id": pos["market_id"],
                    "question": pos.get("question"),
                    "message": ("No fill — there wasn't enough real bid depth to sell into. "
                                "The position is still open.")}
        finally:
            lock.release()

    @staticmethod
    def _target_date_passed(target_date, now):
        """True if this position's weather target date is in the past (UTC).
        Once it has passed, the outcome is fixed and only resolution ($1/$0)
        should close the position — a paper market-exit at a stale/thin quote
        would fabricate a fill (see _check_exit_for_position)."""
        if not target_date:
            return False
        return target_date < now.strftime("%Y-%m-%d")

    def _settlement_state(self, pos):
        """Where the day's observations have already put this position.

        The bucket lives in `signals`, never in `positions`, so this costs one
        indexed lookup plus one observation fetch per position per cycle — both
        already paid for by the edge-decay path further down. It runs EARLIER
        than that path on purpose: the old ordering let a price rule decide the
        position before the physics was even consulted.

        Fails CLOSED. Every error path returns UNKNOWN, which blocks loss exits
        rather than permitting them.
        """
        if not ENABLE_PHYSICS_EXIT_GATE:
            return {"state": UNKNOWN, "observed": None, "reason": "gate disabled"}
        try:
            rows = fetch_query(
                "SELECT bucket_low, bucket_high FROM signals WHERE market_id=? "
                "ORDER BY id DESC LIMIT 1", (pos["market_id"],))
            if not rows:
                return {"state": UNKNOWN, "observed": None,
                        "reason": "no signal row — bucket unknown"}
            return settlement_state(
                pos["city"], pos["target_date"], bool(pos["is_high"]),
                rows[0]["bucket_low"], rows[0]["bucket_high"], pos["side"])
        except Exception as e:
            logging.warning(
                f"settlement_state unavailable for {pos['market_id']} "
                f"({pos['city']} {pos.get('target_date')}): {e}")
            return {"state": UNKNOWN, "observed": None, "reason": f"error: {e}"}

    def _loss_exit_allowed(self, pos, st, obs):
        """May we sell this position at a loss right now?

        Only when the observations say it is already dead (LOCKED_LOSS). The
        three refusals are each deliberate:

          LOCKED_WIN  the position settles at $1. Selling is pure destruction —
                      this is the Qingdao case once its max cleared 30°C.
          UNDECIDED   the outcome is genuinely open, and if the observed extreme
                      is sitting inside the bucket the book is pricing the
                      transit rather than the outcome. This is the state Qingdao
                      was stopped out in.
          UNKNOWN     no observation, no station, day not started, or the lookup
                      threw. Holding risks the stake, which was a known and
                      accepted number at entry; selling blind into a book that
                      may be dislocated risks far more in regret.

        Gains are unaffected — take-profit never consults this.

        Call this only where a loss exit is actually being attempted: the state
        itself is recorded on the trail every cycle regardless (see
        _check_exit_for_position), so the WARNING here marks the genuinely
        interesting event — a price rule wanted out and the physics overruled it.
        """
        state = st.get("state", UNKNOWN)
        if state == LOCKED_LOSS:
            return True
        logging.warning(
            f"PHYSICS GATE BLOCKED a loss exit | {pos['city']} "
            f"{pos.get('target_date')} {pos['side']} | {state} | {st.get('reason')}")
        return False

    def _exit_liquidity_ok(self, token_id, shares, obs=None):
        """Whether the bid side can absorb `shares` near the quote we measured on.

        Walks the live bid book (force=True — the Qingdao book lost 91% of its bid
        depth over the life of that position, and depth read minutes before a
        submit is not depth) and requires both:
          * the full size fills — no partial sweep of a collapsing book, and
          * the average fill lands within EXIT_MAX_SLIPPAGE_FRAC of the top bid.

        Slippage, not total depth, because total depth is what made Qingdao look
        safe: $109 resting against a $12.73 sale. See scanner._walk_bids.

        Returns (ok, detail). An unreadable book is NOT ok — same fail-closed rule
        as the physics gate.
        """
        est = estimate_sale(token_id, shares, force=True)
        if est is None or est.get("vwap") is None:
            detail = "bid book unreadable"
            ok = False
            slip = None
        else:
            slip = est.get("slippage_frac")
            ok = (not est["exhausted"] and slip is not None
                  and slip <= EXIT_MAX_SLIPPAGE_FRAC)
            detail = (
                f"{shares:.2f}sh -> vwap ${est['vwap']:.4f} vs top bid "
                f"${est['best_bid']:.3f}, slippage "
                f"{f'{slip:.2%}' if slip is not None else 'n/a'} vs cap "
                f"{EXIT_MAX_SLIPPAGE_FRAC:.2%}"
                + (f", BOOK EXHAUSTED (only {est['filled_shares']:.2f}sh fillable)"
                   if est["exhausted"] else ""))
        if obs is not None:
            obs.record_rule("exit_liquidity", basis="bid", observed=slip,
                            threshold=EXIT_MAX_SLIPPAGE_FRAC, fired=not ok,
                            detail=detail)
        if not ok:
            logging.warning(f"EXIT LIQUIDITY BLOCK | {token_id[:12]}… | {detail}")
        return ok, detail

    def _check_exit_for_position(self, pos, obs=None):
        obs = obs if obs is not None else _UnloggedObservation()
        entry_time = datetime.fromisoformat(pos["entry_time"])
        now = datetime.now(timezone.utc)
        hold_minutes = (now - entry_time).total_seconds() / 60.0

        # --- Fast take-profit: fire the INSTANT a real fillable bid reaches the
        # target, ahead of the 30-min hold and the target-date hold-to-resolution
        # gate. Requires a CONFIRMED bid (real order-book depth), never a stale or
        # Gamma-fallback price — that guard is what keeps this from re-becoming the
        # phantom-$0.999 exit. At 0.98 there is almost nothing left to gain by
        # holding to $1, so securing it immediately is the intended behaviour.
        tp_ask, tp_bid = get_realtime_price(pos["token_id"])
        take_profit = setting("TAKE_PROFIT_PRICE")
        obs.record_rule("take_profit", basis="bid", observed=tp_bid,
                        threshold=take_profit, fired=tp_bid >= take_profit,
                        detail="fast path, ahead of the 30-minute hold")
        if tp_bid >= take_profit:
            obs.exit_rule_fired = "take_profit"
            self._close_position(
                pos, pnl_dollars=None,
                exit_reason=f"Take Profit (bid {tp_bid:.3f} >= {take_profit:.2f})",
            )
            return

        if hold_minutes < 30:
            return

        # Resolved ONCE per position per cycle and consulted by every loss exit
        # below. Computed here — ahead of the target-date branch and the price
        # reads — because the ordering is the whole point: until 2026-08-11 a
        # percentage stop measured on the mid could close a position before
        # anything looked at what the day had actually done.
        st = self._settlement_state(pos)
        # Recorded every cycle, fired or not. The question the trail exists for is
        # "would a different rule have behaved differently here", and that needs
        # the observed state on the quiet cycles too.
        obs.record_rule("physics_gate", basis="observation",
                        enabled=ENABLE_PHYSICS_EXIT_GATE,
                        fired=st.get("state") == LOCKED_LOSS,
                        detail=f"{st.get('state')}: {st.get('reason')}")

        # Once the target date has passed the temperature is already realized and
        # the market is converging to $1/$0. Do NOT run the paper edge-decay /
        # stop-loss market-exit path here: on a resolving book the only resting
        # quotes are extreme (~0.999) with no real depth, and booking a fill there
        # fabricated the 5 "edge decayed @ 0.999" exits in the historical DB —
        # phantom fills at a price never once observed with size (max NO in 44,879
        # logged signals was 0.81). Leave it for check_resolved_positions() to
        # settle at the true resolution value instead.
        if self._target_date_passed(pos.get("target_date"), now):
            # One exception to hold-to-resolution: a position already collapsed past
            # STOP_LOSS_PCT is heading to $0, and any real bid still resting on it is
            # money that settlement will not return. Salvage it — but ONLY against a
            # confirmed bid with genuine depth, exactly like the fast take-profit
            # above. A stale/extreme quote with no size is the phantom-fill class this
            # gate exists to block, so the depth check is what makes this safe.
            # Measured 2026-07-26 on the three live collapses: by the time the target
            # date has passed the bid is $0.001-0.02, so this recovers cents, not
            # dollars. The real value is not leaving a fillable bid on the table.
            #
            # Split off ENABLE_STOP_LOSS 2026-08-11: this is not the mid-day stop
            # and must survive that knob being turned off. By this point the local
            # day is over, so settlement_state reads the FINAL extreme and answers
            # LOCKED_WIN or LOCKED_LOSS deterministically — which means the physics
            # gate below is what now decides this, and it can only ever fire on a
            # position genuinely heading to $0.
            if ENABLE_POST_DATE_SALVAGE:
                sl_ask, sl_bid = get_realtime_price(pos["token_id"])
                entry = pos["entry_price"]
                # Deliberately bid-based, and the ONLY bid-based stop in the
                # bot: this path exists to sweep a real fillable bid off a
                # position already heading to $0, so the number that matters is
                # what a seller would actually receive, not the mid.
                sl_frac = ((sl_bid - entry) / entry) if (sl_bid > 0 and entry > 0) else None
                obs.record_rule("stop_loss", basis="bid", observed=sl_frac,
                                threshold=-setting("STOP_LOSS_PCT"),
                                fired=sl_frac is not None and sl_frac <= -setting("STOP_LOSS_PCT"),
                                detail="post-target-date salvage; needs confirmed bid depth")
                # The percentage that used to trigger this is gone: with the day
                # over, LOCKED_LOSS already means "settles at $0", so any real bid
                # beats holding and no threshold adds information. The gate is
                # strictly tighter than the old -50% test — it cannot fire on a
                # position that settlement will pay out.
                if sl_bid > 0 and entry > 0 and self._loss_exit_allowed(pos, st, obs):
                    _, bid_depth = get_orderbook_depth_usd(pos["token_id"])
                    shares_held = pos["size_usdc"] / entry
                    # Plain depth test, and deliberately NOT the slippage guard the
                    # mid-day exits use: this position settles at $0, so any
                    # fillable bid beats holding and there is no better price to
                    # slip away from. Guarding slippage here would just forfeit the
                    # cents this path exists to collect.
                    if bid_depth is not None and bid_depth >= shares_held * sl_bid * SALVAGE_MIN_DEPTH_MULTIPLE:
                        obs.exit_rule_fired = "stop_loss_post_date_salvage"
                        self._close_position(
                            pos, pnl_dollars=None,
                            exit_reason=(
                                f"Post-date salvage "
                                f"({(sl_bid - entry) / entry:.1%}, bid ${sl_bid:.3f}, "
                                f"{st.get('state')}, depth ${bid_depth:.2f})"
                            ),
                        )
                        return
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

        # Record all three cheap rules before acting on any of them. Recording
        # only the one that fired would answer "why did it exit" and nothing
        # else; the question the trail exists for — would a DIFFERENT threshold
        # have fired here — needs the observed value on every cycle, including
        # the cycles where nothing happened.
        stop_pct = setting("STOP_LOSS_PCT")
        stop_on = setting("ENABLE_STOP_LOSS")
        obs.record_rule("sustained_loss", basis="mid", observed=streak,
                        threshold=SUSTAINED_LOSS_POLLS,
                        enabled=ENABLE_SUSTAINED_LOSS_GUARD,
                        fired=ENABLE_SUSTAINED_LOSS_GUARD and streak >= SUSTAINED_LOSS_POLLS,
                        detail=(f"drawdown {pnl_pct:.4f} vs min_drop "
                                f"{-SUSTAINED_LOSS_MIN_DROP:.4f}"))
        # The deployed stop measures on MID (current_price), not bid. Both are
        # stored so the choice can be re-examined against real data rather than
        # re-argued: on a book allowing 15% spread the two disagree materially.
        obs.record_rule("stop_loss", basis="mid", observed=pnl_pct,
                        threshold=-stop_pct, enabled=stop_on,
                        fired=pnl_pct <= -stop_pct,
                        detail=("deployed basis" + (" (Gamma fallback price)"
                                                    if used_gamma_fallback else "")))
        bid_frac = ((bid_price - entry_price) / entry_price
                    if bid_price > 0 and entry_price > 0 else None)
        obs.record_rule("stop_loss", basis="bid", observed=bid_frac,
                        threshold=-stop_pct, enabled=stop_on,
                        fired=None if bid_frac is None else bid_frac <= -stop_pct,
                        detail="counterfactual — the deployed stop fires on mid")
        obs.record_rule("take_profit", basis="bid", observed=exit_fill,
                        threshold=setting("TAKE_PROFIT_PRICE"),
                        fired=bid_price > 0 and exit_fill >= setting("TAKE_PROFIT_PRICE"),
                        detail="requires a real bid; Gamma fallback cannot fire this")

        # Both price-based loss rules now answer to the physics gate. Neither can
        # close a position the day's observations have not already killed — that
        # inversion (observations decide, price only proposes) is the fix for
        # Qingdao 2026-08-11, and it holds even if a future operator turns
        # ENABLE_STOP_LOSS back on.
        if (ENABLE_SUSTAINED_LOSS_GUARD and streak >= SUSTAINED_LOSS_POLLS
                and self._loss_exit_allowed(pos, st, obs)):
            obs.exit_rule_fired = "sustained_loss"
            exit_reason = (
                f"Sustained loss ({streak} polls below entry, "
                f"mid=${current_price:.3f} vs entry=${entry_price:.3f}, pnl={pnl_pct:.1%})"
            )
        elif (setting("ENABLE_STOP_LOSS") and pnl_pct <= -setting("STOP_LOSS_PCT")
                and self._loss_exit_allowed(pos, st, obs)):
            obs.exit_rule_fired = "stop_loss"
            exit_reason = f"Stop Loss ({pnl_pct:.1%}, {st.get('state')})"
        elif bid_price > 0 and exit_fill >= setting("TAKE_PROFIT_PRICE"):
            obs.exit_rule_fired = "take_profit"
            # bid_price > 0 guard: on the Gamma fallback exit_fill is a stale
            # estimate with NO real book behind it — firing take-profit there books
            # a paper fill nobody would pay (the phantom-$0.999 failure class).
            exit_reason = f"Take Profit (Price {exit_fill:.2f} >= {setting('TAKE_PROFIT_PRICE'):.2f})"
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
                # Pass the REAL time to resolution — omitting it defaulted to 48h,
                # so exit-side probabilities were computed against a forecast error
                # band ~2x too wide in the final hours before settlement.
                hours_left = 48.0
                try:
                    target_dt = parse_utc_datetime(pos["target_date"] + "T23:59:00+00:00")
                    hours_left = max(0.0, (target_dt - now).total_seconds() / 3600.0)
                except Exception:
                    pass
                engine_res = get_signal_engine(
                    pos["city"], pos["target_date"], bool(pos["is_high"]),
                    hours_to_resolution=hours_left,
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
                                obs_val_f = round_half_away(obs_val_c) * 9.0 / 5.0 + 32.0
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

                # Recorded here and nowhere else: this is the only point in the
                # cycle where a fresh ensemble has actually been run, so it is
                # the only point where the observed edge is a real number rather
                # than a guess. Cycles that never get here store the rule as
                # unevaluated instead of inventing a value.
                obs.record_rule(
                    "thesis_break", observed=current_edge, threshold=adaptive_floor,
                    enabled=ENABLE_THESIS_BREAK_EXIT,
                    fired=current_edge < adaptive_floor,
                    detail=(f"model_prob {latest_prob:.4f}, mid {current_price:.4f}, "
                            f"floor {'raised (late-market)' if adaptive_floor > EXIT_EDGE_FLOOR else 'base'}"))

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
                    # _thesis_broken returns True on "we are in a real loss", so
                    # this rule reaches for the same trigger the stop did and needs
                    # the same gate. Note the guard cannot live inside the
                    # condition below: `not HOLD_WINNERS_TO_RESOLUTION` exits
                    # regardless of thesis_broken, so it gets its own branch. A
                    # thesis-break IN PROFIT is a different decision and passes
                    # through untouched.
                    if (exit_fill < entry_price
                            and not self._loss_exit_allowed(pos, st, obs)):
                        pass
                    elif thesis_broken or not HOLD_WINNERS_TO_RESOLUTION:
                        obs.exit_rule_fired = "thesis_break"
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
            # Choke point. Every mid-day loss exit funnels through here, so both
            # guards are re-asserted at the last instant before real money moves —
            # deliberately duplicating the per-branch checks above. A guard that
            # only exists on the paths someone remembered to wire it into is not a
            # guard, and the branch above is an elif chain a future rule can be
            # appended to. Gains skip both: take-profit selling into a thin book
            # only forgoes upside, it cannot manufacture a Qingdao.
            if exit_fill < entry_price:
                if not self._loss_exit_allowed(pos, st, obs):
                    return
                shares_held = pos["size_usdc"] / entry_price if entry_price > 0 else 0
                liq_ok, _ = self._exit_liquidity_ok(pos["token_id"], shares_held, obs)
                if not liq_ok:
                    # Standing down, not cancelling: the rule stays armed and the
                    # next cycle re-reads the book. Qingdao's exit swept 15.15
                    # shares into $109 of depth and realized 2.2c THROUGH the top
                    # bid; refusing that print costs nothing when the position is
                    # genuinely dead, because a dead position stays dead.
                    logging.warning(
                        f"Loss exit STOOD DOWN for {pos['city']} "
                        f"{pos.get('target_date')} ({exit_reason}) — insufficient "
                        f"bid liquidity; will retry next cycle")
                    return
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
                "AND mode=? "
            "ORDER BY id DESC LIMIT 1",
            (pos["market_id"], pos["side"], current_mode()),
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
        # pnl_dollars=None means "compute it here from the confirmed exit bid"
        # (used by the fast take-profit path). Live mode overrides it from the
        # actual fill below; paper mode books this estimate. Book the sell into
        # the real bid minus the taker fee — never an optimistic mid.
        if pnl_dollars is None:
            try:
                _, close_bid = get_realtime_price(pos["token_id"])
            except Exception:
                close_bid = 0.0
            exit_px = close_bid if close_bid > 0 else pos["entry_price"]
            shares = pos["shares"] if pos.get("shares") else (
                pos["size_usdc"] / pos["entry_price"] if pos["entry_price"] > 0 else 0)
            fee = TAKER_FEE_RATE * exit_px * (1.0 - exit_px) * shares
            pnl_dollars = (exit_px - pos["entry_price"]) * shares - fee
        logging.info(
            f"{'PAPER ' if paper_mode() else ''}EXIT: {pos['market_id']} ({pos['side']}) — "
            f"{exit_reason} | PnL: ${pnl_dollars:.2f}"
        )

        # Order-book depth at exit — the BID side specifically, since closing a
        # position means selling into it (not the ask side logged at entry, which
        # only matters for a fresh buy). Captures whether the market that looked
        # liquid going in was still liquid coming out; a single entry-time depth
        # reading can't tell you that (Seoul/Madrid both went to zero asks after
        # entry this session — the bid side is what actually would have mattered
        # for exiting, and this is the same gap that motivated logging depth at
        # all). Best-effort: exit still proceeds even if depth can't be read.
        try:
            exit_ask_depth, exit_bid_depth = get_orderbook_depth_usd(pos["token_id"])
        except Exception:
            exit_ask_depth, exit_bid_depth = None, None

        # EXTERNAL_ closes were already sold by the user on Polymarket — the
        # shares are gone, so submitting a CLOB sell would be rejected forever
        # and strand the DB row open.
        skip_clob_exit = (exit_reason == "EXPIRED_ON_RESTART"
                          or exit_reason.startswith("RESOLVED_")
                          or exit_reason.startswith("EXTERNAL_"))
        if not paper_mode() and not skip_clob_exit:
            # Prefer the share count actually filled at entry (positions.shares);
            # the historical fallback re-derives it from doubly-rounded values and
            # can exceed real holdings, making the CLOB reject the sell forever.
            held_shares = pos["shares"] if pos.get("shares") else round(pos["size_usdc"] / pos["entry_price"], 2)
            # Fallback price for an unreadable fill: the live bid (what a taker
            # SELL actually crosses into), else entry price — never the 0.5
            # placeholder, which would fabricate the exit PnL and bankroll credit.
            try:
                _, live_bid = get_realtime_price(pos["token_id"])
            except Exception:
                live_bid = 0.0
            sell_fallback = live_bid if live_bid > 0 else pos["entry_price"]
            fill = self._submit_taker(pos["token_id"], "SELL", held_shares,
                                      fallback_price=sell_fallback)   # amount = shares
            if not fill:
                logging.warning(f"Exit SELL did not fill for {pos['market_id']}; leaving open for retry.")
                return
            # Recompute realized PnL from the ACTUAL exit fill price, not the mid estimate.
            # Must subtract the taker fee here too — the paper-mode estimate above does
            # (exit_fee), but this live path previously dropped it entirely even though
            # fee_bps was already fetched, silently overstating every live exit's PnL.
            exit_price = fill["price"]
            sold = min(fill["shares"], held_shares)
            sold_fee = self._fill_fee_rate(fill) * exit_price * (1.0 - exit_price) * sold
            sold_pnl = (exit_price - pos["entry_price"]) * sold - sold_fee
            logging.info(
                f"EXIT FILLED {pos['market_id']} ({pos['side']}): {sold} sh of {held_shares} "
                f"@ ${exit_price:.4f} | realized PnL ${sold_pnl:.2f} | fee_bps={fill['fee_bps']}"
            )
            if sold < held_shares - 0.01:
                # PARTIAL fill: an FAK order takes what the bid holds and kills the
                # rest. Booking a full close here stranded the unsold shares
                # on-chain while the DB went flat and the bankroll was credited
                # cash never received. Shrink the position to the remainder and
                # leave it open — the next monitor cycle retries the rest.
                entry_cost_freed = sold * pos["entry_price"]
                proceeds = sold * exit_price - sold_fee
                reduce_position_atomic(
                    pos_id=pos["id"], market_id=pos["market_id"], side=pos["side"],
                    sold_shares=sold, entry_cost_freed=entry_cost_freed,
                    proceeds=proceeds, pnl_delta=sold_pnl,
                )
                logging.warning(
                    f"PARTIAL EXIT {pos['market_id']}: sold {sold}/{held_shares} sh — "
                    f"position reduced to {held_shares - sold:.2f} sh, retrying remainder next cycle."
                )
                return
            pnl_dollars = sold_pnl

        closed = close_position_atomic(
            pos_id=pos["id"],
            market_id=pos["market_id"],
            side=pos["side"],
            pnl_dollars=pnl_dollars,
            size_usdc=pos["size_usdc"],
            exit_reason=exit_reason,
            exit_ask_depth_usd=exit_ask_depth,
            exit_bid_depth_usd=exit_bid_depth,
        )
        if not closed:
            logging.warning(f"Position {pos['id']} already closed by another thread — skipping duplicate close")
            return

        entry_time = datetime.fromisoformat(pos["entry_time"])
        duration_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600.0
        pnl_pct = pnl_dollars / pos["size_usdc"] if pos["size_usdc"] > 0 else 0
        market_label = pos.get("question") or pos["market_id"]
        send_trade_exit(market_label, pnl_dollars, pnl_pct, duration_hours, exit_reason)
