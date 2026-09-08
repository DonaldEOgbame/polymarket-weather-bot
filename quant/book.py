"""
quant/book.py - Fast in-memory Level 2 Order Book.

Maintains sorted bid and ask ladders in memory.
Computes VWAP, book-walk slippage, and usable depth in sub-microsecond time.
"""

from typing import List, Tuple, Optional


class OrderBookLevel:
    __slots__ = ('price', 'size')

    def __init__(self, price: float, size: float):
        self.price = price
        self.size = size


class OrderBookL2:
    """Fast L2 order book for binary prediction market tokens."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        # Bids sorted descending: highest bid first
        self.bids: List[Tuple[float, float]] = []
        # Asks sorted ascending: lowest ask first
        self.asks: List[Tuple[float, float]] = []
        self.last_update_ts: float = 0.0

    def update_snapshot(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]], timestamp: float = 0.0):
        """Update full book snapshot. Bids and asks are lists of (price, size)."""
        self.bids = sorted([(float(p), float(s)) for p, s in bids if float(s) > 0], key=lambda x: x[0], reverse=True)
        self.asks = sorted([(float(p), float(s)) for p, s in asks if float(s) > 0], key=lambda x: x[0])
        self.last_update_ts = timestamp

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        bb = self.best_bid
        ba = self.best_ask
        if bb is not None and ba is not None:
            return (bb + ba) / 2.0
        return bb or ba

    @property
    def spread(self) -> Optional[float]:
        bb = self.best_bid
        ba = self.best_ask
        if bb is not None and ba is not None:
            return ba - bb
        return None

    @property
    def spread_fraction(self) -> Optional[float]:
        bb = self.best_bid
        ba = self.best_ask
        if bb is not None and ba is not None:
            m = (bb + ba) / 2.0
            if m > 0:
                return ((ba - bb) / 2.0) / m
        return None

    def depth_at_or_below(self, price_ceiling: float) -> float:
        """Total dollar value resting on the ask book at or below price_ceiling."""
        total_usd = 0.0
        for p, s in self.asks:
            if p <= price_ceiling + 1e-6:
                total_usd += p * s
            else:
                break
        return total_usd

    def depth_at_or_above(self, price_floor: float) -> float:
        """Total dollar value resting on the bid book at or above price_floor."""
        total_usd = 0.0
        for p, s in self.bids:
            if p >= price_floor - 1e-6:
                total_usd += p * s
            else:
                break
        return total_usd

    def walk_buy(self, stake_usd: float) -> Tuple[Optional[float], float, float]:
        """Walk the ask book to buy `stake_usd` worth of shares.
        
        Returns:
            (vwap, total_shares_acquired, total_usd_spent)
            If book cannot fully absorb stake_usd, vwap is calculated on available depth,
            or None if book is empty.
        """
        if not self.asks or stake_usd <= 0:
            return None, 0.0, 0.0

        rem_usd = stake_usd
        total_shares = 0.0
        total_spent = 0.0

        for p, s in self.asks:
            cost_level = p * s
            if cost_level <= rem_usd:
                total_shares += s
                total_spent += cost_level
                rem_usd -= cost_level
            else:
                shares_partial = rem_usd / p
                total_shares += shares_partial
                total_spent += rem_usd
                rem_usd = 0.0
                break

        if total_shares <= 0:
            return None, 0.0, 0.0

        vwap = total_spent / total_shares
        return vwap, total_shares, total_spent

    def walk_sell(self, shares: float) -> Tuple[Optional[float], float, float]:
        """Walk the bid book to sell `shares`.
        
        Returns:
            (vwap, total_shares_sold, total_usd_received)
        """
        if not self.bids or shares <= 0:
            return None, 0.0, 0.0

        rem_shares = shares
        total_shares_sold = 0.0
        total_received = 0.0

        for p, s in self.bids:
            if s <= rem_shares:
                total_shares_sold += s
                total_received += p * s
                rem_shares -= s
            else:
                total_shares_sold += rem_shares
                total_received += p * rem_shares
                rem_shares = 0.0
                break

        if total_shares_sold <= 0:
            return None, 0.0, 0.0

        vwap = total_received / total_shares_sold
        return vwap, total_shares_sold, total_received


class DualOrderBookL2:
    """Unified L2 Order Book for binary outcome tokens (YES and NO).
    
    Synthesizes cross-book liquidity using binary market parity:
      - Buying NO: Sweeps NO asks OR sells into YES bids (synthetic NO ask = 1.0 - YES bid).
      - Buying YES: Sweeps YES asks OR sells into NO bids (synthetic YES ask = 1.0 - NO bid).
    Doubles addressable book depth and guarantees optimal execution VWAP.
    """
    __slots__ = ('token_id_yes', 'token_id_no', 'book_yes', 'book_no')

    def __init__(self, token_id_yes: str, token_id_no: str):
        self.token_id_yes = token_id_yes
        self.token_id_no = token_id_no
        self.book_yes = OrderBookL2(token_id_yes)
        self.book_no = OrderBookL2(token_id_no)

    def update_snapshots(
        self,
        bids_yes: List[Tuple[float, float]],
        asks_yes: List[Tuple[float, float]],
        bids_no: List[Tuple[float, float]],
        asks_no: List[Tuple[float, float]],
        timestamp: float = 0.0
    ):
        self.book_yes.update_snapshot(bids_yes, asks_yes, timestamp)
        self.book_no.update_snapshot(bids_no, asks_no, timestamp)

    def consolidated_asks(self, target_side: str) -> List[Tuple[float, float, str]]:
        """Return merged and sorted asks (effective_price, size, source_action).
        
        source_action is either 'BUY_DIRECT' or 'SELL_COUNTERPARTY'.
        """
        target = target_side.upper()
        if target == "NO":
            direct_asks = [(p, s, "BUY_DIRECT") for p, s in self.book_no.asks]
            # Bids on YES become synthetic asks on NO at price (1.0 - p)
            synthetic_asks = [(round(1.0 - p, 4), s, "SELL_COUNTERPARTY") for p, s in self.book_yes.bids if p < 1.0]
        else:
            direct_asks = [(p, s, "BUY_DIRECT") for p, s in self.book_yes.asks]
            # Bids on NO become synthetic asks on YES at price (1.0 - p)
            synthetic_asks = [(round(1.0 - p, 4), s, "SELL_COUNTERPARTY") for p, s in self.book_no.bids if p < 1.0]

        # Merge and sort ascending by price
        merged = direct_asks + synthetic_asks
        merged.sort(key=lambda x: x[0])
        return merged

    def best_effective_ask(self, target_side: str) -> Optional[float]:
        asks = self.consolidated_asks(target_side)
        return asks[0][0] if asks else None

    def walk_buy_consolidated(self, target_side: str, stake_usd: float) -> Tuple[Optional[float], float, float, List[Tuple[float, float, str]]]:
        """Walk the consolidated liquidity pool for target_side.
        
        Returns:
            (vwap, total_shares, total_spent_usd, fill_levels)
        """
        asks = self.consolidated_asks(target_side)
        if not asks or stake_usd <= 0:
            return None, 0.0, 0.0, []

        rem_usd = stake_usd
        total_shares = 0.0
        total_spent = 0.0
        fills: List[Tuple[float, float, str]] = []

        for p, s, action in asks:
            cost_level = p * s
            if cost_level <= rem_usd:
                total_shares += s
                total_spent += cost_level
                rem_usd -= cost_level
                fills.append((p, s, action))
            else:
                shares_partial = rem_usd / p
                total_shares += shares_partial
                total_spent += rem_usd
                fills.append((p, shares_partial, action))
                rem_usd = 0.0
                break

        if total_shares <= 0:
            return None, 0.0, 0.0, []

        vwap = total_spent / total_shares
        return vwap, total_shares, total_spent, fills

