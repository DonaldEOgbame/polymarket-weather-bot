"""Offline replay of logged signals under ANY configuration.

The point of the replay log is that a config question never requires a deploy.
Every row in `replay_signals` carries the raw pre-correction model temperatures
and the correction that was applied to each, so a replay can strip what was
applied, apply something else, and recompute the whole decision — probability,
sigma, every gate — without knowing anything about which config shipped when.

That last part is the lesson from the 2026-07-31 audit: the old `signals` table
stored POST-correction values, so reconstructing what the models actually said
meant hardcoding which corrections were live on which date. MODEL_BIAS_CORRECTIONS
changed twice in one afternoon with no record, and the resulting harness got the
ensemble mean wrong on 5 of 27 trades.

Usage:
    from replay import replay_rows, ConfigOverride
    rows = load_rows(since="2026-08-01")
    base = replay_rows(rows)                                  # as-logged config
    alt  = replay_rows(rows, ConfigOverride(sigma_scale_high=0.95))
"""
import json
import logging
from dataclasses import dataclass, field

from db import fetch_query
from strategy import is_same_day_trade, NON_BINDING_GATES


@dataclass
class ConfigOverride:
    """Any subset of the probability/gating constants. None = use the live value.

    Deliberately explicit rather than a dict of names: a typo'd key in a dict
    silently replays the wrong configuration and looks like a result."""
    sigma_scale_high: float = None
    sigma_scale_low: float = None
    # {(city, is_high): scale}; None = live table. A city with a scale (from
    # either source) skips the convective multiplier, as in production.
    city_sigma_scales: dict = None
    sigma_spread_coef: float = None
    base_forecast_error: dict = None
    min_sigma_f: float = None
    max_sigma_f: float = None
    student_t_df: float = None
    convective_std_inflation: float = None
    narrow_bucket_std_inflation: float = None
    narrow_bucket_edge_threshold: float = None
    edge_threshold: float = None
    min_bucket_prob: float = None
    enable_prob_calibration: bool = None
    prob_calibration_intercept: float = None
    prob_calibration_slope: float = None
    metar_warm_correction_f: float = None
    # model -> (high, low) in °F, replacing MODEL_BIAS_CORRECTIONS entirely
    model_bias: dict = None
    gfs_bias: dict = None
    min_model_agreement: float = None
    max_model_spread_std: float = None
    min_model_confidence: float = None
    max_model_confidence: float = None
    min_entry_price: float = None
    max_entry_price: float = None
    excluded_cities: set = None
    trade_high_markets: bool = None
    trade_low_markets: bool = None
    min_depth_multiple: float = None
    max_entry_spread_fraction: float = None
    max_hours_to_resolution: float = None
    forecast_margin_f: float = None


def load_rows(since=None, until=None, limit=None, city=None, fingerprint=None):
    """Pull replay rows joined to nothing — gates are recomputed, not read."""
    sql = ["SELECT * FROM replay_signals WHERE 1=1"]
    params = []
    if since:
        sql.append("AND timestamp >= ?"); params.append(since)
    if until:
        sql.append("AND timestamp < ?"); params.append(until)
    if city:
        sql.append("AND city_key = ?"); params.append(city)
    if fingerprint:
        sql.append("AND config_fingerprint = ?"); params.append(fingerprint)
    sql.append("ORDER BY id")
    if limit:
        sql.append("LIMIT ?"); params.append(limit)
    return [dict(r) for r in fetch_query(" ".join(sql), tuple(params))]


def _corrected_temps(row, ov):
    """Model temps under `ov`'s corrections, from the stored RAW values.

    No knowledge of what shipped when: the raw values are raw, full stop."""
    import config as C
    raw = json.loads(row["raw_models_pre_correction"] or "{}")
    if not raw:
        # Pre-replay-log rows, or a fetch that failed to record raw values.
        # Fall back to un-applying what was recorded as applied.
        return None
    city_key, is_high = row["city_key"], bool(row["is_high"])
    out = {}
    for model, v in raw.items():
        if ov.model_bias is not None:
            hi_lo = ov.model_bias.get(model)
            if hi_lo is None:
                continue  # model not in the alternative ensemble
            c = hi_lo[0] if is_high else hi_lo[1]
        else:
            c = C.model_bias_correction(model, is_high)
        gfs = ov.gfs_bias if ov.gfs_bias is not None else C.GFS_BIAS_CORRECTIONS
        if model == "gfs_global" and city_key in gfs:
            c += gfs[city_key]
        out[model] = v + c
    return out


def _col(row, name, default=None):
    """A column that may not exist on older rows. Missing means the field
    did not exist when the row was written, not that its value was False."""
    try:
        v = row[name]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def replay_row(row, ov=None):
    """Recompute probability, sigma and every gate for one stored signal.

    Returns a dict with the same shape as the logged columns, so a replay can be
    diffed against the row that produced it — which is exactly the acceptance
    test for this module."""
    import config as C
    import weather as W
    import strategy as S
    ov = ov or ConfigOverride()

    def pick(o, live):
        return live if o is None else o

    temps = _corrected_temps(row, ov)
    if not temps:
        return None
    weights = json.loads(row["model_weights"] or "{}")
    if ov.model_bias is not None:
        weights = {m: w for m, w in weights.items() if m in temps}
    tw = sum(weights.get(m, 0.0) for m in temps)
    if tw <= 0 or len(temps) < C.MIN_MODEL_COUNT:
        return None

    raw_wmean = sum(v * weights.get(m, 0.0) / tw for m, v in temps.items())
    mean = raw_wmean + pick(ov.metar_warm_correction_f, C.METAR_WARM_CORRECTION_F)
    spread_sd = W._weighted_pstdev(temps, weights)
    agreement = (sum(weights.get(m, 0.0) for m, t in temps.items()
                     if abs(t - raw_wmean) < 2.0) / tw)

    # Sigma, recomputed through the production formula with overrides applied.
    base = _interp(pick(ov.base_forecast_error, C.BASE_FORECAST_ERROR),
                   row["lead_hours"])
    coef = pick(ov.sigma_spread_coef, C.SIGMA_SPREAD_COEF)
    city_scales = pick(ov.city_sigma_scales, C.CITY_SIGMA_SCALES)
    city_k = city_scales.get((row["city_key"], bool(row["is_high"])))
    k = city_k if city_k else (
        pick(ov.sigma_scale_high, C.SIGMA_SCALE_HIGH) if row["is_high"]
        else pick(ov.sigma_scale_low, C.SIGMA_SCALE_LOW))
    sigma = k * (base + coef * spread_sd)
    # Per-city scales absorb the convective inflation (weather.py mirror).
    if row["city_key"] in C.CONVECTIVE_CITIES and not city_k:
        sigma *= pick(ov.convective_std_inflation, C.CONVECTIVE_STD_INFLATION)
    max_sigma = pick(ov.max_sigma_f, C.MAX_SIGMA_F)
    sigma = min(max(sigma, pick(ov.min_sigma_f, C.MIN_SIGMA_F)), max_sigma)
    sigma_pre_narrow = sigma
    if row["is_narrow"]:
        sigma = min(sigma * pick(ov.narrow_bucket_std_inflation,
                                 C.NARROW_BUCKET_STD_INFLATION), max_sigma)

    prob = _prob(mean, sigma, row["bucket_low"], row["bucket_high"], ov, C, W)

    no_px = row["no_price"]
    sf = row["spread_fraction"]
    fee = C.TAKER_FEE_RATE * no_px * (1.0 - no_px)
    # Slippage from the WALKED book when the row recorded one, exactly as the
    # live path does. `spread_fraction * price` measures crossing the spread, a
    # single-level move, and understated the real cost of the 2026-08-06 Austin
    # fill by 4x. Rows written before walked_vwap existed fall back to the old
    # formula — that is what those rows were actually traded on, so reproducing
    # them any other way would make the replay disagree with history.
    walked = _col(row, "walked_vwap")
    if walked is not None and no_px:
        slip_frac = max((walked - no_px) / no_px, 0.0)
    else:
        slip_frac = sf if sf is not None else C.SLIPPAGE_FRACTION
    slip = slip_frac * no_px
    max_conf = pick(ov.max_model_confidence, C.MAX_MODEL_CONFIDENCE)
    p_side = min(1.0 - prob["post_floor"], max_conf)
    no_edge = (p_side - no_px) - (fee + slip)

    thr = (pick(ov.narrow_bucket_edge_threshold, C.NARROW_BUCKET_EDGE_THRESHOLD)
           if row["is_narrow"] else pick(ov.edge_threshold, C.EDGE_THRESHOLD))

    fill = walked if walked is not None else no_px
    hours_res = _col(row, "hours_to_resolution") if _col(row, "hours_to_resolution") is not None else row.get("lead_hours")

    lo, hi = row["bucket_low"], row["bucket_high"]
    margin_f = pick(ov.forecast_margin_f, C.FORECAST_MARGIN_F)
    city_name = row.get("city") or row["city_key"]
    excluded = pick(ov.excluded_cities, C.EXCLUDED_CITIES)
    is_high = bool(row["is_high"])
    kind_ok = (pick(ov.trade_high_markets, C.TRADE_HIGH_MARKETS) if is_high
               else pick(ov.trade_low_markets, C.TRADE_LOW_MARKETS))
    gates = [
        ("excluded_city", 1.0 if city_name in excluded else 0.0, 0.0,
         city_name not in excluded),
        ("market_kind", 1.0 if is_high else 0.0, None, kind_ok),
        ("edge_threshold", no_edge, thr, no_edge >= thr),
        ("model_agreement", agreement, pick(ov.min_model_agreement, C.MIN_MODEL_AGREEMENT),
         agreement >= pick(ov.min_model_agreement, C.MIN_MODEL_AGREEMENT)),
        ("model_spread_sd", spread_sd, pick(ov.max_model_spread_std, C.MAX_MODEL_SPREAD_STD),
         spread_sd <= pick(ov.max_model_spread_std, C.MAX_MODEL_SPREAD_STD)),
        ("model_confidence", p_side, pick(ov.min_model_confidence, C.MIN_MODEL_CONFIDENCE),
         p_side > pick(ov.min_model_confidence, C.MIN_MODEL_CONFIDENCE)),
        ("max_model_confidence", p_side, pick(ov.max_model_confidence, C.MAX_MODEL_CONFIDENCE),
         p_side <= pick(ov.max_model_confidence, C.MAX_MODEL_CONFIDENCE)),
        ("time_to_resolution", hours_res, pick(ov.max_hours_to_resolution, C.MAX_HOURS_TO_RESOLUTION),
         hours_res is None or hours_res <= pick(ov.max_hours_to_resolution, C.MAX_HOURS_TO_RESOLUTION)),
        ("same_day_trade", 1.0 if is_same_day_trade(row.get("target_date"), row.get("city"), hours_res) else 0.0,
         1.0 if C.REQUIRE_SAME_DAY else 0.0,
         (not C.REQUIRE_SAME_DAY) or is_same_day_trade(row.get("target_date"), row.get("city"), hours_res)),
        ("book_depth", _col(row, "usable_depth_usd"),
         pick(ov.min_depth_multiple, C.MIN_DEPTH_MULTIPLE) * (_col(row, "stake_usd") or 0.0),
         _col(row, "usable_depth_usd") is None
         or _col(row, "usable_depth_usd") >= pick(ov.min_depth_multiple,
                                                  C.MIN_DEPTH_MULTIPLE) * (_col(row, "stake_usd") or 0.0)),
        ("book_readable", sf, None, sf is not None),
        ("market_spread_frac", sf,
         pick(ov.max_entry_spread_fraction, C.MAX_ENTRY_SPREAD_FRACTION),
         sf is not None and sf <= pick(ov.max_entry_spread_fraction,
                                       C.MAX_ENTRY_SPREAD_FRACTION)),
        ("min_entry_price", fill, pick(ov.min_entry_price, C.MIN_ENTRY_PRICE),
         fill >= pick(ov.min_entry_price, C.MIN_ENTRY_PRICE)),
        ("max_entry_price", fill, pick(ov.max_entry_price, C.MAX_ENTRY_PRICE),
         fill <= pick(ov.max_entry_price, C.MAX_ENTRY_PRICE)),
        ("forecast_margin", mean, margin_f,
         S.forecast_margin_ok("NO", mean, lo, hi, margin_f)),
        ("forecast_direction", raw_wmean, None,
         S.forecast_direction_agrees("NO", raw_wmean, lo, hi)),
    ]
    # The independent-veto gates were removed with the feature on 2026-08-08.
    # Historical v2 rows keep their stored independent_* columns, but a replay
    # scores the CURRENT gate set — a removed gate can refuse nothing.
    return {
        "id": row["id"], "market_id": row["market_id"], "city_key": row["city_key"],
        "target_date": row["target_date"], "is_high": row["is_high"],
        "raw_weighted_mean": raw_wmean, "ensemble_mean": mean,
        "weighted_spread_sd": spread_sd, "model_agreement": agreement,
        "sigma_post_clamp": sigma_pre_narrow, "sigma_final": sigma,
        "prob_raw": prob["raw"], "prob_post_platt": prob["post_platt"],
        "prob_post_floor": prob["post_floor"],
        "edge_post_fee": no_edge, "edge_threshold": thr,
        "gates": [{"gate": g, "observed": o, "threshold": t, "passed": p}
                  for g, o, t, p in gates],
        # Mirrors the live decision exactly: only BINDING gates refuse.
        # would_trade_all_gates keeps the pre-2026-08-12 semantics as the
        # counterfactual ("would the old rule set have taken this?").
        "would_trade": all(p for g, _, _, p in gates
                           if g not in NON_BINDING_GATES),
        "would_trade_all_gates": all(p for _, _, _, p in gates),
        "settled_value": row["settled_value"], "settled_outcome": row["settled_outcome"],
    }


def _interp(table, lead):
    bps = sorted(table)
    if not bps:
        return 2.5
    if lead <= bps[0]:
        return table[bps[0]]
    if lead >= bps[-1]:
        return table[bps[-1]]
    for a, b in zip(bps, bps[1:]):
        if a <= lead <= b:
            return table[a] + (lead - a) / (b - a) * (table[b] - table[a])
    return table[bps[-1]]


def _prob(mean, sigma, lo, hi, ov, C, W):
    def pick(o, live):
        return live if o is None else o
    sigma = max(sigma, 0.5)
    df = pick(ov.student_t_df, C.SIGMA_STUDENT_T_DF)
    cdf = ((lambda x: W._student_t_cdf(x, mean, sigma, df)) if df and df > 2.0
           else (lambda x: W._norm_cdf(x, mean, sigma)))
    lb = (lo - 0.5) if lo is not None else -1000.0
    ub = (hi + 0.5) if hi is not None else 1000.0
    raw = max(0.0, min(1.0, cdf(ub) - cdf(lb)))
    bounded = lo is not None and hi is not None
    p = raw
    if bounded and pick(ov.enable_prob_calibration, C.ENABLE_PROB_CALIBRATION) \
            and 0.0 < p < 0.5:
        import math
        eps = 1e-4
        pc = min(max(p, eps), 1.0 - eps)
        z = (pick(ov.prob_calibration_intercept, C.PROB_CALIBRATION_INTERCEPT)
             + pick(ov.prob_calibration_slope, C.PROB_CALIBRATION_SLOPE)
             * math.log(pc / (1.0 - pc)))
        cal = 1.0 / (1.0 + math.exp(-z))
        p = p + ((0.5 - p) / 0.5) * (cal - p)
    post_platt = p
    floor = pick(ov.min_bucket_prob, C.MIN_BUCKET_PROB)
    if floor > 0.0 and p < floor:
        p = floor
    return {"raw": raw, "post_platt": post_platt,
            "post_floor": max(0.0, min(1.0, p))}


def replay_rows(rows, ov=None):
    out = []
    for r in rows:
        try:
            res = replay_row(r, ov)
        except Exception as e:
            logging.warning(f"replay failed for signal {r.get('id')}: {e}")
            res = None
        if res:
            out.append(res)
    return out
