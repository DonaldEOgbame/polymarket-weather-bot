"""
Calibration harness — answers the only question that matters: are the model's
probabilities trustworthy, or is the bot manufacturing fake edge?

It takes every logged signal, reconstructs the ensemble forecast from the stored
per-model temps, fetches the *realized* temperature from Open-Meteo's archive, and
reports three diagnostics:

  1. SIGMA CALIBRATION — z = (actual - ensemble_mean) / ensemble_std.
     If sigma is honest, z ~ N(0, 1), so std(z) ≈ 1.0.
       std(z) > 1  → sigma too TIGHT → probabilities overconfident → fake edge.
       std(z) < 1  → sigma too WIDE  → probabilities too timid → missed edge.
     The reported "sigma scale factor" is what you'd multiply BASE_FORECAST_ERROR
     by to make the spread match reality.

  2. PROBABILITY RELIABILITY — bin predicted bucket prob vs observed hit rate,
     plus an overall Brier score. This is the direct test of "when it says 30%,
     does it happen 30% of the time?"

  3. PER-MODEL ACCURACY — MAE and signed bias for each NWP model, so weight
     tables and bias corrections can be tuned against ground truth.

Usage:
    python calibrate.py                 # all signals in the DB
    python calibrate.py --days 30       # signals whose target_date is within N days
    python calibrate.py --no-fetch      # only use already-logged model_accuracy rows

Archive note: Open-Meteo's ERA5 archive lags real time by ~5 days, so very recent
target dates resolve as "pending" and are excluded until the data lands.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

from db import fetch_query
from weather import WEIGHTS, get_station_coords
from utils import get_session

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def _reconstruct_mean(raw_models: dict, region: str):
    """Weighted ensemble mean over the models present, using the region weights.
    Returns None if no known-weighted model is present."""
    weights = WEIGHTS.get(region, {})
    total = sum(weights[m] for m in raw_models if m in weights)
    if total == 0:
        return None
    return sum(t * (weights[m] / total) for m, t in raw_models.items() if m in weights)


def _fetch_actuals(coords, date_str, session, cache):
    """Return (actual_max, actual_min) for a city/date from the archive, or
    (None, None) if not yet available. Cached per (lat, lon, date)."""
    key = (coords["lat"], coords["lon"], date_str)
    if key in cache:
        return cache[key]
    params = {
        "latitude": coords["lat"],
        "longitude": coords["lon"],
        "start_date": date_str,
        "end_date": date_str,
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
    }
    result = (None, None)
    try:
        resp = session.get(ARCHIVE_URL, params=params, timeout=20)
        if resp.status_code == 200:
            daily = resp.json().get("daily", {})
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])
            hi = highs[0] if highs and highs[0] is not None else None
            lo = lows[0] if lows and lows[0] is not None else None
            result = (hi, lo)
    except Exception as e:
        print(f"  ! archive fetch failed for {date_str} @ {coords['lat']},{coords['lon']}: {e}",
              file=sys.stderr)
    cache[key] = result
    return result


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bucket_prob(mean, std, lb, ub):
    """Mirror weather.get_bucket_probability: ±0.5°F padding, open-ended bounds."""
    std = max(std, 0.5)
    lo = (lb - 0.5) if lb is not None else -1000.0
    hi = (ub + 0.5) if ub is not None else 1000.0
    return max(0.0, min(1.0, _norm_cdf((hi - mean) / std) - _norm_cdf((lo - mean) / std)))


def _in_bucket(actual, lb, ub):
    lo = (lb - 0.5) if lb is not None else -1e9
    hi = (ub + 0.5) if ub is not None else 1e9
    return lo <= actual <= hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None,
                    help="Only signals whose target_date is within N days of today.")
    ap.add_argument("--no-fetch", action="store_true",
                    help="Skip archive fetches; only summarize what already resolves.")
    args = ap.parse_args()

    where, params = "raw_models IS NOT NULL", ()
    if args.days is not None:
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        where += " AND target_date >= date(?, ?)"
        params = (cutoff, f"-{args.days} days")

    signals = fetch_query(
        f"SELECT city, target_date, bucket_low, bucket_high, model_prob, ensemble_std, "
        f"raw_models FROM signals WHERE {where} ORDER BY target_date",
        params,
    )
    if not signals:
        print("No signals found. Run the scanner first, then come back.")
        return

    session = get_session()
    archive_cache = {}

    z_scores = []                       # sigma calibration (one per unique city/date forecast)
    seen_forecast = set()
    reliability = defaultdict(lambda: [0, 0.0, 0])   # bin -> [hits, sum_pred, n]
    brier_terms = []
    per_model = defaultdict(lambda: [0.0, 0.0, 0])   # model -> [sum_abs_err, sum_signed_err, n]

    resolved = pending = unmapped = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for s in signals:
        city, date_str = s["city"], s["target_date"]
        _, coords = get_station_coords(city or "")
        if not coords:
            unmapped += 1
            continue
        region = coords["region"]
        try:
            raw = json.loads(s["raw_models"] or "{}")
        except Exception:
            raw = {}
        if not raw:
            continue

        mean = _reconstruct_mean(raw, region)
        if mean is None:
            continue

        if args.no_fetch or date_str > today:
            actual_hi, actual_lo = (None, None)
        else:
            actual_hi, actual_lo = _fetch_actuals(coords, date_str, session, archive_cache)
        if actual_hi is None and actual_lo is None:
            pending += 1
            continue

        # Recover which field this forecast targeted: pick the actual closest to
        # the reconstructed ensemble mean (forecasts cluster around their own field).
        cands = [v for v in (actual_hi, actual_lo) if v is not None]
        actual = min(cands, key=lambda v: abs(v - mean))
        resolved += 1

        std = s["ensemble_std"] or 0.5
        # one z per distinct forecast (city/date/field), not per bucket
        fkey = (city, date_str, round(actual, 1))
        if fkey not in seen_forecast:
            seen_forecast.add(fkey)
            z_scores.append((actual - mean) / max(std, 0.5))
            for m, t in raw.items():
                pm = per_model[m]
                pm[0] += abs(t - actual)
                pm[1] += (t - actual)
                pm[2] += 1

        # reliability uses the stored model_prob against realized bucket outcome
        outcome = 1.0 if _in_bucket(actual, s["bucket_low"], s["bucket_high"]) else 0.0
        p = s["model_prob"]
        if p is not None:
            brier_terms.append((p - outcome) ** 2)
            b = reliability[min(int(p * 10), 9)]
            b[0] += outcome
            b[1] += p
            b[2] += 1

    # ---------- report ----------
    print("=" * 64)
    print("CALIBRATION REPORT")
    print("=" * 64)
    print(f"signals examined : {len(signals)}")
    print(f"resolved         : {resolved}")
    print(f"pending (archive lag / future): {pending}")
    print(f"unmapped city    : {unmapped}")

    if resolved == 0:
        print("\nNothing resolved yet — ERA5 archive lags ~5 days. Re-run once your")
        print("target dates are >5 days in the past, or backfill with older signals.")
        return

    # 1. sigma calibration
    print("\n--- 1. SIGMA CALIBRATION (z = (actual - mean) / sigma) ---")
    n = len(z_scores)
    mz = sum(z_scores) / n
    sz = math.sqrt(sum((z - mz) ** 2 for z in z_scores) / n) if n > 1 else float("nan")
    print(f"  forecasts scored : {n}")
    print(f"  mean(z)          : {mz:+.3f}   (≠0 → systematic warm/cold bias)")
    print(f"  std(z)           : {sz:.3f}    (1.0 = perfectly calibrated spread)")
    if n >= 8 and not math.isnan(sz):
        if sz > 1.15:
            print(f"  VERDICT: sigma is TOO TIGHT — overconfident. Scale BASE_FORECAST_ERROR")
            print(f"           by ~{sz:.2f}x. Current 'edges' are partly illusory.")
        elif sz < 0.85:
            print(f"  VERDICT: sigma is TOO WIDE — timid. You can scale BASE_FORECAST_ERROR")
            print(f"           by ~{sz:.2f}x to surface more real edge.")
        else:
            print("  VERDICT: sigma is well-calibrated. Trust the probabilities.")
    else:
        print("  (need >=8 resolved forecasts for a reliable verdict)")

    # 2. probability reliability + Brier
    print("\n--- 2. PROBABILITY RELIABILITY ---")
    if brier_terms:
        brier = sum(brier_terms) / len(brier_terms)
        print(f"  Brier score      : {brier:.4f}  (lower is better; 0.25 = coin flip)")
    print("  pred-bin   n   predicted   observed")
    for b in range(10):
        hits, spred, cnt = reliability[b]
        if cnt:
            print(f"  {b/10:.1f}-{(b+1)/10:.1f}  {cnt:>4}    {spred/cnt:6.1%}     {hits/cnt:6.1%}")
    print("  (predicted ≈ observed per row → calibrated. predicted > observed → overconfident.)")

    # 3. per-model accuracy
    print("\n--- 3. PER-MODEL ACCURACY (vs realized) ---")
    print("  model            n     MAE    bias")
    for m, (abs_e, sgn_e, cnt) in sorted(per_model.items(), key=lambda kv: kv[1][0] / max(kv[1][2], 1)):
        if cnt:
            print(f"  {m:<15} {cnt:>4}  {abs_e/cnt:5.2f}°F  {sgn_e/cnt:+5.2f}°F")
    print("  (bias>0 → model runs warm vs actual; feed into GFS_BIAS_CORRECTIONS etc.)")


if __name__ == "__main__":
    main()
