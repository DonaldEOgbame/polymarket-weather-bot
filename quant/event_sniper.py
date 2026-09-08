"""
quant/event_sniper.py - Path A: Real-Time Event-Driven Physical Certainty Sniper.

Replaces slow 10-minute periodic polling with a high-frequency, event-driven loop:
1. Ingests live METAR/SPECI observations from FAA/NOAA Aviation Weather Center
   every 5-15 seconds for all stations with active markets expiring today.
2. Evaluates temperature thresholds against pre-registered buckets using
   NanosecondSniperCore (210ns opcode dispatch).
3. On physical lock, immediately pulls DualOrderBookL2 order books to sweep resting
   liquidity <= 0.96 within milliseconds of NOAA publication.
4. Executes trades directly via OrderExecutor without blocking the main event loop.
"""

import os
import sys
import time
import json
import logging
import threading
import urllib.request
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone

from quant.fast_sniper import NanosecondSniperCore, PrebuiltSnipeOrder
from quant.book import DualOrderBookL2
from scanner import scan_markets, get_book, _best_ask_bid_from_book, _book_depth_usd
from lattice import quantise_c
from db import get_portfolio_state, log_sniper_audit
from alerts import send_trade_entry
from types import SimpleNamespace
import config as C


class EventDrivenSniper:
    def __init__(self, executor, poll_interval_sec: float = 10.0, max_snipe_price: float = 0.96):
        self.executor = executor
        self.poll_interval = poll_interval_sec
        self.max_snipe_price = max_snipe_price
        self.running = False
        self.thread: Optional[threading.Thread] = None

        self.core = NanosecondSniperCore()
        self._last_obs_times: Dict[str, str] = {}
        self._registered_orders: Dict[str, PrebuiltSnipeOrder] = {}
        self._market_by_token: Dict[str, dict] = {}
        self._sniped_buckets: Set[str] = set()
        self._active_markets_by_station: Dict[str, List[dict]] = {}

    def start(self):
        """Start the background event listener thread."""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, name="EventDrivenSniper", daemon=True)
        self.thread.start()
        logging.info("🎯 EventDrivenSniper started (listening for NOAA/FAA METAR broadcasts)...")

    def stop(self):
        """Stop the background listener thread."""
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        logging.info("🎯 EventDrivenSniper stopped.")

    def refresh_active_markets(self):
        """Discover today's active markets and register them with the nanosecond core."""
        try:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            all_markets = scan_markets()
            today_markets = [m for m in all_markets if getattr(m, "date", "") <= today_utc]

            self.core = NanosecondSniperCore()
            self._registered_orders.clear()
            self._market_by_token.clear()
            self._active_markets_by_station.clear()

            from metar import STATION_ICAO

            for m in today_markets:
                city = getattr(m, "city", "")
                stn_info = STATION_ICAO.get(city)
                if not stn_info:
                    continue
                icao = stn_info[0]

                m_dict = {
                    "market_id": m.market_id,
                    "city": city,
                    "station_icao": icao,
                    "date": m.date,
                    "is_high": m.is_high,
                    "bucket_low": m.bucket_low,
                    "bucket_high": m.bucket_high,
                    "bucket_type": getattr(m, "bucket_type", "range"),
                    "tokens": getattr(m, "tokens", {}),
                    "bucket_label": f"{m.bucket_low}-{m.bucket_high}",
                    "opp": m
                }
                self._active_markets_by_station.setdefault(icao, []).append(m_dict)

                no_token = getattr(m, "token_id_no", None) or getattr(m, "tokens", {}).get("NO")
                can_register = (m.is_high and m.bucket_high is not None) or (not m.is_high and m.bucket_low is not None)
                if no_token and can_register:
                    order = PrebuiltSnipeOrder(
                        token_id=no_token,
                        price=self.max_snipe_price,
                        size=10.0,
                        raw_payload_bytes=b"",
                        target_date=m.date,
                        city=city,
                        side="NO"
                    )
                    self.core.register_no_trigger(
                        icao=icao,
                        bucket_id=m.market_id,
                        is_high=m.is_high,
                        bucket_low=m.bucket_low,
                        bucket_high=m.bucket_high,
                        prebuilt_order=order,
                        pad=0.0
                    )
                    self._registered_orders[m.market_id] = order
                    self._market_by_token[no_token] = m_dict

            station_count = len(self._active_markets_by_station)
            market_count = sum(len(ms) for ms in self._active_markets_by_station.values())
            logging.info(f"🎯 EventDrivenSniper: Watching {market_count} buckets across {station_count} stations.")
        except Exception as e:
            logging.error(f"Error refreshing sniper markets: {e}", exc_info=True)

    def _fetch_live_metars(self, stations: List[str]) -> List[dict]:
        """Fetch real-time METARs from FAA/NOAA Aviation Weather Center in a single call."""
        if not stations:
            return []
        ids_str = ",".join(stations)
        url = f"https://aviationweather.gov/api/data/metar?ids={ids_str}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "PolymarketSniperBot/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logging.debug(f"Aviationweather METAR fetch warning: {e}")
        return []

    def _evaluate_metar_tick(self, metar_item: dict):
        """Process a live METAR observation."""
        icao = metar_item.get("icaoId")
        obs_time = str(metar_item.get("obsTime", ""))
        temp_c = metar_item.get("temp")

        if not icao or temp_c is None:
            return

        last_time = self._last_obs_times.get(icao)
        if last_time and obs_time <= last_time:
            return
        self._last_obs_times[icao] = obs_time

        markets = self._active_markets_by_station.get(icao, [])
        if not markets:
            return
        city = markets[0]["city"]
        temp_f = quantise_c(float(temp_c), city)

        # 210ns opcode evaluation
        t_trigger = time.perf_counter()
        fired_orders = self.core.evaluate_observation_hot_path(icao, temp_f)
        for order in fired_orders:
            if order.token_id in self._sniped_buckets:
                continue
            self._attempt_snipe(order, temp_f, t_trigger)

    def _attempt_snipe(self, order: PrebuiltSnipeOrder, observed_temp_f: float, t_trigger: Optional[float] = None):
        """Sweep resting liquidity on the Polymarket CLOB for a physically locked bucket."""
        if t_trigger is None:
            t_trigger = time.perf_counter()

        m_info = self._market_by_token.get(order.token_id, {})
        market_id = m_info.get("market_id", "")
        bucket_label = m_info.get("bucket_label", "")
        station_icao = m_info.get("station_icao", "")
        city = order.city
        target_date = order.target_date

        try:
            book_data = get_book(order.token_id, force=True)
            latency_ms = (time.perf_counter() - t_trigger) * 1000.0

            if not book_data:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=None, best_bid=None, ask_depth_usd=0.0, stake_usd=0.0,
                    outcome="BOOK_UNAVAILABLE", detail="CLOB order book fetch returned empty",
                    latency_ms=latency_ms
                )
                self._sniped_buckets.add(order.token_id)
                return

            best_ask, best_bid = _best_ask_bid_from_book(book_data)
            ask_depth, _ = _book_depth_usd(book_data)

            if best_ask is None or best_ask <= 0:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=best_ask, best_bid=best_bid, ask_depth_usd=0.0, stake_usd=0.0,
                    outcome="NO_ASKS", detail="Book has zero ask levels",
                    latency_ms=latency_ms
                )
                self._sniped_buckets.add(order.token_id)
                return

            if best_ask > order.price:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=best_ask, best_bid=best_bid, ask_depth_usd=ask_depth, stake_usd=0.0,
                    outcome="REPRICED_ABOVE_CEILING",
                    detail=f"Market repriced to {best_ask:.3f} > max ceiling {order.price:.2f}",
                    latency_ms=latency_ms
                )
                self._sniped_buckets.add(order.token_id)
                return

            if ask_depth <= 0:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=best_ask, best_bid=best_bid, ask_depth_usd=0.0, stake_usd=0.0,
                    outcome="ZERO_DEPTH", detail="No resting size available at or below ceiling",
                    latency_ms=latency_ms
                )
                self._sniped_buckets.add(order.token_id)
                return

            portfolio = get_portfolio_state()
            available = portfolio.get("available_cash", 0.0)
            stake = min(ask_depth, order.size, available * 0.15)
            if stake < 1.0:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=best_ask, best_bid=best_bid, ask_depth_usd=ask_depth, stake_usd=stake,
                    outcome="INSUFFICIENT_FUNDS",
                    detail=f"Stake ${stake:.2f} below $1.00 minimum (available cash: ${available:.2f})",
                    latency_ms=latency_ms
                )
                self._sniped_buckets.add(order.token_id)
                return

            opp = m_info.get("opp")
            if opp is None:
                opp = SimpleNamespace(
                    market_id=market_id,
                    city=city,
                    date=target_date,
                    is_high=m_info.get("is_high", True),
                    question=m_info.get("question", f"{city} weather"),
                )

            signal_data = {
                "signal": True,
                "opp": opp,
                "action": "BUY",
                "side": order.side,
                "token_id": order.token_id,
                "market_id": market_id,
                "city": order.city,
                "price": best_ask,
                "size_usdc": stake,
                "stake_usd": stake,
                "walked_vwap": best_ask,
                "edge": 1.0 - best_ask,
                "model_prob": 1.0,
                "reason": f"PHYSICAL_CERTAINTY_SNIPE: {order.city} reached {observed_temp_f:.1f}°F ({bucket_label})"
            }

            logging.info(
                f"⚡ [SNIPER TRIGGERED] Sweeping {order.side} on {order.city} {bucket_label} "
                f"@ ${best_ask:.3f} (depth: ${ask_depth:.1f}, stake: ${stake:.2f})!"
            )

            res = self.executor.execute_trade(signal_data)
            self._sniped_buckets.add(order.token_id)

            if res is not False and getattr(self.executor, "_entry_recording_broken", False) is False:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=best_ask, best_bid=best_bid, ask_depth_usd=ask_depth, stake_usd=stake,
                    outcome="FILLED", detail="Order executed and submitted",
                    latency_ms=latency_ms
                )
                logging.info(f"✅ [SNIPER FILLED] Successfully executed physical certainty snipe on {order.city}!")
                send_trade_entry(f"{order.city} {bucket_label}", best_ask, 1.0, 1.0 - best_ask, stake)
            else:
                log_sniper_audit(
                    station_icao=station_icao, city=city, target_date=target_date,
                    bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                    best_ask=best_ask, best_bid=best_bid, ask_depth_usd=ask_depth, stake_usd=stake,
                    outcome="EXECUTION_REJECTED", detail="Executor refused or rejected the trade",
                    latency_ms=latency_ms
                )
        except Exception as e:
            latency_ms = (time.perf_counter() - t_trigger) * 1000.0 if t_trigger else 0.0
            log_sniper_audit(
                station_icao=station_icao, city=city, target_date=target_date,
                bucket_label=bucket_label, side=order.side, observed_temp_f=observed_temp_f,
                best_ask=None, best_bid=None, ask_depth_usd=0.0, stake_usd=0.0,
                outcome="ERROR", detail=str(e), latency_ms=latency_ms
            )
            logging.error(f"Error during snipe execution for {order.token_id}: {e}", exc_info=True)

    def _run_loop(self):
        """Continuous event monitoring loop."""
        last_refresh = 0.0
        while self.running:
            try:
                now = time.time()
                if now - last_refresh > 300.0:
                    self.refresh_active_markets()
                    last_refresh = now

                stations = list(self._active_markets_by_station.keys())
                if stations:
                    metars = self._fetch_live_metars(stations)
                    for m in metars:
                        self._evaluate_metar_tick(m)
            except Exception as e:
                logging.error(f"Exception in EventDrivenSniper loop: {e}", exc_info=True)

            time.sleep(self.poll_interval)
