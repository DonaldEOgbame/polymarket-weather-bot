"""Purged, grouped, walk-forward evaluation of a configuration.

Every constant in config.py has been fitted at least once on a sample too small
to support it, three times with confidence intervals wider than the correction.
This is the thing that would have said so.

Three design choices, each load-bearing:

GROUPED BY DATE, ACROSS ALL CITIES. One synoptic pattern hits a dozen cities at
    once, so a Chicago row and an Austin row on the same target date are not two
    observations — they are one, seen twice. A random split puts some of them in
    train and some in test, the model "learns" the day, and measured skill is
    wildly overstated. Folds are therefore whole target dates, and a date is
    entirely in train or entirely in test.

PURGED. A signal logged at 06:00 for a target date three days out overlaps, in
    information, with signals for the days on either side: the same model run
    produced them. An embargo of PURGE_DAYS around each test fold is dropped
    from training so the split is on information, not just on the calendar.

WALK-FORWARD. Train on the past, test on the future, roll. Never the reverse.
    A k-fold that trains on August to predict July is measuring interpolation
    and reporting it as forecasting skill.

SCORED ON WHAT PAYS. Log score and Brier on the buckets a configuration would
    actually have TRADED, then simulated P&L net of fees and spread. Not MAE:
    a configuration can improve mean absolute error on the ensemble and lose
    money, because the money is in the tails of bounded buckets and MAE is
    dominated by the middle. Not win rate: at these prices a 79% win rate can
    lose and a 60% win rate can win.

Runs against whatever sample exists and gets more useful as settled_value fills.
It will report a small n honestly rather than producing a confident number from
23 rows, which is the specific failure mode this project keeps hitting.
"""
import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from replay import ConfigOverride, load_rows, replay_rows

# Days of embargo either side of a test fold. Forecast lead times run to 72h and
# a single model run informs several target dates, so 3 is the horizon over
# which two rows can share information.
PURGE_DAYS = 3

# The reliability band the bot actually bets in. Restricting to it is not
# cherry-picking: a diagram over [0, 1] is dominated by bins holding no trades,
# and the whole question is whether p=0.05 means 5%.
RELIABILITY_BAND = (0.01, 0.15)

HORIZON_BANDS = [(0, 24), (24, 48), (48, 96)]


def _log_score(p, outcome):
    """Negative log likelihood of the realised outcome. Lower is better.

    Unbounded on a confident miss, which is the point — it is the only common
    score that punishes "P(YES)=0.00008" on a bucket that hit, and that is
    exactly what Guangzhou #31 did."""
    p = min(max(p, 1e-9), 1.0 - 1e-9)
    return -math.log(p if outcome else 1.0 - p)


def _brier(p, outcome):
    return (p - (1.0 if outcome else 0.0)) ** 2


def _pnl(row, prob, price, stake, fee_rate, outcome_yes):
    """Simulated P&L of one NO trade, net of fees and spread.

    Realistic fills: entry crosses the spread at the logged ask-side price and
    pays the taker fee both legs (fee_bps is round-trip — the 2026-07-24
    correction). Settlement pays $1 per share on a NO that wins."""
    if not price or price <= 0 or price >= 1:
        return 0.0
    shares = stake / price
    spread = row.get("spread_fraction") or 0.0
    entry_cost = stake * (1.0 + spread)
    fee = fee_rate * price * (1.0 - price) * shares
    payout = 0.0 if outcome_yes else shares * 1.0
    return payout - entry_cost - fee


def _folds(dates, n_folds, purge_days=PURGE_DAYS):
    """Walk-forward (train_dates, test_dates) pairs, purged around the test set.

    Dates are sorted, so every test fold is strictly later than its training
    data. The first fold is skipped: with nothing before it there is nothing to
    train on."""
    dates = sorted(set(dates))
    if len(dates) < n_folds + 1:
        n_folds = max(1, len(dates) - 1)
    size = max(1, len(dates) // (n_folds + 1))
    out = []
    for k in range(1, n_folds + 1):
        cut = size * k
        test = dates[cut:cut + size]
        if not test:
            break
        lo = _parse(test[0]) - timedelta(days=purge_days)
        train = [d for d in dates[:cut] if _parse(d) < lo]
        if train and test:
            out.append((train, test))
    return out


def _parse(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def evaluate(rows, ov=None, stake=6.0, fee_rate=0.05):
    """Score one configuration over already-selected rows.

    Only rows that (a) the configuration would have traded and (b) have settled
    are scored. Both conditions matter: scoring untraded rows measures a
    forecast nobody acted on, and scoring unsettled rows measures nothing."""
    import config as C
    replayed = replay_rows(rows, ov)
    by_id = {r["id"]: r for r in rows}

    scored = []
    for r in replayed:
        if r["settled_outcome"] is None:
            continue
        if not r["would_trade"]:
            continue
        outcome_yes = r["settled_outcome"] == "YES"
        p_yes = r["prob_post_floor"]
        src = by_id.get(r["id"], {})
        scored.append({
            "id": r["id"], "city": r["city_key"], "target_date": r["target_date"],
            "is_high": bool(r["is_high"]), "lead_hours": src.get("lead_hours"),
            "p_yes": p_yes, "outcome_yes": outcome_yes,
            # The bot buys NO, so the probability it is betting on is 1 - p_yes.
            "p_traded": 1.0 - p_yes, "traded_won": not outcome_yes,
            "log_score": _log_score(p_yes, outcome_yes),
            "brier": _brier(p_yes, outcome_yes),
            "pnl": _pnl(src, p_yes, src.get("no_price"), stake, fee_rate, outcome_yes),
        })

    n = len(scored)
    if n == 0:
        return {"n": 0, "note": "no settled rows this configuration would have traded"}
    return {
        "n": n,
        "log_score": sum(s["log_score"] for s in scored) / n,
        "brier": sum(s["brier"] for s in scored) / n,
        "pnl": sum(s["pnl"] for s in scored),
        "pnl_per_trade": sum(s["pnl"] for s in scored) / n,
        "win_rate": sum(1 for s in scored if s["traded_won"]) / n,
        "scored": scored,
    }


def walk_forward(rows, ov=None, n_folds=4, stake=6.0):
    """Out-of-sample scores, fold by fold.

    The training half is not used to FIT anything here — the harness scores a
    configuration rather than fitting one — but the split is still enforced, and
    reported, so that when a fitting step is added (Phase 3.2) it inherits a
    correct protocol rather than needing one bolted on."""
    by_date = defaultdict(list)
    for r in rows:
        if r.get("target_date"):
            by_date[r["target_date"]].append(r)

    folds = _folds(list(by_date), n_folds)
    if not folds:
        return {"folds": [], "note": f"only {len(by_date)} distinct target dates — "
                                     f"not enough for a walk-forward split"}

    out = []
    for i, (train, test) in enumerate(folds):
        test_rows = [r for d in test for r in by_date[d]]
        res = evaluate(test_rows, ov, stake=stake)
        res.pop("scored", None)
        out.append({"fold": i, "train_dates": len(train), "test_dates": len(test),
                    "purged_days": PURGE_DAYS, **res})
    graded = [f for f in out if f["n"] > 0]
    summary = {}
    if graded:
        tot = sum(f["n"] for f in graded)
        summary = {
            "total_scored": tot,
            "log_score": sum(f["log_score"] * f["n"] for f in graded) / tot,
            "brier": sum(f["brier"] * f["n"] for f in graded) / tot,
            "pnl": sum(f["pnl"] for f in graded),
        }
    return {"folds": out, "summary": summary}


def reliability(scored, band=RELIABILITY_BAND, bins=6):
    """Predicted vs realised frequency, restricted to the band the bot bets in.

    Reported on the TRADED side's probability, because that is the number the
    edge calculation uses. A bot that says "NO is 95% likely" and is right 88%
    of the time is losing money that a p(YES) diagram would show as a small
    absolute error."""
    lo, hi = band
    in_band = [s for s in scored if lo <= s["p_yes"] <= hi]
    if not in_band:
        return {"band": band, "n": 0}
    width = (hi - lo) / bins
    out = []
    for b in range(bins):
        a, z = lo + b * width, lo + (b + 1) * width
        sel = [s for s in in_band if a <= s["p_yes"] < z or (b == bins - 1 and s["p_yes"] == z)]
        if not sel:
            continue
        out.append({
            "bin": f"{a:.3f}-{z:.3f}", "n": len(sel),
            "predicted": sum(s["p_yes"] for s in sel) / len(sel),
            "observed": sum(1 for s in sel if s["outcome_yes"]) / len(sel),
        })
    return {"band": band, "n": len(in_band), "bins": out}


def split_reliability(scored):
    """Reliability by direction and by horizon band, separately.

    Pooled reliability hides the finding that matters: sigma was fitted per
    direction on 2026-07-31 precisely because maxima and minima are different
    problems with opposite biases, and a pooled diagram would have averaged them
    into looking calibrated."""
    out = {"by_direction": {}, "by_horizon": {}}
    for label, sel in (("high", [s for s in scored if s["is_high"]]),
                       ("low", [s for s in scored if not s["is_high"]])):
        out["by_direction"][label] = reliability(sel)
    for lo, hi in HORIZON_BANDS:
        sel = [s for s in scored
               if s["lead_hours"] is not None and lo <= s["lead_hours"] < hi]
        out["by_horizon"][f"{lo}-{hi}h"] = reliability(sel)
    return out


def check_platt_was_out_of_sample(rows):
    """Whether the Platt constants could have been fitted on the rows scored here.

    Returns a warning, not a verdict — the harness cannot know what the fit was
    run on. But the constants were fitted on 2026-07-31, so any row logged
    before that date is in-sample for them, and reliability measured on those
    rows is measuring the fit against itself.

    PROB_CALIBRATION_* were fitted against the `signals` table (96,307 resolved
    signals), which is a different population from replay_signals — but an
    overlapping one, over the same markets and the same days."""
    fit_date = "2026-07-31"
    before = [r for r in rows if (r.get("timestamp") or "") < fit_date]
    return {
        "platt_fit_date": fit_date,
        "rows_before_fit": len(before),
        "rows_after_fit": len(rows) - len(before),
        "warning": (
            f"{len(before)} of {len(rows)} rows predate the Platt fit and are "
            f"IN-SAMPLE for PROB_CALIBRATION_INTERCEPT/SLOPE. Reliability on "
            f"those rows measures the fit against itself."
            if before else
            "All rows postdate the 2026-07-31 Platt fit — reliability here is "
            "out-of-sample for the calibration constants."),
    }


def double_counting_report():
    """The overlapping corrections, named.

    Three separate adjustments are applied to one error, and nothing checks that
    they do not overlap:

      METAR_WARM_CORRECTION_F   a single global °F shift
      GFS_BIAS_CORRECTIONS      per-city, gfs_global only
      MODEL_BIAS_CORRECTIONS    per-model, per-direction

    All three were fitted against the same quantity: Open-Meteo forecast minus
    METAR observation. If the per-model table already absorbs the warm bias,
    METAR_WARM_CORRECTION_F double-counts it; if the per-city GFS table absorbs
    a city's warmth, the per-model GFS entry does it again. Reported rather than
    resolved, because resolving it needs the sample the harness exists to
    accumulate."""
    import config as C
    from families import family_of
    rows = []
    if C.METAR_WARM_CORRECTION_F:
        rows.append(f"METAR_WARM_CORRECTION_F is {C.METAR_WARM_CORRECTION_F:+.2f}°F "
                    f"globally, ON TOP of every per-model correction")
    overlap = sorted(set(C.GFS_BIAS_CORRECTIONS))
    if overlap:
        hi, lo = C.model_bias_correction("gfs_global", True), \
                 C.model_bias_correction("gfs_global", False)
        if hi or lo:
            rows.append(
                f"gfs_global carries BOTH a per-model correction "
                f"({hi:+.2f}/{lo:+.2f}°F high/low) and per-city corrections for "
                f"{len(overlap)} cities — the same error corrected twice for "
                f"those cities")
    return {
        "metar_warm_correction_f": C.METAR_WARM_CORRECTION_F,
        "gfs_bias_cities": len(C.GFS_BIAS_CORRECTIONS),
        "overlaps": rows,
        "note": ("These are three overlapping adjustments to ONE error "
                 "(Open-Meteo minus METAR). Nothing currently checks they do "
                 "not double-count."),
    }


def run(since=None, until=None, n_folds=4, stake=6.0, ov=None):
    rows = load_rows(since=since, until=until)
    settled = [r for r in rows if r.get("settled_value") is not None]
    report = {
        "rows_loaded": len(rows),
        "rows_settled": len(settled),
        "distinct_target_dates": len({r.get("target_date") for r in settled}),
        "distinct_cities": len({r.get("city_key") for r in settled}),
        "fingerprints": sorted({r.get("config_fingerprint") for r in rows if r.get("config_fingerprint")}),
        "platt": check_platt_was_out_of_sample(rows),
        "double_counting": double_counting_report(),
    }
    if not settled:
        report["note"] = (
            "No settled replay rows. Until backfill_replay_outcomes has run "
            "against resolved target dates there is nothing to score — this is "
            "the state production was in until 2026-08-05, with 182,530 rows "
            "and 0 settled.")
        return report

    report["walk_forward"] = walk_forward(settled, ov, n_folds=n_folds, stake=stake)
    full = evaluate(settled, ov, stake=stake)
    scored = full.pop("scored", [])
    report["in_sample"] = full
    report["reliability"] = reliability(scored)
    report["reliability_splits"] = split_reliability(scored)
    if full.get("n", 0) < 100:
        report["warning"] = (
            f"n={full.get('n')} scored trades. Every constant in config.py has "
            f"been fitted at least once on a sample this size and the "
            f"confidence interval exceeded the correction each time. Treat "
            f"these numbers as a pipeline check, not as evidence.")
    return report


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--stake", type=float, default=6.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    report = run(since=args.since, until=args.until, n_folds=args.folds,
                 stake=args.stake)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
