import json
import logging
from weather import get_signal_engine, get_bucket_probability
from db import execute_query
from datetime import datetime, timezone
from config import (
    EDGE_THRESHOLD, MIN_MODEL_AGREEMENT, MAX_MODEL_SPREAD,
    KELLY_CAP, HARD_MAX_POSITION_SIZE, MIN_POSITION_SIZE,
    MAX_POSITION_FRACTION, MAX_TOTAL_EXPOSURE_FRACTION, BASE_POSITION_FRACTION,
    SHADOW_MIN_AGREEMENT, SHADOW_MAX_SPREAD, SHADOW_MAX_SIZE_USDC,
    ENABLE_SHADOW_EXPLORATION, PAPER_MODE,
    NARROW_BUCKET_WIDTH_F, NARROW_BUCKET_EDGE_THRESHOLD, NARROW_BUCKET_STD_INFLATION,
    MIN_MODEL_COUNT, CONVECTIVE_STD_INFLATION,
    TAKER_FEE_RATE, SLIPPAGE_FRACTION,
)


def transaction_cost(price):
    """Per-share cost of taking liquidity at `price`: Polymarket's dynamic taker
    fee (feeRate * p * (1-p)) plus a spread/slippage allowance. Returned in price
    units so it can be subtracted directly from per-share edge."""
    fee = TAKER_FEE_RATE * price * (1.0 - price)
    slippage = SLIPPAGE_FRACTION * price
    return fee + slippage

def calculate_kelly(edge, price):
    """Fractional Kelly criterion for binary prediction markets.
    
    For a market paying $1 on YES at cost `price`:
      Full Kelly: f = edge / (1 - price)
      We cap at KELLY_CAP.
    
    Returns the fraction of bankroll to bet.
    """
    if price <= 0 or price >= 1.0:
        return 0.0
    f = edge / (1.0 - price)
    return min(max(0.0, f), KELLY_CAP)

def evaluate_opportunity(opp, portfolio_state, engine_res=None):
    """Evaluate a market opportunity and decide whether to trade.

    Pass engine_res from prefetch_signal_engines() to skip the weather API call.
    If not provided, fetches live (slow — avoid in bulk eval loops).
    """
    if engine_res is None:
        engine_res = get_signal_engine(
            opp.city, opp.date, opp.is_high,
            hours_to_resolution=opp.hours_to_resolution
        )
    if not engine_res:
        return None

    # --- Narrow-bucket std inflation (Fix 3) ---
    # When the bucket is ≤ NARROW_BUCKET_WIDTH_F wide, inflate ensemble_std before
    # computing probabilities. This makes the Gaussian spread wider, reducing
    # overconfident probability estimates on thin exact/range buckets.
    lb = opp.bucket_low
    ub = opp.bucket_high
    if lb is not None and ub is not None:
        bucket_width = abs(ub - lb) if ub != lb else 1.0  # exact buckets treated as 1°F
    elif lb is None or ub is None:
        bucket_width = 999.0  # above/below markets — full open range, no inflation
    else:
        bucket_width = 999.0

    is_narrow = bucket_width <= NARROW_BUCKET_WIDTH_F
    if is_narrow:
        import copy
        engine_res = copy.copy(engine_res)
        engine_res["ensemble_std"] = engine_res["ensemble_std"] * NARROW_BUCKET_STD_INFLATION
        logging.debug(
            f"Narrow-bucket std inflation x{NARROW_BUCKET_STD_INFLATION} "
            f"for {opp.city} (width={bucket_width:.1f}°F)"
        )

    # Use elevated edge threshold for narrow buckets (Fix 1)
    effective_edge_threshold = NARROW_BUCKET_EDGE_THRESHOLD if is_narrow else EDGE_THRESHOLD

    prob = get_bucket_probability(engine_res, opp.bucket_low, opp.bucket_high)

    # Subtract the real per-share transaction cost (Polymarket dynamic taker fee +
    # spread/slippage) from raw edge so the threshold check is on *net* edge after
    # frictions. Cost is priced at the side actually bought.
    yes_edge = (prob - opp.yes_price) - transaction_cost(opp.yes_price)
    no_edge = ((1.0 - prob) - opp.no_price) - transaction_cost(opp.no_price)

    agreement = engine_res["model_agreement"]
    spread = engine_res["model_spread"]
    model_count = engine_res.get("model_count", MIN_MODEL_COUNT)

    signal = None
    kelly = 0.0
    side = None
    target_price = 0.0
    target_token = None
    edge_used = 0.0
    skip_reason = None

    # Evaluate YES side
    if yes_edge >= effective_edge_threshold:
        if agreement < MIN_MODEL_AGREEMENT:
            skip_reason = f"YES edge {yes_edge:.3f} but agreement too low ({agreement:.2f} < {MIN_MODEL_AGREEMENT})"
        elif spread > MAX_MODEL_SPREAD:
            skip_reason = f"YES edge {yes_edge:.3f} but spread too wide ({spread:.1f}°F > {MAX_MODEL_SPREAD}°F)"
        else:
            signal = "BUY_YES"
            side = "YES"
            kelly = calculate_kelly(yes_edge, opp.yes_price)
            target_price = opp.yes_price
            target_token = opp.token_id_yes
            edge_used = yes_edge
    
    # Evaluate NO side (independent check)
    if signal is None and no_edge >= effective_edge_threshold:
        if agreement < MIN_MODEL_AGREEMENT:
            skip_reason = f"NO edge {no_edge:.3f} but agreement too low ({agreement:.2f} < {MIN_MODEL_AGREEMENT})"
        elif spread > MAX_MODEL_SPREAD:
            skip_reason = f"NO edge {no_edge:.3f} but spread too wide ({spread:.1f}°F > {MAX_MODEL_SPREAD}°F)"
        else:
            signal = "BUY_NO"
            side = "NO"
            kelly = calculate_kelly(no_edge, opp.no_price)
            target_price = opp.no_price
            target_token = opp.token_id_no
            edge_used = no_edge

    if not signal and not skip_reason:
        narrow_note = f" [narrow bucket {bucket_width:.1f}°F, threshold={effective_edge_threshold:.0%}]" if is_narrow else ""
        skip_reason = f"Insufficient edge (YES: {yes_edge:.3f}, NO: {no_edge:.3f}, threshold: {effective_edge_threshold}){narrow_note}"

    # --- Shadow diagnostic ---
    # Runs whenever strict evaluation fails and at least one side has edge >= threshold.
    # Logs what would have happened under relaxed agreement/spread limits.
    # If ENABLE_SHADOW_EXPLORATION=true and PAPER_MODE=true, places a tiny exploration trade.
    is_shadow_trade = False
    if not signal:
        shadow_signal_created = False
        for s_edge, s_side, s_price, s_token in [
            (yes_edge, "YES", opp.yes_price, opp.token_id_yes),
            (no_edge, "NO", opp.no_price, opp.token_id_no),
        ]:
            if s_edge < effective_edge_threshold:
                continue

            shadow_agr_ok = agreement >= SHADOW_MIN_AGREEMENT
            shadow_spr_ok = spread < SHADOW_MAX_SPREAD
            shadow_passes = shadow_agr_ok and shadow_spr_ok

            strict_blocks = []
            if agreement < MIN_MODEL_AGREEMENT:
                strict_blocks.append(f"agr({agreement:.2f}<{MIN_MODEL_AGREEMENT})")
            if spread > MAX_MODEL_SPREAD:
                strict_blocks.append(f"spread({spread:.1f}>{MAX_MODEL_SPREAD})")

            shadow_verdict = (
                "ok" if shadow_passes
                else (f"agr_fail({agreement:.2f}<{SHADOW_MIN_AGREEMENT})" if not shadow_agr_ok
                      else f"spread_fail({spread:.1f}>={SHADOW_MAX_SPREAD})")
            )
            logging.info(
                f"SHADOW_{'PASS' if shadow_passes else 'BLOCK'} | "
                f"{opp.city} {opp.date} [{s_side}] | "
                f"edge={s_edge:.3f} agr={agreement:.2f} spread={spread:.1f}°F | "
                f"strict_blocked=[{', '.join(strict_blocks) or 'none'}] | "
                f"shadow={shadow_verdict}"
            )

            if shadow_passes and ENABLE_SHADOW_EXPLORATION and PAPER_MODE and not shadow_signal_created:
                signal = f"EXPLORE_{s_side}"
                side = s_side
                kelly = calculate_kelly(s_edge, s_price)
                target_price = s_price
                target_token = s_token
                edge_used = s_edge
                skip_reason = None
                is_shadow_trade = True
                shadow_signal_created = True

    # Calculate final size incorporating micro-account logic
    final_size = 0.0
    if signal:
        if is_shadow_trade:
            available_cash = portfolio_state.get("available_cash", 0)
            final_size = min(SHADOW_MAX_SIZE_USDC, available_cash)
            if final_size < MIN_POSITION_SIZE:
                signal = None
                skip_reason = f"Shadow exploration size ${final_size:.2f} below minimum ${MIN_POSITION_SIZE}"
        else:
            available_cash = portfolio_state["available_cash"]
            total_equity = portfolio_state["total_equity"]
            locked_cash = portfolio_state["locked_cash"]

            # Base size from edge/kelly (with a floor of BASE_POSITION_FRACTION if Kelly is small)
            fraction_to_use = max(kelly, BASE_POSITION_FRACTION)
            suggested_size = total_equity * fraction_to_use

            # Apply limits: fraction of bankroll and hard dollar cap
            final_size = min(
                suggested_size,
                total_equity * MAX_POSITION_FRACTION,
                HARD_MAX_POSITION_SIZE
            )

            # Enforce minimum position size
            if final_size < MIN_POSITION_SIZE:
                # Micro-account rescue: if calculated size is below minimum, floor to MIN_POSITION_SIZE
                # if we have the cash and it fits within our total exposure limit, preventing sizing deadlock.
                if (MIN_POSITION_SIZE <= available_cash and
                        locked_cash + MIN_POSITION_SIZE <= total_equity * MAX_TOTAL_EXPOSURE_FRACTION):
                    logging.info(
                        f"Micro-account rescue: calculated size ${final_size:.2f} floored "
                        f"to MIN_POSITION_SIZE ${MIN_POSITION_SIZE:.2f} (equity=${total_equity:.2f})"
                    )
                    final_size = MIN_POSITION_SIZE
                else:
                    signal = None
                    skip_reason = f"Calculated size ${final_size:.2f} below minimum ${MIN_POSITION_SIZE}"

            # Enforce maximum total exposure cap across the portfolio
            if signal and locked_cash + final_size > total_equity * MAX_TOTAL_EXPOSURE_FRACTION:
                signal = None
                skip_reason = f"Total exposure cap reached. Locked: ${locked_cash:.2f}, Size: ${final_size:.2f}, Max Allowed: ${total_equity * MAX_TOTAL_EXPOSURE_FRACTION:.2f}"

            # Ensure we actually have the cash available to deploy
            elif signal and available_cash < final_size:
                signal = None
                skip_reason = f"Insufficient available cash (${available_cash:.2f}) for trade size (${final_size:.2f})"

    # Log every evaluation to signals table for research
    timestamp = datetime.now(timezone.utc).isoformat()
    raw_models_json = json.dumps(engine_res.get("raw_models", {}))
    execute_query('''
        INSERT INTO signals (timestamp, market_id, city, target_date, bucket_low, bucket_high,
            model_prob, yes_price, no_price, edge, confidence, model_spread, ensemble_std,
            raw_models, signal_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        timestamp, opp.market_id, opp.city, opp.date, opp.bucket_low, opp.bucket_high,
        prob, opp.yes_price, opp.no_price, edge_used or max(yes_edge, no_edge),
        agreement, spread, engine_res["ensemble_std"], raw_models_json,
        signal or f"SKIP: {skip_reason}"
    ))

    if not signal:
        inflations = []
        if is_narrow:
            inflations.append(f"narrow-bucket x{NARROW_BUCKET_STD_INFLATION}")
        if engine_res.get("convective_inflated"):
            inflations.append(f"convective x{CONVECTIVE_STD_INFLATION}")
        inflation_note = f" [std inflated: {', '.join(inflations)}]" if inflations else ""
        logging.info(f"SKIP {opp.market_id} ({opp.city} {opp.date}): {skip_reason}{inflation_note}")
        return None
        
    return {
        "opp": opp,
        "signal": signal,
        "side": side,
        "token_id": target_token,
        "size_usdc": final_size,
        "price": target_price,
        "model_prob": prob,
        "edge": edge_used
    }
