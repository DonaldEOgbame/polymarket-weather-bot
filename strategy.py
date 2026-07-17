import json
import logging
from weather import get_signal_engine, get_bucket_probability
from scanner import get_realtime_price, get_orderbook_depth_usd, PARSER_VERSION
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
    TAKER_FEE_RATE, SLIPPAGE_FRACTION, MAX_ENTRY_SPREAD_FRACTION,
    FORECAST_MARGIN_F, YES_MARGIN_WIDTH_FRACTION,
)


def transaction_cost(price, spread_fraction=None):
    """Per-share cost of taking liquidity at `price`: Polymarket's dynamic taker
    fee (feeRate * p * (1-p)) plus a spread/slippage allowance. Returned in price
    units so it can be subtracted directly from per-share edge.

    spread_fraction, when known (live half-spread / mid), replaces the flat
    SLIPPAGE_FRACTION guess with the real cost of crossing the book right now."""
    fee = TAKER_FEE_RATE * price * (1.0 - price)
    slippage = (spread_fraction if spread_fraction is not None else SLIPPAGE_FRACTION) * price
    return fee + slippage


def get_live_spread_fraction(token_id):
    """Fetch the live half-spread as a fraction of mid price for a token.
    Returns None if the book can't be read (falls back to SLIPPAGE_FRACTION)."""
    ask, bid = get_realtime_price(token_id)
    if ask <= 0 or bid <= 0:
        return None
    mid = (ask + bid) / 2.0
    if mid <= 0:
        return None
    return ((ask - bid) / 2.0) / mid

def forecast_margin_ok(side, ensemble_mean, bucket_low, bucket_high, margin_f):
    """True if the ensemble mean sits at least `margin_f` °F clear of the bucket in
    the direction the bet needs.

    Only applies to bounded (exact/range) buckets — an open-ended above/below market
    has no near boundary to cut close to. For a NO bet (temp will MISS the bucket) the
    mean must be at least margin_f OUTSIDE the bucket: below (low-0.5)-margin or above
    (high+0.5)+margin. For a YES bet (temp will LAND in the bucket) the mean must be at
    least margin_f INSIDE — i.e. not within margin_f of either edge. margin_f<=0 or an
    open-ended bucket always passes (nothing to gate).

    YES's [lo+margin_f, hi-margin_f] window is empty whenever the padded bucket is
    narrower than 2*margin_f — every real bucket here is 1.8-2.8°F padded-wide vs.
    a 5°F requirement at the default 2.5°F margin, so an unguarded YES check would be
    structurally unsatisfiable regardless of forecast quality (currently moot since
    YES entries are hard-disabled elsewhere, but this keeps the function correct on
    its own). Capping the effective margin at exactly half the padded width fixes the
    "always fails" bug but trades it for an "almost always fails" one: the passing
    window collapses to the bucket's exact midpoint, a single float value real means
    will essentially never land on. Instead cap at YES_MARGIN_WIDTH_FRACTION of the
    padded half-width, leaving a real, non-degenerate window — still tightest at the
    center, just not a single point — so "clear of both edges" stays meaningful on
    narrow buckets instead of silently becoming impossible twice over."""
    if margin_f <= 0 or bucket_low is None or bucket_high is None:
        return True
    lo = bucket_low - 0.5   # same ±0.5 padding get_bucket_probability uses
    hi = bucket_high + 0.5
    if side == "NO":
        return ensemble_mean <= lo - margin_f or ensemble_mean >= hi + margin_f
    # YES: mean must be comfortably inside the bucket, clear of both edges
    half_width = (hi - lo) / 2.0
    effective_margin = min(margin_f, half_width * YES_MARGIN_WIDTH_FRACTION)
    return lo + effective_margin <= ensemble_mean <= hi - effective_margin


def forecast_direction_agrees(side, raw_weighted_mean, bucket_low, bucket_high):
    """True if the model-weighted forecast mean (same per-model weighting as
    engine_res["ensemble_mean"], BEFORE any resolution-source correction like
    METAR_WARM_CORRECTION_F — that shift is folded into ensemble_mean upstream
    in get_signal_engine, not applied here) points the same direction as the
    bet. Hard rule, independent of edge size: a trade must never bet against
    what the models themselves predict, only exploit mispricing on the side
    the models already favor.

    Originated from a real trade 2026-07-10: Helsinki "above 29C", raw models
    averaging ~81F (predicting NOT crossing it — ~2.7F under the threshold),
    but the METAR-warm-corrected calibrated probability still cleared edge on
    YES because the market priced NO even more confidently (87.25%) than the
    raw forecast justified. That bet against the models' own directional call,
    riding entirely on a global (not city-verified) correction in the
    distribution's thin tail. Extended to bounded buckets too, by request —
    NO requires the weighted mean outside the (padded) bucket, YES requires it
    inside, mirroring forecast_margin_ok's direction logic but as a hard
    pass/fail independent of FORECAST_MARGIN_F (so it still applies even if
    that margin is ever set to 0).

    Uses the same per-model weights as the actual trade decision (not a flat
    average across models) — using an unweighted mean here would let this gate
    disagree with, and falsely block, the very edge calculation it's meant to
    police."""
    if raw_weighted_mean is None:
        return True
    raw_mean = raw_weighted_mean

    if bucket_low is not None and bucket_high is not None:
        lo = bucket_low - 0.5
        hi = bucket_high + 0.5
        model_predicts_yes = lo <= raw_mean <= hi  # models predict landing IN the bucket
        return model_predicts_yes if side == "YES" else not model_predicts_yes

    threshold = bucket_low if bucket_low is not None else bucket_high
    if threshold is None:
        return True
    is_above_bucket = bucket_low is not None  # "above X" has only bucket_low set
    if is_above_bucket:
        model_predicts_yes = raw_mean >= threshold
    else:  # "below X" has only bucket_high set
        model_predicts_yes = raw_mean <= threshold
    return model_predicts_yes if side == "YES" else not model_predicts_yes


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

    # Real live spread at evaluation time, replacing the flat SLIPPAGE_FRACTION guess.
    # A wide spread means the cost of actually crossing the book is likely to eat
    # most or all of the modeled edge, so it gates entry outright rather than just
    # being netted out of the edge calculation.
    yes_spread_frac = get_live_spread_fraction(opp.token_id_yes)
    no_spread_frac = get_live_spread_fraction(opp.token_id_no)

    # Subtract the real per-share transaction cost (Polymarket dynamic taker fee +
    # spread/slippage) from raw edge so the threshold check is on *net* edge after
    # frictions. Cost is priced at the side actually bought.
    yes_edge = (prob - opp.yes_price) - transaction_cost(opp.yes_price, yes_spread_frac)
    no_edge = ((1.0 - prob) - opp.no_price) - transaction_cost(opp.no_price, no_spread_frac)

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

    # YES side disabled by request: every real winning trade to date has been NO
    # (NO is structurally favored on bounded weather buckets), and the two YES
    # signals the bot has generated live were both judged bad bets after the fact
    # (Helsinki 2026-07-10, reversed; Shanghai margin-fail cases). Never open YES.
    if yes_edge >= effective_edge_threshold:
        skip_reason = f"YES edge {yes_edge:.3f} but YES entries are disabled"
    
    # Evaluate NO side (independent check)
    if signal is None and no_edge >= effective_edge_threshold:
        if agreement < MIN_MODEL_AGREEMENT:
            skip_reason = f"NO edge {no_edge:.3f} but agreement too low ({agreement:.2f} < {MIN_MODEL_AGREEMENT})"
        elif spread > MAX_MODEL_SPREAD:
            skip_reason = f"NO edge {no_edge:.3f} but spread too wide ({spread:.1f}°F > {MAX_MODEL_SPREAD}°F)"
        elif no_spread_frac is None:
            # Fail CLOSED: an unreadable book (empty/one-sided/error) is most
            # likely exactly the thin market this gate exists to block — skipping
            # the check here let trades fire on Gamma-mid "edge" no real fill
            # could capture.
            skip_reason = f"NO edge {no_edge:.3f} but order-book spread unreadable — cannot verify entry cost"
        elif no_spread_frac > MAX_ENTRY_SPREAD_FRACTION:
            skip_reason = f"NO edge {no_edge:.3f} but market spread too wide ({no_spread_frac:.1%} > {MAX_ENTRY_SPREAD_FRACTION:.0%})"
        elif not forecast_margin_ok("NO", engine_res["ensemble_mean"], opp.bucket_low, opp.bucket_high, FORECAST_MARGIN_F):
            skip_reason = f"NO edge {no_edge:.3f} but forecast too close to bucket edge (mean {engine_res['ensemble_mean']:.1f}°F, need ≥{FORECAST_MARGIN_F}°F clear of bucket)"
        elif not forecast_direction_agrees("NO", engine_res.get("raw_weighted_mean"), opp.bucket_low, opp.bucket_high):
            skip_reason = f"NO edge {no_edge:.3f} but raw model forecast points the other way (bet requires models to predict missing the bucket, before resolution-source correction)"
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
    # Spread for whichever side drove the decision (the traded side, or the
    # higher-edge side if skipped) — lets calibrate.py separate real edge from
    # edge that was actually just spread cost.
    logged_spread_frac = (
        (yes_spread_frac if side == "YES" else no_spread_frac) if side
        else (yes_spread_frac if yes_edge >= no_edge else no_spread_frac)
    )
    # Order-book $ depth on the traded side, ONLY fetched when a trade actually
    # fires (not on every skip — that's thousands of extra CLOB calls per scan
    # for data that's never used). Answers "how big a position could this book
    # have actually absorbed at entry" after the fact — the live book moves on
    # or the market resolves within days, so this can't be reconstructed later
    # from Polymarket's API, only captured at the moment it happened.
    ask_depth_usd = bid_depth_usd = None
    if signal:
        ask_depth_usd, bid_depth_usd = get_orderbook_depth_usd(target_token)

    execute_query('''
        INSERT INTO signals (timestamp, market_id, city, target_date, bucket_low, bucket_high,
            model_prob, yes_price, no_price, edge, confidence, model_spread, ensemble_std,
            raw_models, signal_type, market_spread_frac, parser_version,
            ask_depth_usd, bid_depth_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        timestamp, opp.market_id, opp.city, opp.date, opp.bucket_low, opp.bucket_high,
        prob, opp.yes_price, opp.no_price, edge_used or max(yes_edge, no_edge),
        agreement, spread, engine_res["ensemble_std"], raw_models_json,
        signal or f"SKIP: {skip_reason}", logged_spread_frac, PARSER_VERSION,
        ask_depth_usd, bid_depth_usd
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
        "edge": edge_used,
        "model_count": model_count
    }
