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


# --- Exit-side physics: has the local day already decided this market? ---
#
# Everything above conditions a FORECAST for the entry decision. What follows
# answers a narrower, harder question for the exit decision: is the outcome
# already arithmetic? The two are separate on purpose — `condition` may decline
# for a dozen soft reasons (conditioning disabled, too early, no diurnal range)
# and still leave the ratchet below perfectly usable.

LOCKED_WIN = "LOCKED_WIN"
LOCKED_LOSS = "LOCKED_LOSS"
UNDECIDED = "UNDECIDED"
UNKNOWN = "UNKNOWN"


def _day_phase(city_key, target_date):
    """(local_hour, day_over) for the target date at the settlement station.

    local_hour is None once the day is over, which is exactly when day_over is
    True — a past day has no "hours still to come", so the ratchet below treats
    the observed extreme as final. Returns (None, False) for a FUTURE day and
    for an unreadable station: nothing is known, and the caller must not read
    that as "decided".
    """
    from metar import get_station
    _, tz = get_station(city_key)
    if not tz:
        return (None, False)
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        return (None, False)
    today = now.date().isoformat()
    if target_date == today:
        return (now.hour + now.minute / 60.0, False)
    if target_date < today:
        return (None, True)
    return (None, False)


_UNSET = object()   # so an injected observed=None means "none available", not "fetch it"


def settlement_state(city_key, target_date, is_high, bucket_low, bucket_high,
                     side, observed=_UNSET, hour=None, day_over=None):
    """Where today's observations have already put this position, as arithmetic.

    A daily maximum can only RISE for the rest of the local day; a daily minimum
    can only FALL. That one-way ratchet decides some positions outright, whatever
    the order book happens to be printing:

      * observed extreme already CLEAR of the bucket on the side the ratchet
        cannot walk back  ->  a NO settles at $1.  LOCKED_WIN
      * observed extreme short of the bucket with the day's rise spent  ->  it
        will never reach the bucket.                                LOCKED_WIN
      * observed extreme INSIDE the bucket with the rise spent  ->  a NO settles
        at $0.                                                     LOCKED_LOSS
      * anything else                                              UNDECIDED

    UNDECIDED deliberately covers the most dangerous moment in the life of one of
    these positions: the observed extreme sitting inside the bucket with heating
    still to come. There the book is pricing the TRANSIT, not the outcome, and it
    dislocates violently. Qingdao 2026-08-11 was stopped out in exactly that
    state — mid 0.295 at 13:18 local with the running max at 30.3°C, one bucket
    step below the 30.8°C it peaked at 42 minutes later, and it settled at $1.00.
    See [gate scoreboard] and the STOP_LOSS_PCT note in config.py, which recorded
    the identical Chongqing 2026-07-25 near-miss a fortnight before it recurred.

    Returns UNKNOWN when the observation, station or local hour is unavailable.
    Callers must treat UNKNOWN as "do not sell at a loss", never as permission.

    `observed`, `hour` and `day_over` exist to be injected by tests; in
    production all three are resolved here.
    """
    from config import EXIT_PEAK_PASSED_FRACTION, BUCKET_EDGE_PAD_F

    if hour is None and day_over is None:
        hour, day_over = _day_phase(city_key, target_date)
    if hour is None and not day_over:
        return {"state": UNKNOWN, "observed": None, "reason": "day not started or station unreadable"}

    if observed is _UNSET:
        observed = observed_extreme(city_key, target_date, is_high)
    if observed is None:
        return {"state": UNKNOWN, "observed": None, "reason": "no observation available"}

    # Once the day is over the extreme IS final, so there is no rise left by
    # definition. Mid-day, consult the fitted diurnal curve.
    if day_over:
        rise_left = 0.0
    else:
        frac = remaining_fraction(hour, is_high)
        # An unusable curve is not evidence the day is done — stay UNDECIDED.
        rise_left = 1.0 if frac is None else frac[0]
    spent = rise_left <= EXIT_PEAK_PASSED_FRACTION

    # Pad the bucket by the same half-degree the exit path's METAR check uses, so
    # the two never disagree about whether a reading is "in" the bucket.
    lo = (bucket_low - BUCKET_EDGE_PAD_F) if bucket_low is not None else float("-inf")
    hi = (bucket_high + BUCKET_EDGE_PAD_F) if bucket_high is not None else float("inf")

    # `no_wins` = does the final extreme land OUTSIDE the bucket? Resolved from
    # the ratchet direction, then flipped at the end for a YES position.
    if is_high:
        if observed > hi:
            no_wins = True                      # max only rises; already clear above
        elif observed < lo:
            no_wins = True if spent else None   # may still climb into the bucket
        else:
            # Inside. Escaping needs a climb above `hi` — and an open-ended
            # "X or above" bucket has no `hi` to climb above, so a max already
            # inside one can never leave it however much rise is left.
            no_wins = False if (spent or hi == float("inf")) else None
    else:
        if observed < lo:
            no_wins = True                      # min only falls; already clear below
        elif observed > hi:
            no_wins = True if spent else None   # may still fall into the bucket
        else:
            no_wins = False if (spent or lo == float("-inf")) else None

    detail = (f"observed {observed:.1f}°F vs bucket [{lo:.1f}, {hi:.1f}], "
              f"{'day over' if day_over else f'{hour:.1f}h local'}, "
              f"rise left {rise_left:.1%}{' (spent)' if spent else ''}")

    if no_wins is None:
        return {"state": UNDECIDED, "observed": observed, "reason": detail}
    ours_wins = no_wins if str(side).upper() == "NO" else not no_wins
    return {"state": LOCKED_WIN if ours_wins else LOCKED_LOSS,
            "observed": observed, "reason": detail}
