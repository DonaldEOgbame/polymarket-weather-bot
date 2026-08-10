import json
import logging
from weather import (get_signal_engine, get_bucket_probability,
                     bucket_probability_stages)
from scanner import (get_realtime_price, get_orderbook_depth_usd, estimate_fill,
                     PARSER_VERSION)
from risk import risk_direction
from db import execute_query
from datetime import datetime, timezone
from config import (
    EDGE_THRESHOLD, MIN_MODEL_AGREEMENT, MAX_MODEL_SPREAD_STD,
    KELLY_CAP, MIN_POSITION_SIZE,
    MAX_POSITION_FRACTION, BASE_POSITION_FRACTION, HARD_MAX_POSITION_SIZE,
    SHADOW_MIN_AGREEMENT, SHADOW_MAX_SPREAD_STD, SHADOW_MAX_SIZE_USDC,
    ENABLE_SHADOW_EXPLORATION, paper_mode,
    NARROW_BUCKET_WIDTH_F, NARROW_BUCKET_EDGE_THRESHOLD, NARROW_BUCKET_STD_INFLATION,
    MAX_SIGMA_F,
    MIN_MODEL_COUNT, CONVECTIVE_STD_INFLATION,
    TAKER_FEE_RATE, SLIPPAGE_FRACTION, MAX_ENTRY_SPREAD_FRACTION,
    FORECAST_MARGIN_F, YES_MARGIN_WIDTH_FRACTION, MAX_ENTRY_PRICE,
    MIN_DEPTH_MULTIPLE, REQUIRE_DEPTH_TO_TRADE,
    MIN_MODEL_CONFIDENCE, MAX_MODEL_CONFIDENCE, MIN_ENTRY_PRICE, MAX_HOURS_TO_RESOLUTION,
    ARMED_REENTRY_ENABLED, ARMED_SIGNAL_TTL_HOURS,
    setting,
)
# FIXED_POSITION_SIZE / MAX_TOTAL_EXPOSURE_FRACTION are
# deliberately NOT imported as constants: they are dashboard-tunable at runtime
# and read via config.setting() at the moment of each sizing decision, so a
# settings change applies to the next trade with no restart.


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


def _log_replay_row(*, timestamp, opp, engine_res, prob_stages, sigma_final,
                    bucket_width, is_narrow, no_edge, yes_edge, prob,
                    edge_threshold, gates, no_spread_frac, decision, skip_reason,
                    ask_depth_usd, bid_depth_usd,
                    usable_depth_usd=None, stake_usd=None, walked_vwap=None):
    """Write the replay row for one evaluated opportunity.

    Records INPUTS, not conclusions, so any future configuration can be scored
    against this market offline. There is deliberately no second evaluation in
    production: dual-config scoring doubles the compute and the failure surface
    and still only ever compares two configurations.

    Best-effort, and the try/except is load-bearing rather than defensive:
    log_replay_signal swallows its own DB errors, but everything BEFORE it —
    the imports, the JSON encoding, reading engine keys that an older cached
    engine_res may not carry — runs on the trading path. Without this, a replay
    schema change could stop the bot placing trades. Research telemetry must
    never be able to do that."""
    try:
        _log_replay_row_inner(
            timestamp=timestamp, opp=opp, engine_res=engine_res,
            prob_stages=prob_stages, sigma_final=sigma_final,
            bucket_width=bucket_width, is_narrow=is_narrow, no_edge=no_edge,
            yes_edge=yes_edge, prob=prob, edge_threshold=edge_threshold,
            gates=gates, no_spread_frac=no_spread_frac, decision=decision,
            skip_reason=skip_reason, ask_depth_usd=ask_depth_usd,
            bid_depth_usd=bid_depth_usd,
            usable_depth_usd=usable_depth_usd, stake_usd=stake_usd,
            walked_vwap=walked_vwap,
        )
    except Exception as e:
        logging.error(f"replay row build failed for {opp.market_id}: {e}", exc_info=True)


def _log_replay_row_inner(*, timestamp, opp, engine_res, prob_stages, sigma_final,
                          bucket_width, is_narrow, no_edge, yes_edge, prob,
                          edge_threshold, gates, no_spread_frac, decision,
                          skip_reason, ask_depth_usd, bid_depth_usd,
                          usable_depth_usd=None, stake_usd=None, walked_vwap=None):
    from db import log_replay_signal
    from config import config_fingerprint, REPLAY_SCHEMA_VERSION, paper_mode
    from metar import STATION_ICAO

    ss = engine_res.get("sigma_stages") or {}
    city_key = engine_res.get("city_key")
    lo, hi = opp.bucket_low, opp.bucket_high
    bucket_type = ("range" if lo is not None and hi is not None and lo != hi
                   else "exact" if lo is not None and hi is not None
                   else "above" if lo is not None
                   else "below" if hi is not None else "unbounded")
    icao = (STATION_ICAO.get(city_key) or (None, None))[0]

    row = {
        "timestamp": timestamp,
        "schema_version": REPLAY_SCHEMA_VERSION,
        "config_fingerprint": config_fingerprint(),
        "mode": "paper" if paper_mode() else "live",
        "market_id": opp.market_id, "city": opp.city, "city_key": city_key,
        "station_icao": icao, "region": engine_res.get("region"),
        "target_date": opp.date, "is_high": int(bool(opp.is_high)),
        "lead_hours": opp.hours_to_resolution,
        "bucket_low": lo, "bucket_high": hi, "bucket_type": bucket_type,
        "bucket_width": bucket_width, "is_narrow": int(bool(is_narrow)),
        "raw_models_pre_correction": json.dumps(
            engine_res.get("raw_models_pre_correction") or {}),
        "corrections_applied": json.dumps(engine_res.get("corrections_applied") or {}),
        "model_weights": json.dumps(engine_res.get("model_weights") or {}),
        # Open-Meteo's daily endpoint does not return per-model run init times in
        # the shape we request, so this is NULL until the request adds them.
        # The column exists now because backfilling a column is cheap and
        # backfilling the DATA it would have held is impossible.
        "model_run_init": None,
        "model_count": engine_res.get("model_count"),
        "weighted_spread_sd": engine_res.get("model_spread_std"),
        "unweighted_range": engine_res.get("model_spread_range"),
        "model_agreement": engine_res.get("model_agreement"),
        "raw_weighted_mean": engine_res.get("raw_weighted_mean"),
        "ensemble_mean": engine_res.get("ensemble_mean"),
        "yes_price": opp.yes_price, "no_price": opp.no_price,
        "volume": getattr(opp, "volume", None),
        "spread_fraction": no_spread_frac,
        "ask_depth_usd": ask_depth_usd, "bid_depth_usd": bid_depth_usd,
        # The depth the entry decision was actually made against, and the stake
        # it was compared to. Both are needed: the requirement is a MULTIPLE of
        # the stake, so depth alone cannot reconstruct the gate.
        "usable_depth_usd": usable_depth_usd,
        "stake_usd": stake_usd,
        "walked_vwap": walked_vwap,
        "sigma_base": ss.get("base"),
        "sigma_post_spread": ss.get("post_spread"),
        "sigma_post_direction": ss.get("post_direction"),
        "sigma_post_convective": ss.get("post_convective"),
        "sigma_post_clamp": ss.get("post_clamp"),
        "sigma_post_narrow": sigma_final,
        "sigma_final": sigma_final,
        "prob_raw": prob_stages.get("raw"),
        "prob_post_platt": prob_stages.get("post_platt"),
        "prob_post_floor": prob_stages.get("post_floor"),
        "edge_raw": (1.0 - prob) - opp.no_price,
        "edge_post_fee": no_edge,
        "edge_threshold": edge_threshold,
        "side_evaluated": "NO",
        "decision": decision or "SKIP",
        "skip_reason": skip_reason,
    }

    # No independent_* columns since REPLAY_SCHEMA_VERSION 3: the veto feature
    # was removed 2026-08-08. The columns stay in the table for the v2 history;
    # NULL there now means "feature absent", exactly as it does on v1 rows.
    log_replay_signal(row, gates)


def _depth_gate(usable_depth_usd, stake):
    """The book-depth gate row.

    Refuses when the usable ask depth is less than MIN_DEPTH_MULTIPLE times the
    stake. Both numbers are recorded so the row answers "how thin was it, and
    against what stake" without a second query — the requirement scales with
    `effective_stake()`, so the same market can pass at $2 and fail at $6. The
    $2 -> $6 change is exactly what made the 2026-08-06 Austin fill reachable,
    which is why this is a multiple and not a dollar figure.

    Unknown depth REFUSES when REQUIRE_DEPTH_TO_TRADE: an entry that cannot
    see the book it is about to cross has no idea what it will pay."""
    required = MIN_DEPTH_MULTIPLE * stake
    if usable_depth_usd is None:
        return {
            "gate": "book_depth", "observed": None, "threshold": required,
            "passed": not REQUIRE_DEPTH_TO_TRADE,
            "detail": (f"order-book depth unreadable — cannot verify that "
                       f"${stake:.2f} can fill at or below {MAX_ENTRY_PRICE:.2f} "
                       f"without walking the book"),
        }
    return {
        "gate": "book_depth", "observed": usable_depth_usd, "threshold": required,
        "passed": usable_depth_usd >= required,
        "detail": (f"only ${usable_depth_usd:.2f} resting at or below "
                   f"{MAX_ENTRY_PRICE:.2f}, need ${required:.2f} "
                   f"({MIN_DEPTH_MULTIPLE:g}x the ${stake:.2f} stake) — a taker "
                   f"order this size would walk the book"),
    }


def _no_side_gates(opp, engine_res, no_edge, edge_threshold, agreement, spread,
                   no_spread_frac, usable_depth_usd=None, stake=None,
                   prob=0.0, walked_vwap=None):
    """Every NO-side gate as a structured record, in decision order.

    Returns [{gate, observed, threshold, passed, detail}, ...]. `detail` is the
    human-readable skip reason, so the decision path and the replay log share
    one wording as well as one condition.

    Order matters and matches the original if/elif chain exactly: the first
    failure is the one reported, and reordering would change which reason a
    multiply-blocked market shows.

    Every gate is evaluated regardless of whether an earlier one already failed.
    That costs a few comparisons and buys the counterfactual: "how often would
    this gate have bound if the one before it hadn't?" is the question the
    shadow run exists to answer, and short-circuit evaluation destroys it."""
    mean = engine_res["ensemble_mean"]
    lo, hi = opp.bucket_low, opp.bucket_high
    price = opp.no_price
    fill = walked_vwap if walked_vwap is not None else price
    p_side = min(1.0 - prob, MAX_MODEL_CONFIDENCE)
    payoff = ((1.0 - fill) / fill) if fill and fill > 0 else float("inf")

    return [
        {"gate": "edge_threshold", "observed": no_edge, "threshold": edge_threshold,
         "passed": no_edge >= edge_threshold,
         "detail": f"Insufficient NO edge ({no_edge:.3f} < {edge_threshold})"},
        {"gate": "model_agreement", "observed": agreement, "threshold": MIN_MODEL_AGREEMENT,
         "passed": agreement >= MIN_MODEL_AGREEMENT,
         "detail": f"NO edge {no_edge:.3f} but agreement too low "
                   f"({agreement:.2f} < {MIN_MODEL_AGREEMENT})"},
        {"gate": "model_spread_sd", "observed": spread, "threshold": MAX_MODEL_SPREAD_STD,
         "passed": spread <= MAX_MODEL_SPREAD_STD,
         "detail": f"NO edge {no_edge:.3f} but spread too wide "
                   f"({spread:.2f}°F sd > {MAX_MODEL_SPREAD_STD}°F sd)"},
        {"gate": "model_confidence", "observed": p_side, "threshold": MIN_MODEL_CONFIDENCE,
         "passed": p_side > MIN_MODEL_CONFIDENCE,
         "detail": f"NO edge {no_edge:.3f} but model confidence too low "
                   f"({p_side:.3f} <= {MIN_MODEL_CONFIDENCE:.2f})"},
        {"gate": "max_model_confidence", "observed": p_side, "threshold": MAX_MODEL_CONFIDENCE,
         "passed": p_side <= MAX_MODEL_CONFIDENCE,
         "detail": f"NO edge {no_edge:.3f} but model confidence too high "
                   f"({p_side:.3f} > {MAX_MODEL_CONFIDENCE:.2f})"},
        {"gate": "time_to_resolution", "observed": opp.hours_to_resolution,
         "threshold": MAX_HOURS_TO_RESOLUTION,
         "passed": opp.hours_to_resolution < MAX_HOURS_TO_RESOLUTION,
         "detail": f"NO edge {no_edge:.3f} but time to resolution too long "
                   f"({opp.hours_to_resolution:.1f}h >= {MAX_HOURS_TO_RESOLUTION:.0f}h)"},
        # Liquidity, before anything about price. A book that cannot absorb the
        # stake at an acceptable price makes every downstream number — the edge,
        # the entry price, the payoff ratio — a statement about a fill that will
        # not happen.
        _depth_gate(usable_depth_usd, stake if stake is not None else 0.0),
        # Fail CLOSED on an unreadable book: empty/one-sided/error is most likely
        # exactly the thin market this gate exists to block. Observed is None
        # rather than 0.0 so a replay can tell "unreadable" from "zero spread".
        {"gate": "book_readable", "observed": no_spread_frac, "threshold": None,
         "passed": no_spread_frac is not None,
         "detail": f"NO edge {no_edge:.3f} but order-book spread unreadable — "
                   f"cannot verify entry cost"},
        {"gate": "market_spread_frac", "observed": no_spread_frac,
         "threshold": MAX_ENTRY_SPREAD_FRACTION,
         "passed": no_spread_frac is not None and no_spread_frac <= MAX_ENTRY_SPREAD_FRACTION,
         "detail": f"NO edge {no_edge:.3f} but market spread too wide "
                   f"({(no_spread_frac or 0):.1%} > {MAX_ENTRY_SPREAD_FRACTION:.0%})"},
        {"gate": "min_entry_price", "observed": fill, "threshold": MIN_ENTRY_PRICE,
         "passed": fill >= MIN_ENTRY_PRICE,
         "detail": f"NO edge {no_edge:.3f} but entry fill price too low "
                   f"({fill:.3f} < {MIN_ENTRY_PRICE:.2f})"},
        {"gate": "max_entry_price", "observed": fill, "threshold": MAX_ENTRY_PRICE,
         "passed": fill <= MAX_ENTRY_PRICE,
         "detail": f"NO edge {no_edge:.3f} but entry fill price too high "
                   f"({fill:.3f} > {MAX_ENTRY_PRICE:.2f}): risks $1.00 to win "
                   f"${payoff:.2f}"},
        {"gate": "forecast_margin", "observed": mean, "threshold": FORECAST_MARGIN_F,
         "passed": forecast_margin_ok("NO", mean, lo, hi, FORECAST_MARGIN_F),
         "detail": f"NO edge {no_edge:.3f} but forecast too close to bucket edge "
                   f"(mean {mean:.1f}°F, need ≥{FORECAST_MARGIN_F}°F clear of bucket)"},
        {"gate": "forecast_direction", "observed": engine_res.get("raw_weighted_mean"),
         "threshold": None,
         "passed": forecast_direction_agrees(
             "NO", engine_res.get("raw_weighted_mean"), lo, hi),
         "detail": f"NO edge {no_edge:.3f} but raw model forecast points the other way "
                   f"(bet requires models to predict missing the bucket, before "
                   f"resolution-source correction)"},
    ]


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
        # Re-clamp to MAX_SIGMA_F AFTER inflating. compute_sigma applies the
        # ceiling, then this multiplies by 1.4 on top of it, so before 2026-07-31
        # the sigma actually used to price a bucket could reach MAX_SIGMA_F * 1.4
        # = 11.2°F — a ceiling the constant's name promises and did not deliver.
        # Clamping last makes MAX_SIGMA_F mean what it says. It is also the
        # conservative direction: a smaller sigma makes a narrow bucket look MORE
        # likely, which lowers the NO edge, so this can only remove trades.
        engine_res["ensemble_std"] = min(
            engine_res["ensemble_std"] * NARROW_BUCKET_STD_INFLATION, MAX_SIGMA_F
        )
        logging.debug(
            f"Narrow-bucket std inflation x{NARROW_BUCKET_STD_INFLATION} "
            f"for {opp.city} (width={bucket_width:.1f}°F)"
        )

    # Use elevated edge threshold for narrow buckets (Fix 1)
    effective_edge_threshold = NARROW_BUCKET_EDGE_THRESHOLD if is_narrow else EDGE_THRESHOLD

    # Stages, then the value. bucket_probability_stages and
    # get_bucket_probability share an implementation, so `prob` below is
    # exactly prob_stages["post_floor"] — the log cannot record a different
    # number from the one that was traded on.
    prob_stages = bucket_probability_stages(engine_res, opp.bucket_low, opp.bucket_high)
    sigma_final = engine_res["ensemble_std"]
    prob = get_bucket_probability(engine_res, opp.bucket_low, opp.bucket_high)

    # Real live spread at evaluation time, replacing the flat SLIPPAGE_FRACTION guess.
    # A wide spread means the cost of actually crossing the book is likely to eat
    # most or all of the modeled edge, so it gates entry outright rather than just
    # being netted out of the edge calculation.
    yes_spread_frac = get_live_spread_fraction(opp.token_id_yes)
    no_spread_frac = get_live_spread_fraction(opp.token_id_no)

    # What the intended stake would ACTUALLY fill at, by walking the real book.
    #
    # Fetched HERE, before the decision, which is the whole fix. Depth used to be
    # read three hundred lines below inside `if signal:` — collected purely for
    # logging, one line after the commitment it could have prevented. On
    # 2026-08-06 that ordering let a $6 order into $26.49 of ask depth fill at
    # 0.9818 against a 0.64 quote.
    #
    # Only levels at or below MAX_ENTRY_PRICE count: depth resting at 0.95 is not
    # depth you can use when your cap is 0.80, it is what a taker walks into
    # after exhausting everything cheaper.
    intended_stake = setting("FIXED_POSITION_SIZE") or MIN_POSITION_SIZE
    fill_est = estimate_fill(opp.token_id_no, intended_stake, MAX_ENTRY_PRICE)
    usable_depth = fill_est["usable_depth_usd"] if fill_est else None
    walked_vwap = fill_est["vwap"] if fill_est else None

    # Subtract the real per-share transaction cost (Polymarket dynamic taker fee +
    # spread/slippage) from raw edge so the threshold check is on *net* edge after
    # frictions. Cost is priced at the side actually bought.
    #
    # The NO side prices slippage from the WALKED book when it is readable.
    # `spread_fraction * price` describes crossing the spread — a single-level
    # move — and silently assumes the size fits in the top level. When it does
    # not, the order pays every level it eats: modelled 0.085 against an actual
    # 0.34 on the Austin book, a 4x understatement.
    yes_edge = (prob - opp.yes_price) - transaction_cost(opp.yes_price, yes_spread_frac)
    no_slip_frac = no_spread_frac
    if walked_vwap is not None and opp.no_price > 0:
        # Realised slippage as a fraction of the quote, so transaction_cost's
        # `fraction * price` shape is preserved and the fee term is untouched.
        no_slip_frac = max((walked_vwap - opp.no_price) / opp.no_price, 0.0)
    p_side = min(1.0 - prob, MAX_MODEL_CONFIDENCE)
    no_edge = (p_side - opp.no_price) - transaction_cost(opp.no_price, no_slip_frac)

    agreement = engine_res["model_agreement"]
    # Weighted stdev since 2026-07-31, not max-min — see MAX_MODEL_SPREAD_STD.
    spread = engine_res["model_spread"]
    model_count = engine_res.get("model_count", MIN_MODEL_COUNT)

    # --- Armed re-entry waiver (2026-08-08) ---
    # If this market previously passed every gate except the entry-price floor
    # (see the arming block after the decision below), the narrow-bucket edge
    # surcharge is waived down to the base EDGE_THRESHOLD while the arm lives:
    # the market rising toward the model's side is confirmation, which is
    # evidence against the overconfidence the surcharge hedges. The waiver
    # floor is the base threshold — never below it — because a market that
    # cannot clear the bar every ordinary trade clears has no current edge,
    # only a stale one, and entering on a remembered edge is chasing.
    #
    # The arm is revoked PERMANENTLY the moment model confidence falls to
    # MIN_MODEL_CONFIDENCE: "price reached the floor" cannot distinguish
    # market-confirmed-the-model from model-quietly-gave-up, and the edge
    # check plus this revocation are what tell them apart. Fails OPEN (no
    # waiver, behaviour identical to unarmed) on any DB error — the arm store
    # must only ever be able to add back trades the floor removed.
    armed = None
    if ARMED_REENTRY_ENABLED:
        try:
            from db import get_active_arm, resolve_arm
            armed = get_active_arm(opp.market_id)
            if armed and p_side <= MIN_MODEL_CONFIDENCE:
                resolve_arm(
                    opp.market_id, "expired",
                    f"model confidence {p_side:.3f} fell to the "
                    f"{MIN_MODEL_CONFIDENCE:.2f} floor")
                armed = None
        except Exception as e:
            logging.error(f"armed-signal lookup failed for {opp.market_id}: {e}")
            armed = None
    if armed and is_narrow and effective_edge_threshold > EDGE_THRESHOLD:
        effective_edge_threshold = EDGE_THRESHOLD
        logging.info(
            f"ARMED_WAIVER | {opp.city} {opp.date} | narrow-bucket threshold "
            f"{NARROW_BUCKET_EDGE_THRESHOLD} waived to base {EDGE_THRESHOLD} "
            f"(armed {armed['armed_at']} at fill {armed['arm_fill']:.3f}, "
            f"edge then {armed['arm_edge']:.3f})")

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
    
    # Every NO-side gate, evaluated independently and in decision order. The
    # decision below consumes this list rather than re-testing the conditions,
    # so what gets LOGGED and what actually gated the trade cannot diverge —
    # which is what made "which gate cut this trade?" unanswerable from the old
    # free-text skip_reason during the 2026-07-31 reconciliation.
    no_gates = _no_side_gates(opp, engine_res, no_edge, effective_edge_threshold,
                              agreement, spread, no_spread_frac,
                              usable_depth_usd=usable_depth, stake=intended_stake,
                              prob=prob, walked_vwap=walked_vwap)

    # Evaluate NO side (independent check)
    if signal is None and no_edge >= effective_edge_threshold:
        failed = next((g for g in no_gates if not g["passed"]
                       and g["gate"] != "edge_threshold"), None)
        if failed is not None:
            skip_reason = failed["detail"]
        else:
            signal = "BUY_NO"
            side = "NO"
            kelly = calculate_kelly(no_edge, opp.no_price)
            target_price = opp.no_price
            target_token = opp.token_id_no
            edge_used = no_edge

    # Full Armed Re-entry Waiver:
    # When a market was previously qualified & ARMED (having passed 100% of all quality,
    # agreement, and spread gates at low price), its price rising to MIN_ENTRY_PRICE is market
    # confirmation of the model. Edge naturally decays as price moves up, so we waive
    # the edge threshold check and execute BUY_NO as long as price is within [MIN_ENTRY_PRICE, MAX_ENTRY_PRICE]
    # and model confidence remains valid.
    fill_price = walked_vwap if walked_vwap is not None else opp.no_price
    if armed and signal is None and fill_price >= MIN_ENTRY_PRICE and fill_price <= MAX_ENTRY_PRICE and p_side > MIN_MODEL_CONFIDENCE:
        depth_ok = usable_depth is None or usable_depth >= (intended_stake * MIN_DEPTH_MULTIPLE)
        time_ok = opp.hours_to_resolution < MAX_HOURS_TO_RESOLUTION
        if depth_ok and time_ok:
            signal = "BUY_NO"
            side = "NO"
            kelly = calculate_kelly(max(no_edge, EDGE_THRESHOLD), opp.no_price)
            target_price = opp.no_price
            target_token = opp.token_id_no
            edge_used = max(no_edge, EDGE_THRESHOLD)
            skip_reason = None
            logging.info(
                f"ARMED_FULL_WAIVER_EXECUTION | {opp.city} {opp.date} | "
                f"Armed signal executed on price floor reach ({fill_price:.3f} >= {MIN_ENTRY_PRICE:.2f}) "
                f"waiving decaying edge check (armed at {armed['arm_fill']:.3f} with edge {armed['arm_edge']:.3f})")

    # Arm (or refresh) when the price floor is the ONLY failing gate: every
    # other gate — edge at the FULL surcharged threshold, agreement, spread,
    # confidence, time, liquidity — passed, so the arm records
    # "qualified, waiting on market confirmation". Checking the full gate list
    # rather than the reported skip_reason is what makes this exact: the first
    # failure is what gets reported, but a sole-failure claim needs all of them.
    if ARMED_REENTRY_ENABLED and signal is None:
        gate_fails = [g for g in no_gates if not g["passed"]]
        floor_fail = next((g for g in gate_fails
                           if g["gate"] == "min_entry_price"), None)
        # The floor is the REPORTED reason (first in decision order) while
        # later gates also failed: say why this didn't arm, or the dashboard
        # shows two identical-looking floor skips of which only one armed.
        if (floor_fail is not None and len(gate_fails) > 1
                and skip_reason == floor_fail["detail"]):
            others = ", ".join(g["gate"] for g in gate_fails
                               if g["gate"] != "min_entry_price")
            skip_reason = f"{skip_reason} — not armed (also failed: {others})"
        if len(gate_fails) == 1 and gate_fails[0]["gate"] == "min_entry_price":
            try:
                from db import arm_signal
                arm_fill = walked_vwap if walked_vwap is not None else opp.no_price
                arm_signal(opp.market_id, opp.city, opp.date,
                           opp.bucket_low, opp.bucket_high,
                           arm_fill, no_edge, p_side, effective_edge_threshold,
                           ARMED_SIGNAL_TTL_HOURS)
                waiver_note = (" (narrow-bucket surcharge waived)" if is_narrow
                               else "")
                skip_reason = (
                    f"{gate_fails[0]['detail']} — armed {ARMED_SIGNAL_TTL_HOURS:.0f}h: "
                    f"enters if fill reaches {MIN_ENTRY_PRICE:.2f} with edge >= "
                    f"{EDGE_THRESHOLD}{waiver_note}")
            except Exception as e:
                logging.error(f"arming failed for {opp.market_id}: {e}")

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

            # The payoff-asymmetry cap applies to exploration trades too — this path
            # builds a signal directly and would otherwise bypass the strict gate chain.
            if s_price >= MAX_ENTRY_PRICE:
                logging.info(
                    f"SHADOW_BLOCK | {opp.city} {opp.date} [{s_side}] | "
                    f"entry price {s_price:.3f} >= MAX_ENTRY_PRICE {MAX_ENTRY_PRICE:.2f}"
                )
                continue

            shadow_agr_ok = agreement >= SHADOW_MIN_AGREEMENT
            shadow_spr_ok = spread < SHADOW_MAX_SPREAD_STD
            shadow_passes = shadow_agr_ok and shadow_spr_ok

            strict_blocks = []
            if agreement < MIN_MODEL_AGREEMENT:
                strict_blocks.append(f"agr({agreement:.2f}<{MIN_MODEL_AGREEMENT})")
            if spread > MAX_MODEL_SPREAD_STD:
                strict_blocks.append(f"spread_sd({spread:.2f}>{MAX_MODEL_SPREAD_STD})")

            shadow_verdict = (
                "ok" if shadow_passes
                else (f"agr_fail({agreement:.2f}<{SHADOW_MIN_AGREEMENT})" if not shadow_agr_ok
                      else f"spread_fail_sd({spread:.2f}>={SHADOW_MAX_SPREAD_STD})")
            )
            logging.info(
                f"SHADOW_{'PASS' if shadow_passes else 'BLOCK'} | "
                f"{opp.city} {opp.date} [{s_side}] | "
                f"edge={s_edge:.3f} agr={agreement:.2f} spread={spread:.2f}°F sd | "
                f"strict_blocked=[{', '.join(strict_blocks) or 'none'}] | "
                f"shadow={shadow_verdict}"
            )

            if shadow_passes and ENABLE_SHADOW_EXPLORATION and paper_mode() and not shadow_signal_created:
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

            # Snapshot the runtime-tunable knobs ONCE for this decision, so a
            # dashboard save landing mid-evaluation cannot mix old and new
            # values inside a single sizing computation.
            fixed_stake = setting("FIXED_POSITION_SIZE")
            exposure_fraction = setting("MAX_TOTAL_EXPOSURE_FRACTION")

            if fixed_stake > 0:
                # Flat-stake mode: every trade is the same size or it does not
                # happen. The stake is the ONLY per-trade size authority — there
                # is deliberately no second knob that can silently shrink it.
                # Portfolio-level risk is bounded by MAX_CONCURRENT_POSITIONS,
                # MAX_TOTAL_EXPOSURE_FRACTION and the daily loss budget below.
                final_size = fixed_stake
                # Strict: no partial fills of the stake. Shrinking to fit available
                # cash would reintroduce uneven sizes, which is what flat staking
                # exists to avoid — so an underfunded signal is skipped instead.
                if available_cash < final_size:
                    signal = None
                    skip_reason = (
                        f"Flat stake ${final_size:.2f} exceeds available cash "
                        f"${available_cash:.2f} — skipping (no partial stakes)"
                    )
            else:
                # Base size from edge/kelly (with a floor of BASE_POSITION_FRACTION if Kelly is small)
                fraction_to_use = max(kelly, BASE_POSITION_FRACTION)
                suggested_size = total_equity * fraction_to_use

                # Apply limits: fraction of bankroll and hard dollar cap
                # HARD_MAX_POSITION_SIZE is the dollar cap on THIS path only.
                # It is a code/env constant rather than a dashboard setting: in
                # flat-stake mode nothing reads it.
                final_size = min(
                    suggested_size,
                    total_equity * MAX_POSITION_FRACTION,
                    HARD_MAX_POSITION_SIZE
                )

            # Enforce minimum position size. The micro-account rescue below is a
            # Kelly-path concession (it would silently override a flat stake set
            # below MIN_POSITION_SIZE), so in flat-stake mode just skip instead.
            if signal and final_size < MIN_POSITION_SIZE and fixed_stake > 0:
                signal = None
                skip_reason = (
                    f"Flat stake ${final_size:.2f} is below the ${MIN_POSITION_SIZE:.2f} "
                    f"CLOB minimum — raise FIXED_POSITION_SIZE"
                )
            elif signal and final_size < MIN_POSITION_SIZE:
                # Micro-account rescue: if calculated size is below minimum, floor to MIN_POSITION_SIZE
                # if we have the cash and it fits within our total exposure limit, preventing sizing deadlock.
                if (MIN_POSITION_SIZE <= available_cash and
                        locked_cash + MIN_POSITION_SIZE <= total_equity * exposure_fraction):
                    logging.info(
                        f"Micro-account rescue: calculated size ${final_size:.2f} floored "
                        f"to MIN_POSITION_SIZE ${MIN_POSITION_SIZE:.2f} (equity=${total_equity:.2f})"
                    )
                    final_size = MIN_POSITION_SIZE
                else:
                    signal = None
                    skip_reason = f"Calculated size ${final_size:.2f} below minimum ${MIN_POSITION_SIZE}"

            # Enforce maximum total exposure cap across the portfolio
            if signal and locked_cash + final_size > total_equity * exposure_fraction:
                signal = None
                skip_reason = f"Total exposure cap reached. Locked: ${locked_cash:.2f}, Size: ${final_size:.2f}, Max Allowed: ${total_equity * exposure_fraction:.2f}"

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

    _log_replay_row(
        timestamp=timestamp, opp=opp, engine_res=engine_res,
        prob_stages=prob_stages, sigma_final=sigma_final,
        bucket_width=bucket_width, is_narrow=is_narrow,
        no_edge=no_edge, yes_edge=yes_edge, prob=prob,
        edge_threshold=effective_edge_threshold, gates=no_gates,
        no_spread_frac=no_spread_frac, decision=signal, skip_reason=skip_reason,
        ask_depth_usd=ask_depth_usd, bid_depth_usd=bid_depth_usd,
        usable_depth_usd=usable_depth, stake_usd=intended_stake,
        walked_vwap=walked_vwap,
    )

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
        # The walked ask VWAP the gates approved. The executor MUST price its
        # limit from this, not from `price` (the mid): every gate — the entry
        # floor, the cap, the netted edge — was evaluated at this fill, and a
        # limit derived from the mid refuses the very book the gates accepted.
        "walked_vwap": walked_vwap,
        "model_prob": prob,
        "edge": edge_used,
        "model_count": model_count,
        # Which sign of temperature surprise would lose this position money.
        # Computed HERE because it needs the ensemble mean that priced the
        # bucket — by the time the executor consults the correlated-exposure
        # caps for the NEXT trade, this forecast has already moved on. See
        # risk.risk_direction.
        "risk_direction": risk_direction(
            side, opp.bucket_low, opp.bucket_high,
            engine_res.get("raw_weighted_mean")),
    }
