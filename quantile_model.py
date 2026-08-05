"""Quantile meta-model: one learned object replacing the hand-fitted stack.

What it would replace, and why replacing it is attractive:

    MODEL_BIAS_CORRECTIONS   per model, per direction, hand-fitted
    GFS_BIAS_CORRECTIONS     per city, one model
    METAR_WARM_CORRECTION_F  one global shift
    SIGMA_SPREAD_COEF        intercept + slope on ensemble spread
    SIGMA_SCALE_HIGH/LOW     per-direction scaling
    CONVECTIVE_STD_INFLATION per-city variance inflation
    NARROW_BUCKET_STD_INFLATION
    PROB_CALIBRATION_*       Platt scaling on top of all of it

Eight groups of constants, each fitted separately against overlapping data,
composed by multiplication, and then calibrated to undo the composition's
errors. LightGBM quantile regression on the settled value learns the whole
mapping at once: predict 9-11 quantiles, interpolate the CDF, read bucket
probabilities straight off it. Bias, heteroskedastic sigma, per-direction
asymmetry and calibration all become properties of one object fitted on one
objective, and the discrete lattice falls out naturally because the CDF is
evaluated at the bucket edges rather than assumed Gaussian.

WHY IT IS DISABLED.

This cannot be fitted yet. Three prior refits — sigma, Student-t df, and the
per-direction bias columns — each returned confidence intervals wider than the
correction they proposed, on 23 trades. A gradient-boosted model with ~30
features fitted on the same sample would not be a better answer to that
problem; it would be the same failure with more parameters and less visibility
into it.

So the pipeline is built, the training script is wired, and
ENABLE_QUANTILE_MODEL defaults to false. It stays off until harness.py shows it
beating the current stack OUT OF SAMPLE on the purged, grouped, walk-forward
split. Shipping a model fitted on the current sample repeats the failure this
project has already hit three times, and the fourth time would be harder to
detect because the object is opaque.

Deliberately NOT built separately: inverse-error weighting and any other
performance-based member weighting. Those are a strictly weaker special case of
what this learns — a linear reweighting of members with no interaction terms,
no heteroskedasticity, and no calibration — so building them would be building
a worse version of this and then having to retire it.
"""
import json
import logging
import math
import os
import sys

from config import (ENABLE_QUANTILE_MODEL, QUANTILE_MODEL_PATH,
                    QUANTILE_LEVELS)

# lightgbm is an optional dependency: the bot must run without it, since the
# model is off by default and the Fly image should not carry a 100MB wheel to
# support a disabled feature. Imported lazily inside the training path.
_MODEL_CACHE = {}


def available():
    """Whether a trained model is present AND enabled."""
    return ENABLE_QUANTILE_MODEL and os.path.exists(QUANTILE_MODEL_PATH)


# --- Features ---------------------------------------------------------------
# Grouped by FAMILY rather than by member, using the Phase 2.1 structure. Two
# reasons: the feature vector must have a fixed width while the member set
# varies per city and per day (per-city extra_models makes this the normal
# case, not an edge case), and per-member features would hand the model three
# correlated ICON columns to overfit on.

FEATURE_FAMILIES = ["ECMWF", "NCEP", "DWD", "CCMEP", "UKMO", "JMA", "KMA",
                    "CMA", "BOM", "METEOFRANCE", "NBM", "HARMONIE", "ARPAE"]


def build_features(row):
    """Feature vector for one signal. Returns (names, values).

    Every feature is available at DECISION time. That is not a stylistic
    preference — a feature computed from the settled value, however indirectly,
    produces a model that scores beautifully in the harness and loses money
    live, and it is the single easiest mistake to make here."""
    from families import family_of, native_timestep

    raw = row.get("raw_models_pre_correction")
    temps = json.loads(raw) if isinstance(raw, str) else (raw or {})
    weights = row.get("model_weights")
    weights = json.loads(weights) if isinstance(weights, str) else (weights or {})

    names, values = [], []

    # Per-family mean, deviation from the ensemble mean, and total weight.
    fam_vals, fam_w = {}, {}
    tw = sum(weights.get(m, 0.0) for m in temps) or 1.0
    ens_mean = sum(t * weights.get(m, 0.0) for m, t in temps.items()) / tw
    for m, t in temps.items():
        f = family_of(m)
        w = weights.get(m, 0.0)
        fam_vals[f] = fam_vals.get(f, 0.0) + t * w
        fam_w[f] = fam_w.get(f, 0.0) + w
    for f in FEATURE_FAMILIES:
        w = fam_w.get(f, 0.0)
        mean = (fam_vals[f] / w) if w > 0 else None
        names += [f"{f}_dev", f"{f}_weight"]
        # Deviation from the ensemble mean rather than the absolute temperature:
        # absolute values make the model learn climate (Singapore is warm than
        # Helsinki) instead of learning ERROR, and climate is not what is being
        # predicted here.
        values += [(mean - ens_mean) if mean is not None else 0.0, w / tw]

    # Ensemble-level.
    names += ["ensemble_mean", "spread_sd", "family_count", "member_count",
              "agreement", "lead_hours", "is_high", "bucket_width",
              "mean_native_timestep"]
    steps = [native_timestep(m) for m in temps if native_timestep(m)]
    lo, hi = row.get("bucket_low"), row.get("bucket_high")
    values += [
        ens_mean,
        row.get("weighted_spread_sd") or 0.0,
        len(fam_w),
        len(temps),
        row.get("model_agreement") or 0.0,
        row.get("lead_hours") or 0.0,
        1.0 if row.get("is_high") else 0.0,
        (hi - lo) if lo is not None and hi is not None else 999.0,
        (sum(steps) / len(steps)) if steps else 0.0,
    ]

    # Seasonal and diurnal context. Sine/cosine of day-of-year so December and
    # January are adjacent rather than 364 apart.
    td = row.get("target_date") or ""
    doy = 0
    if len(td) == 10:
        from datetime import date
        y, m, d = int(td[:4]), int(td[5:7]), int(td[8:10])
        doy = date(y, m, d).timetuple().tm_yday
    names += ["doy_sin", "doy_cos"]
    values += [math.sin(2 * math.pi * doy / 365.25),
               math.cos(2 * math.pi * doy / 365.25)]
    return names, values


# --- Inference --------------------------------------------------------------

def predict_quantiles(row):
    """Predicted settled value at each QUANTILE_LEVELS, or None.

    Sorted before returning: quantile regression fits each level independently,
    so nothing stops the 0.6 prediction landing below the 0.5 one on a given
    input. An unsorted CDF yields negative bucket probabilities, so the crossing
    is repaired here rather than trusted not to happen."""
    if not available():
        return None
    model = _load()
    if model is None:
        return None
    _, x = build_features(row)
    preds = [m.predict([x])[0] for m in model["boosters"]]
    return sorted(preds)


def bucket_probability(row, bucket_low, bucket_high):
    """P(settled value lands in the bucket), read off the interpolated CDF.

    Handles the lattice naturally: the CDF is evaluated at the real bucket edges
    rather than assuming a Gaussian around a point estimate."""
    qs = predict_quantiles(row)
    if not qs:
        return None
    lo = bucket_low - 0.5 if bucket_low is not None else -1e9
    hi = bucket_high + 0.5 if bucket_high is not None else 1e9
    return max(0.0, min(1.0, _cdf(qs, hi) - _cdf(qs, lo)))


def _cdf(quantiles, x):
    """Linear interpolation of the empirical CDF defined by the predicted
    quantiles. Flat outside the fitted range — the model has no view beyond its
    outermost quantile and pretending otherwise is how a tail gets priced at
    0.00008."""
    levels = QUANTILE_LEVELS
    if x <= quantiles[0]:
        return 0.0
    if x >= quantiles[-1]:
        return 1.0
    for i in range(len(quantiles) - 1):
        a, b = quantiles[i], quantiles[i + 1]
        if a <= x <= b:
            if b == a:
                return levels[i]
            t = (x - a) / (b - a)
            return levels[i] + t * (levels[i + 1] - levels[i])
    return 1.0


def _load():
    if "model" in _MODEL_CACHE:
        return _MODEL_CACHE["model"]
    try:
        import lightgbm as lgb
    except ImportError:
        logging.warning("ENABLE_QUANTILE_MODEL is set but lightgbm is not "
                        "installed — falling back to the parametric stack")
        _MODEL_CACHE["model"] = None
        return None
    try:
        with open(QUANTILE_MODEL_PATH) as fh:
            meta = json.load(fh)
        boosters = [lgb.Booster(model_str=s) for s in meta["boosters"]]
        _MODEL_CACHE["model"] = {"boosters": boosters, "features": meta["features"]}
    except Exception as e:
        logging.error(f"could not load quantile model: {e}")
        _MODEL_CACHE["model"] = None
    return _MODEL_CACHE["model"]


# --- Training ---------------------------------------------------------------

def train(rows, out_path=None, num_leaves=15, n_estimators=200,
          min_data_in_leaf=40):
    """Fit one booster per quantile level on the SETTLED value.

    Hyperparameters are deliberately small. On the sample sizes this project
    actually has, a deep forest memorises the training days — and days, not
    rows, are the unit of independence here (one synoptic pattern, a dozen
    cities). num_leaves=15 with min_data_in_leaf=40 is closer to a smoother
    than to a learner, which is the correct ambition until the sample grows.

    Refuses below MIN_TRAINING_ROWS. That is not caution for its own sake:
    three prior refits on ~23 observations each returned intervals wider than
    the correction, and an opaque model fitted on the same sample would repeat
    that failure with less visibility into it."""
    from config import QUANTILE_MIN_TRAINING_ROWS

    # Sample-size guard BEFORE the optional import. It is the cheaper and more
    # informative failure of the two, and "you have 23 rows" should not be
    # reported as "lightgbm is not installed" on a machine that never needed
    # lightgbm to learn it was not going to train anything.
    usable = [r for r in rows if r.get("settled_value") is not None]
    if len(usable) < QUANTILE_MIN_TRAINING_ROWS:
        raise ValueError(
            f"{len(usable)} settled rows, need >= {QUANTILE_MIN_TRAINING_ROWS}. "
            f"Three prior refits on ~23 observations each produced confidence "
            f"intervals wider than the correction they proposed; a "
            f"gradient-boosted model on the same sample is that failure with "
            f"more parameters. Let the replay log fill.")

    import lightgbm as lgb

    names, _ = build_features(usable[0])
    X = [build_features(r)[1] for r in usable]
    y = [float(r["settled_value"]) for r in usable]

    boosters = []
    for q in QUANTILE_LEVELS:
        m = lgb.LGBMRegressor(objective="quantile", alpha=q,
                              num_leaves=num_leaves, n_estimators=n_estimators,
                              min_child_samples=min_data_in_leaf, verbose=-1)
        m.fit(X, y)
        boosters.append(m.booster_.model_to_string())

    out_path = out_path or QUANTILE_MODEL_PATH
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"features": names, "levels": list(QUANTILE_LEVELS),
                   "boosters": boosters, "n_train": len(usable)}, fh)
    logging.info(f"trained {len(boosters)} quantile boosters on {len(usable)} "
                 f"rows -> {out_path}")
    return out_path


def main():
    """Train, then REPORT rather than enable.

    Deliberately does not flip ENABLE_QUANTILE_MODEL. The gate is
    harness.py showing this beats the current stack out-of-sample, and a
    training script that enables its own output is a training script that
    will eventually enable a worse model."""
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from replay import load_rows
    rows = load_rows(since=args.since)
    try:
        path = train(rows, out_path=args.out)
    except ValueError as e:
        print(f"NOT TRAINED: {e}")
        return 1
    except ImportError:
        print("lightgbm is not installed. pip install lightgbm")
        return 1
    print(f"trained -> {path}")
    print("\nStill DISABLED. Set ENABLE_QUANTILE_MODEL=true only after "
          "harness.py shows this beating the parametric stack on the purged, "
          "grouped, walk-forward split.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
