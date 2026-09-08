"""
quant/sniper.py - Sub-Second METAR Physical Settlement Sniper.

Exploits arithmetic certainty during the intraday settlement window.
When an airport station thermometer confirms that a daily extreme has already
breached a bucket boundary, the outcome is mathematically LOCKED (P=1.00 for NO).
Snipes stale resting asks priced <= 0.96 on the CLOB with near-zero downside.
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

from intraday import settlement_state, LOCKED_WIN, LOCKED_LOSS
from metar import resolved_extreme_f, get_station
from quant.book import OrderBookL2, DualOrderBookL2


@dataclass
class SnipeSignal:
    market_id: str
    token_id: str
    city: str
    target_date: str
    side: str  # "NO" or "YES"
    best_ask: float
    vwap: float
    usable_depth: float
    edge: float
    observed_temp: float
    bucket_low: Optional[float]
    bucket_high: Optional[float]
    confidence: float = 1.0


class MetarSettlementSniper:
    """Institutional physical certainty sniper with dual YES/NO and synthetic book support."""

    def __init__(self, max_snipe_price: float = 0.96, min_edge: float = 0.03):
        self.max_snipe_price = max_snipe_price
        self.min_edge = min_edge

    def evaluate_side(
        self,
        market_id: str,
        token_id: str,
        side: str,
        city_key: str,
        target_date: str,
        is_high: bool,
        bucket_low: Optional[float],
        bucket_high: Optional[float],
        book: OrderBookL2,
        obs: float,
        stake_usd: float = 5.0
    ) -> Optional[SnipeSignal]:
        """Evaluate a specific side (YES or NO) for physical lock certainty."""
        side_norm = side.upper()

        # Check physical settlement state (strict monotonic barrier breaches only)
        state_res = settlement_state(
            city_key=city_key,
            target_date=target_date,
            is_high=is_high,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            side=side_norm,
            observed=obs,
            strict_monotonic=True
        )
        state_val = state_res.get("state") if isinstance(state_res, dict) else state_res

        if state_val != LOCKED_WIN:
            return None

        ba = book.best_ask
        if ba is None or ba > self.max_snipe_price:
            return None

        vwap, shares, spent = book.walk_buy(stake_usd)
        if vwap is not None and vwap <= self.max_snipe_price:
            edge = 1.0 - vwap
            if edge >= self.min_edge:
                depth = book.depth_at_or_below(self.max_snipe_price)
                return SnipeSignal(
                    market_id=market_id,
                    token_id=token_id,
                    city=city_key,
                    target_date=target_date,
                    side=side_norm,
                    best_ask=ba,
                    vwap=vwap,
                    usable_depth=depth,
                    edge=edge,
                    observed_temp=obs,
                    bucket_low=bucket_low,
                    bucket_high=bucket_high,
                    confidence=0.999
                )
        return None

    def evaluate_market(
        self,
        market_id: str,
        token_id_no: str,
        city_key: str,
        target_date: str,
        is_high: bool,
        bucket_low: Optional[float],
        bucket_high: Optional[float],
        book: OrderBookL2,
        stake_usd: float = 5.0,
        token_id_yes: Optional[str] = None,
        book_yes: Optional[OrderBookL2] = None,
        obs: Optional[float] = None
    ) -> Optional[SnipeSignal]:
        """Evaluate whether a market offers a locked physical certainty on NO or YES."""
        # 1. Fetch observed extreme so far if not injected
        if obs is None:
            try:
                obs = resolved_extreme_f(city_key, target_date, is_high)
            except Exception as e:
                logging.debug(f"METAR read failed for {city_key} {target_date}: {e}")
                return None

        if obs is None:
            return None

        best_sig: Optional[SnipeSignal] = None

        # 2. Evaluate NO side
        if token_id_no and book:
            sig_no = self.evaluate_side(
                market_id=market_id,
                token_id=token_id_no,
                side="NO",
                city_key=city_key,
                target_date=target_date,
                is_high=is_high,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                book=book,
                obs=obs,
                stake_usd=stake_usd
            )
            if sig_no:
                best_sig = sig_no

        # 3. Evaluate YES side (if token & book provided)
        if token_id_yes and book_yes:
            sig_yes = self.evaluate_side(
                market_id=market_id,
                token_id=token_id_yes,
                side="YES",
                city_key=city_key,
                target_date=target_date,
                is_high=is_high,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                book=book_yes,
                obs=obs,
                stake_usd=stake_usd
            )
            if sig_yes:
                if best_sig is None or sig_yes.edge > best_sig.edge:
                    best_sig = sig_yes

        return best_sig

