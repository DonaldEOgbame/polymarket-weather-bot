"""
quant/risk.py - Institutional Portfolio Risk Manager & Circuit Breaker Daemon.

Manages sub-strategy capital allocations, synoptic regional air-mass covariance,
dynamic Kelly sizing, and multi-tier drawdown circuit breakers.
"""

from typing import Dict, List, Optional
from dataclasses import dataclass
from risk import SYNOPTIC_GROUPS


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str
    adjusted_stake_usd: float = 0.0


class InstitutionalRiskManager:
    """Multi-strategy institutional risk governor."""

    def __init__(
        self,
        max_total_exposure_pct: float = 0.70,  # Max 70% bankroll locked across all positions
        max_synoptic_group_exposure_pct: float = 0.25,  # Max 25% bankroll on one air mass
        max_daily_loss_pct: float = 0.15,  # 15% daily drawdown breaker
        max_portfolio_drawdown_pct: float = 0.20,  # 20% high-water mark breaker
        max_concurrent_positions: int = 12
    ):
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_group_exposure_pct = max_synoptic_group_exposure_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_dd_pct = max_portfolio_drawdown_pct
        self.max_concurrent = max_concurrent_positions

    def evaluate_entry(
        self,
        strategy_name: str,
        city: str,
        target_date: str,
        requested_stake_usd: float,
        bankroll_usd: float,
        locked_cash_usd: float,
        high_water_mark_usd: float,
        daily_realized_pnl_usd: float,
        open_positions: List[Dict]
    ) -> RiskCheckResult:
        """Evaluate whether a trade entry complies with all institutional risk limits."""
        total_equity = bankroll_usd + locked_cash_usd

        # 1. Hard Portfolio Drawdown Circuit Breaker
        if high_water_mark_usd > 0:
            current_dd = (high_water_mark_usd - total_equity) / high_water_mark_usd
            if current_dd >= self.max_dd_pct:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"CRITICAL: Portfolio Drawdown Breaker Fired ({current_dd:.1%} >= {self.max_dd_pct:.1%})"
                )

        # 2. Daily Loss Circuit Breaker
        daily_loss_limit = self.max_daily_loss_pct * total_equity
        if daily_realized_pnl_usd <= -daily_loss_limit:
            return RiskCheckResult(
                allowed=False,
                reason=f"Daily Loss Limit Breached (${daily_realized_pnl_usd:.2f} <= -${daily_loss_limit:.2f})"
            )

        # 3. Concurrent Position Count Limit
        if len(open_positions) >= self.max_concurrent:
            return RiskCheckResult(
                allowed=False,
                reason=f"Max concurrent positions reached ({len(open_positions)} >= {self.max_concurrent})"
            )

        # 4. Total Capital Exposure Cap
        current_exposure = locked_cash_usd
        max_allowed_locked = self.max_total_exposure_pct * total_equity
        if current_exposure + requested_stake_usd > max_allowed_locked:
            available_budget = max_allowed_locked - current_exposure
            if available_budget < 1.0:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Total exposure cap reached (${current_exposure:.2f} + ${requested_stake_usd:.2f} > ${max_allowed_locked:.2f})"
                )
            # Clip stake to fit within remaining budget
            requested_stake_usd = available_budget

        # 5. Synoptic Regional Air Mass Correlation Limit
        group = SYNOPTIC_GROUPS.get(city, "GLOBAL")
        group_exposure = 0.0
        for pos in open_positions:
            pos_city = pos.get("city")
            if pos.get("target_date") == target_date and SYNOPTIC_GROUPS.get(pos_city) == group:
                group_exposure += float(pos.get("size_usdc", 0.0))

        max_group_budget = self.max_group_exposure_pct * total_equity
        if group_exposure + requested_stake_usd > max_group_budget:
            avail_group_budget = max_group_budget - group_exposure
            if avail_group_budget < 1.0:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Synoptic group exposure cap for {group} on {target_date} reached (${group_exposure:.2f} >= ${max_group_budget:.2f})"
                )
            requested_stake_usd = avail_group_budget

        # 6. Duplicate City-Date Check
        for pos in open_positions:
            if pos.get("city") == city and pos.get("target_date") == target_date:
                # Disallow multiple directional positions on the same city/date unless strategy is market making
                if strategy_name != "MARKET_MAKER":
                    return RiskCheckResult(
                        allowed=False,
                        reason=f"Already have open directional position on {city} {target_date}"
                    )

        return RiskCheckResult(
            allowed=True,
            reason="Pass all institutional risk checks",
            adjusted_stake_usd=round(requested_stake_usd, 2)
        )
