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
from metar import fetch_day_extremes, get_station, round_half_away

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def _reconstruct_mean(raw_models: dict, region: str):
    """Weighted ensemble mean over the models present, using the region weights.
    Returns None if no known-weighted model is present."""
    weights = WEIGHTS.get(region, {})
    total = sum(weights[m] for m in raw_models if m in weights)
    if total == 0:
        return None
    return sum(t * (weights[m] / total) for m, t in raw_models.items() if m in weights)


def _fetch_actuals_metar(city_key, date_str, cache):
    """Return (actual_max_F, actual_min_F) from the METAR observation feed — the SAME
    source Polymarket resolves against — rounded to whole °C then converted to °F.
    (None, None) if the station is unmapped or the day isn't published yet."""
    key = ("metar", city_key, date_str)
    if key in cache:
        return cache[key]
    icao, tz = get_station(city_key)
    result = (None, None)
    if icao:
        mx_c, mn_c = fetch_day_extremes(icao, tz, date_str)
        def to_f(c):
            return round_half_away(c) * 9.0 / 5.0 + 32.0 if c is not None else None
        result = (to_f(mx_c), to_f(mn_c))
    cache[key] = result
    return result


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
    ap.add_argument("--source", choices=("metar", "era5"), default="metar",
                    help="Actuals source. 'metar' (default) is the SAME feed Polymarket "
                         "resolves against — use it to measure real-settlement calibration. "
                         "'era5' uses Open-Meteo's archive (differs by up to ~1°C).")
    args = ap.parse_args()
    print(f"Actuals source: {args.source.upper()}"
          + ("  (Polymarket's resolution ruler)" if args.source == "metar"
             else "  (ERA5 reanalysis — NOT the resolution source)"))

    where, params = "raw_models IS NOT NULL", ()
    if args.days is not None:
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        where += " AND target_date >= date(?, ?)"
        params = (cutoff, f"-{args.days} days")

    signals = fetch_query(
        f"SELECT id, market_id, city, target_date, bucket_low, bucket_high, model_prob, "
        f"ensemble_std, raw_models FROM signals WHERE {where} ORDER BY target_date",
        params,
    )
    if not signals:
        print("No signals found. Run the scanner first, then come back.")
        return

    # Canonical bucket per market_id: use the LAST (most recent) bucket_low/high
    # logged for each market_id, not whatever happens to be on any given row.
    # A market's bucket can legitimately change over its scan history if the
    # parser itself changes (as it did for the 2026-06 Celsius zero-width bug) —
    # mixing old and new bucket definitions for the same market silently
    # contaminates the reliability/Brier numbers below (measured impact: ~29%
    # of resolved rows affected, Brier score understated by ~0.015 / ~10%
    # relative in the DB this was found against). Rows are already ORDER BY
    # target_date, not by id, so re-derive the canonical bucket explicitly
    # rather than assume row order reflects recency.
    # "Most recent" must be derived from the signal id (insertion order), not from
    # iteration order of a target_date-sorted result — within one market all rows
    # share a target_date, so the old "last row wins" was an arbitrary tie-break
    # that could just as easily crown the PRE-fix bucket as canonical.
    canonical_bucket, canonical_id = {}, {}
    for s in signals:
        mid = s["market_id"]
        if mid and s["id"] >= canonical_id.get(mid, -1):
            canonical_id[mid] = s["id"]
            canonical_bucket[mid] = (s["bucket_low"], s["bucket_high"])
    # Stale-bucket rows are EXCLUDED from reliability/Brier, not rescored: their
    # stored model_prob was computed for the old bucket definition, so scoring it
    # against the canonical bucket's outcome compares a prediction and an outcome
    # of two different events.
    stale_bucket_rows = 0
    for s in signals:
        mid = s["market_id"]
        s["stale_bucket"] = False
        if mid and mid in canonical_bucket:
            if (s["bucket_low"], s["bucket_high"]) != canonical_bucket[mid]:
                stale_bucket_rows += 1
                s["stale_bucket"] = True

    session = get_session()
    archive_cache = {}

    # Which daily field (high vs low) each market targets, from ground truth where
    # we have it. The closest-to-mean fallback below mislabels outcomes exactly on
    # big forecast busts (a badly-missed high can sit closer to the actual LOW),
    # scoring the worst misses against the wrong field.
    import re as _re
    is_high_map = {}
    for r in fetch_query("SELECT market_id, question FROM markets WHERE question IS NOT NULL"):
        ql = (r["question"] or "").lower()
        # First-occurrence tie-break, matching scanner.py's classification.
        low_m = _re.search(r"\b(lowest|minimum|low|cold|coolest)\b", ql)
        high_m = _re.search(r"\b(highest|maximum|high|warm|hottest)\b", ql)
        if low_m and high_m:
            is_high_map[r["market_id"]] = high_m.start() <= low_m.start()
        elif low_m or high_m:
            is_high_map[r["market_id"]] = bool(high_m)
    for r in fetch_query("SELECT market_id, is_high FROM trades WHERE is_high IS NOT NULL"):
        is_high_map[r["market_id"]] = bool(r["is_high"])

    z_scores = []                       # sigma calibration (one per unique city/date forecast)
    seen_forecast = set()
    reliability = defaultdict(lambda: [0, 0.0, 0])   # bin -> [hits, sum_pred, n]
    brier_terms = []
    market_brier = {}                   # market_id -> brier of its last resolved row
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
        elif args.source == "metar":
            actual_hi, actual_lo = _fetch_actuals_metar(city, date_str, archive_cache)
        else:
            actual_hi, actual_lo = _fetch_actuals(coords, date_str, session, archive_cache)
        if actual_hi is None and actual_lo is None:
            pending += 1
            continue

        # Recover which field this forecast targeted: prefer the market's known
        # high/low orientation; fall back to closest-to-mean only when unknown.
        known_high = is_high_map.get(s["market_id"])
        if known_high is True and actual_hi is not None:
            actual = actual_hi
        elif known_high is False and actual_lo is not None:
            actual = actual_lo
        else:
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

        # reliability uses the stored model_prob against realized bucket outcome.
        # Stale-bucket rows are excluded — their prob refers to a different bucket.
        outcome = 1.0 if _in_bucket(actual, s["bucket_low"], s["bucket_high"]) else 0.0
        p = s["model_prob"]
        if p is not None and not s["stale_bucket"]:
            brier_terms.append((p - outcome) ** 2)
            # Per-market Brier (last resolved row wins): the per-row Brier weights a
            # market by how often it was scanned, so churned markets dominate it.
            if s["market_id"]:
                market_brier[s["market_id"]] = (p - outcome) ** 2
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
    if stale_bucket_rows:
        print(f"stale-bucket rows excluded from Brier/reliability: {stale_bucket_rows} "
              f"({stale_bucket_rows / len(signals):.1%} of all rows) — their bucket_low/high "
              f"differs from this market_id's most recent (canonical) value, so their "
              f"model_prob refers to a different bucket than the outcome being scored")

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
        print(f"  Brier (per-row)  : {brier:.4f}  (lower is better; 0.25 = coin flip; "
              f"weighted by scan frequency)")
    if market_brier:
        mb = sum(market_brier.values()) / len(market_brier)
        print(f"  Brier (per-mkt)  : {mb:.4f}  over {len(market_brier)} markets "
              f"(last signal per market — immune to scan-frequency weighting)")
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
