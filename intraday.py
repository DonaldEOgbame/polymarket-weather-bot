"""Conditioning the forecast distribution on observations already in hand.

Until now nothing in this bot looked at what the day had already done. At 15:00
local with the station reading 91°F, the model was still pricing "will the max
be below 91?" off a 00Z forecast — a question whose answer is already known and
is no.

The conditioning has two parts, and the first is worth more than the second:

1. A HARD BOUND. The final daily max cannot be below the max already observed;
   the final daily min cannot be above the min already observed. This is not a
   probabilistic statement, it is arithmetic, and it collapses whole buckets to
   zero regardless of what any ensemble says.

2. THE REMAINING RISE. Between the observed extreme and the end of the day there
   is some rise left, and how much depends mostly on how far through the diurnal
   cycle the station is. Modelled as a fitted, DIMENSIONLESS fraction of the
   day's diurnal range (see fit_remaining_rise.py), multiplied by the range the
   ensemble forecasts for that day — which Phase 1.2 made available for free by
   returning max and min from one request.

Expected to pay most on LOW markets. An overnight minimum is usually set before
dawn, so by the time a market has a full trading day of liquidity the answer has
been sitting in the observations for hours. Daily maxima land mid-afternoon with
settlement close behind, leaving a much shorter window.

Falls back to the unconditioned distribution whenever observations are missing,
the local day has not started, or the station is unreadable. That path must stay
cheap and silent: it is the common case for a market 48 hours out.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (ENABLE_INTRADAY_CONDITIONING, INTRADAY_MIN_HOURS_ELAPSED,
                    REMAINING_RISE_TABLE, INTRADAY_SIGMA_FLOOR_F)


def local_hour(city_key, target_date):
    """Hours elapsed into the target LOCAL day, or None if it is not today.

    Returns None for a future day (nothing observed yet) and None for a past day
    (the day is over; settlement, not conditioning, is the right tool). The
    station's own timezone is used because that is the day the market settles
    on — see the Phase 1.1 audit."""
    from metar import get_station
    _, tz = get_station(city_key)
    if not tz:
        return None
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        return None
    today = now.date().isoformat()
    if target_date != today:
        return None
    return now.hour + now.minute / 60.0


def remaining_fraction(hour, is_high):
    """Fitted (mean, sd) fraction of the diurnal range still to come at `hour`.

    Interpolated between the two bracketing whole hours, because the table is
    hourly and the curve is steep through the late morning — snapping 11:59 back
    to 11:00 there is worth a meaningful fraction of a degree."""
    lo = int(hour)
    hi = min(lo + 1, 23)
    t = hour - lo
    mk, sk = ("f_mean", "f_sd") if is_high else ("g_mean", "g_sd")
    a, b = REMAINING_RISE_TABLE.get(lo), REMAINING_RISE_TABLE.get(hi)
    if a is None or b is None:
        return None
    return (a[mk] + t * (b[mk] - a[mk]), a[sk] + t * (b[sk] - a[sk]))


def observed_extreme(city_key, target_date, is_high):
    """The extreme observed SO FAR today, on the settlement ruler, or None.

    Deliberately resolved_extreme_f and not final_extreme_f: the point is the
    running value mid-day. final_extreme_f returns None until the local day has
    fully elapsed, which is every moment this function is useful."""
    from metar import resolved_extreme_f
    try:
        return resolved_extreme_f(city_key, target_date, is_high)
    except Exception as e:
        logging.debug(f"intraday observation unavailable for {city_key} "
                      f"{target_date}: {e}")
        return None


def condition(engine_result, target_date):
    """Return engine_result conditioned on today's observations so far.

    The returned dict is a COPY with `ensemble_mean`, `ensemble_std` and a new
    `hard_bound` adjusted, plus an `intraday` block recording every input so the
    replay log can reconstruct the decision. The original is never mutated —
    prefetch_signal_engines hands the same engine result to several markets.

    Returns the input unchanged when conditioning does not apply."""
    if not ENABLE_INTRADAY_CONDITIONING:
        return engine_result

    city_key = engine_result.get("city_key")
    is_high = bool(engine_result.get("is_high"))
    hour = local_hour(city_key, target_date)
    if hour is None or hour < INTRADAY_MIN_HOURS_ELAPSED:
        # Either the day has not started or it is too early for the observed
        # extreme to constrain anything — before dawn the running max is just
        # the overnight low and carries almost no information about the peak.
        return engine_result

    observed = observed_extreme(city_key, target_date, is_high)
    if observed is None:
        return engine_result

    frac = remaining_fraction(hour, is_high)
    if frac is None:
        return engine_result
    frac_mean, frac_sd = frac

    # The diurnal range the ENSEMBLE expects for this day. Available because
    # Phase 1.2 returns both directions from one request. Without it the
    # remaining rise cannot be converted from a fraction into degrees.
    diurnal = engine_result.get("forecast_diurnal_range_f")
    if diurnal is None or diurnal <= 0:
        return engine_result

    mu = engine_result["ensemble_mean"]
    sigma = engine_result["ensemble_std"]
    expected_remaining = frac_mean * diurnal

    if is_high:
        # The final max is at least what has been observed, plus whatever rise
        # the diurnal cycle has left. max() rather than a blend: if the ensemble
        # predicts higher still, it may know about a late frontal surge that the
        # climatology cannot, and taking the larger keeps that.
        conditioned_mu = max(mu, observed + expected_remaining)
    else:
        conditioned_mu = min(mu, observed - expected_remaining)

    # Uncertainty about the final extreme is now bounded by uncertainty about
    # the REMAINING rise, which shrinks through the day and reaches zero once
    # the peak has passed. Never taken above the ensemble's own sigma: the
    # observation can only remove uncertainty, never add it.
    conditioned_sigma = max(min(sigma, frac_sd * diurnal), INTRADAY_SIGMA_FLOOR_F)

    out = dict(engine_result)
    out["ensemble_mean"] = conditioned_mu
    out["ensemble_std"] = conditioned_sigma
    # Consumed by the bucket CDF as a hard truncation point. Arithmetic, not
    # probability: no distribution may place mass below an observed maximum.
    out["hard_bound"] = observed
    out["hard_bound_is_floor"] = bool(is_high)
    out["intraday"] = {
        "applied": True,
        "local_hour": round(hour, 2),
        "observed": observed,
        "remaining_fraction": round(frac_mean, 4),
        "remaining_fraction_sd": round(frac_sd, 4),
        "forecast_diurnal_range_f": round(diurnal, 2),
        "expected_remaining_f": round(expected_remaining, 2),
        "mean_before": round(mu, 2),
        "mean_after": round(conditioned_mu, 2),
        "sigma_before": round(sigma, 3),
        "sigma_after": round(conditioned_sigma, 3),
    }
    if conditioned_mu != mu or conditioned_sigma != sigma:
        logging.info(
            f"INTRADAY | {city_key} {target_date} {'max' if is_high else 'min'} | "
            f"{hour:.1f}h local, observed {observed:.1f}°F, "
            f"{frac_mean:.0%} of a {diurnal:.1f}°F range still to come | "
            f"mean {mu:.1f}→{conditioned_mu:.1f} sigma {sigma:.2f}→{conditioned_sigma:.2f}"
        )
    return out
