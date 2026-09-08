import asyncio
import logging
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.book import OrderBookL2, DualOrderBookL2
from quant.sniper import MetarSettlementSniper, SnipeSignal
from quant.fast_sniper import NanosecondSniperCore, PrebuiltSnipeOrder
from quant.qrisk import InstitutionalRiskManager
from db import fetch_query, open_position_atomic


class QuantSniperEngine:
    """Ultra-low-latency pure physical certainty sniper execution engine."""

    def __init__(
        self,
        paper_mode: bool = True,
        max_snipe_price: float = 0.96,
        min_edge: float = 0.03
    ):
        self.paper_mode = paper_mode
        self.sniper = MetarSettlementSniper(max_snipe_price=max_snipe_price, min_edge=min_edge)
        self.fast_core = NanosecondSniperCore()
        self.risk = InstitutionalRiskManager(max_total_exposure_pct=0.70, max_concurrent_positions=12)
        self.books: Dict[str, OrderBookL2] = {}
        self.running = False

    def get_or_create_book(self, token_id: str) -> OrderBookL2:
        if token_id not in self.books:
            self.books[token_id] = OrderBookL2(token_id)
        return self.books[token_id]

    def update_book_from_market_data(
        self,
        token_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        depth_usd: float = 50.0
    ):
        book = self.get_or_create_book(token_id)
        bids = [(best_bid, depth_usd / best_bid)] if (best_bid and best_bid > 0) else []
        asks = [(best_ask, depth_usd / best_ask)] if (best_ask and best_ask > 0) else []
        book.update_snapshot(bids=bids, asks=asks, timestamp=time.time())

    async def execute_snipe(self, signal: SnipeSignal) -> bool:
        portfolio = self.get_portfolio_state()
        stake = min(signal.usable_depth, max(2.0, portfolio["bankroll"] * 0.15))

        risk_res = self.risk.evaluate_entry(
            strategy_name="METAR_SNIPER",
            city=signal.city,
            target_date=signal.target_date,
            requested_stake_usd=stake,
            bankroll_usd=portfolio["bankroll"],
            locked_cash_usd=portfolio["locked"],
            high_water_mark_usd=portfolio["total_equity"],
            daily_realized_pnl_usd=portfolio["daily_pnl"],
            open_positions=portfolio["open_positions"]
        )
        if not risk_res.allowed:
            logging.info(f"Snipe gated by risk: {risk_res.reason}")
            return False

        fill_stake = risk_res.adjusted_stake_usd
        fill_price = signal.vwap
        shares = fill_stake / fill_price

        logging.info(
            f"🎯 [METAR SNIPER] Executing {signal.side} on {signal.city} {signal.target_date} "
            f"@ {fill_price:.3f} (${fill_stake:.2f}, {shares:.1f} shares) | Edge: {signal.edge:.1%}"
        )

        open_position_atomic(
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            price=fill_price,
            stake=fill_stake,
            question=f"METAR Snipe: {signal.city} {signal.target_date}",
            is_high=True,
            city=signal.city,
            target_date=signal.target_date,
            end_date_iso=signal.target_date,
            shares=shares,
            mode="paper" if self.paper_mode else "live"
        )
        return True

    def get_portfolio_state(self) -> Dict[str, any]:
        b_rows = fetch_query("SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1")
        balance = float(b_rows[0]["balance"]) if b_rows else 100.0
        mode = "paper" if self.paper_mode else "live"
        pos_rows = fetch_query("SELECT * FROM positions WHERE mode=?", (mode,))
        locked = sum(float(p["size_usdc"]) for p in pos_rows)
        pnl_rows = fetch_query(
            "SELECT pnl FROM trades WHERE mode=? AND date(exit_time) = date('now')",
            (mode,)
        )
        daily_pnl = sum(float(r["pnl"]) for r in pnl_rows if r["pnl"] is not None)
        return {
            "bankroll": balance,
            "locked": locked,
            "total_equity": balance + locked,
            "daily_pnl": daily_pnl,
            "open_positions": pos_rows
        }
