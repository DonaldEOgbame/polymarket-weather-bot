"""Per-city sigma calibration from the replay log (owner request 2026-08-12).

Fits a per-city, per-direction sigma scale k so that z = (actual - mean)/sigma
has RMS 1, from `replay_signals` cells scored against settlement-grade actuals
(metar.day_extremes_f — the ruler the markets pay on). Prints a paste-ready
CITY_SIGMA_SCALES table; NEVER writes config (repo convention: calibration
constants move only in a commit, with provenance).

Method notes, in the order they bit previous calibrations:
- Substrate is replay_signals, not model_accuracy: model_accuracy only grows
  for TRADED markets (~1 city-day per trade); the replay log records every
  scanned market.
- One cell per (city_key, target_date, is_high): the EARLIEST row whose TRUE
  time-to-resolution (civil day end minus scan time — stored lead_hours
  carries the pre-5e0bce4 midnight-UTC bug) is inside the 16h trading
  window. That is the snapshot the bot would have ENTERED on. The first cut
  of this script took the LATEST in-window row instead and fitted k~0.57:
  a scan minutes before day end has effectively observed the outcome, so
  late snapshots make any sigma look 2x too wide. Entry-time sigma is what
  prices the trade; calibrate that.
- Cells where the MIN/MAX_SIGMA_F clamp bit are EXCLUDED: a clamped sigma
  carries no information about the scale that produced it.
- Residuals are computed against sigma_post_spread (base + coef*spread,
  BEFORE the direction scale and BEFORE convective inflation), so the fitted
  k per city ABSORBS CONVECTIVE_STD_INFLATION — at runtime a city with a
  fitted scale skips the convective multiplier (see weather.compute_sigma_stages).
- n ~= 7 cells/city: per-city point estimates are noise (config.py's own
  SIGMA_SCALE_LOW history). Scales are shrunk toward the global re-fit:
      k_c^2 = (n_c * k_hat_c^2 + N0 * k_g^2) / (n_c + N0)
  LOW cells exist for only ~8 cities at n~1 — no per-city LOW fits; the
  global SIGMA_SCALE_LOW re-fit is reported instead.
- Actuals are persisted to `city_day_actuals` (created here) so re-runs
  don't refetch; the network step is one batched IEM call per station
  (metar.prewarm_day_extremes). HKO cities are month-batched inside metar.

Usage:
    DB_PATH=backups/bot-YYYY-MM-DD.db python3 calibrate_city_sigma.py
    ... --n0 14            # shrinkage prior strength (pseudo-cells)
    ... --score-only       # report z-stats under CURRENT config, fit nothing
"""
import argparse
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone

import config as C
import metar
from db import fetch_query, execute_query

MIN_LOW_CITIES_FOR_TABLE = 5  # below this, LOW stays a single global scale


def _ensure_actuals_table():
    execute_query("""
        CREATE TABLE IF NOT EXISTS city_day_actuals (
            city_key TEXT NOT NULL,
            target_date TEXT NOT NULL,
            max_f REAL, min_f REAL,
            fetched_at TEXT,
            PRIMARY KEY (city_key, target_date))""")


def _load_actuals():
    return {(r["city_key"], r["target_date"]): (r["max_f"], r["min_f"])
            for r in fetch_query("SELECT * FROM city_day_actuals")}


def backfill_actuals(city_days):
    """Fetch settlement-grade (max_f, min_f) for every (city, date) not yet
    persisted. One IEM range call per station, then per-day cache hits."""
    _ensure_actuals_table()
    have = _load_actuals()
    need = sorted(cd for cd in city_days if cd not in have)
    if not need:
        return have
    by_city = defaultdict(list)
    for ck, d in need:
        by_city[ck].append(d)
    for ck, days in sorted(by_city.items()):
        icao, tz = metar.get_station(ck)
        if not icao:
            print(f"  actuals: no station for {ck!r}, skipped", file=sys.stderr)
            continue
        if ck not in metar.HKO_CITIES:
            metar.prewarm_day_extremes(icao, tz, min(days), max(days))
        for d in days:
            mx, mn = metar.day_extremes_f(ck, d)
            if mx is None and mn is None:
                continue  # day incomplete or gap — never persist emptiness
            execute_query(
                "INSERT OR REPLACE INTO city_day_actuals "
                "(city_key, target_date, max_f, min_f, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ck, d, mx, mn, datetime.now(timezone.utc).isoformat()))
            have[(ck, d)] = (mx, mn)
    return have


def _true_hours(row):
    """Civil-day-end hours at scan time — NOT the stored lead_hours, which on
    pre-5e0bce4 rows measures to midnight UTC (up to ~29h short for US
    cities)."""
    from scanner import get_target_day_end_utc
    end = get_target_day_end_utc(row["target_date"], row["city_key"])
    ts = datetime.fromisoformat(row["timestamp"])
    return (end - ts).total_seconds() / 3600.0


def load_cells():
    """One ENTRY-TIME replay row per (city_key, target_date, is_high).

    The earliest row whose true time-to-resolution is inside the trading
    window — the snapshot the bot would have entered on. Cells with no
    in-window row at all are dropped (nothing tradeable to calibrate).
    Clamp-bit cells are excluded downstream (reported)."""
    rows = fetch_query("""
        SELECT city_key, target_date, is_high, lead_hours, timestamp,
               ensemble_mean, weighted_spread_sd, sigma_post_spread,
               sigma_post_convective, sigma_post_clamp, config_fingerprint
          FROM replay_signals
         WHERE ensemble_mean IS NOT NULL AND sigma_post_spread IS NOT NULL
         ORDER BY timestamp""")
    cells = {}
    for r in rows:
        key = (r["city_key"], r["target_date"], bool(r["is_high"]))
        if key in cells:
            continue  # rows are timestamp-ordered: first in-window row wins
        th = _true_hours(r)
        if 0.0 < th <= C.MAX_HOURS_TO_RESOLUTION:
            cells[key] = dict(r)
    return cells


def _rms(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else float("nan")


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def build_residuals(cells, actuals):
    """r = (actual - ensemble_mean) / sigma_post_spread per usable cell."""
    out = []          # (city, date, is_high, r, z_current)
    excluded = defaultdict(int)
    for (ck, d, is_high), row in sorted(cells.items()):
        a = actuals.get((ck, d))
        if not a:
            excluded["no_actual"] += 1
            continue
        actual = a[0] if is_high else a[1]
        if actual is None:
            excluded["no_actual"] += 1
            continue
        post_spread = row["sigma_post_spread"]
        post_conv = row["sigma_post_convective"] or post_spread
        clamped = row["sigma_post_clamp"]
        if post_spread is None or clamped is None or post_spread <= 0:
            excluded["no_sigma"] += 1
            continue
        # clamp bit in either direction: the stored final sigma is not the
        # formula's output, so this cell cannot inform the scale
        if clamped >= C.MAX_SIGMA_F - 1e-3 or (
                clamped <= C.MIN_SIGMA_F + 1e-3 and post_conv > clamped):
            excluded["clamped"] += 1
            continue
        err = actual - row["ensemble_mean"]
        out.append((ck, d, is_high, err / post_spread,
                    err / max(clamped, 1e-9)))
    return out, excluded


def fit(residuals, n0):
    highs = [x for x in residuals if x[2]]
    lows = [x for x in residuals if not x[2]]
    k_g_high = _rms([x[3] for x in highs])
    k_g_low = _rms([x[3] for x in lows])

    per_city = {}
    by_city = defaultdict(list)
    for ck, d, ih, r, _z in highs:
        by_city[ck].append(r)
    for ck, rs in sorted(by_city.items()):
        k_hat = _rms(rs)
        k_shrunk = math.sqrt((len(rs) * k_hat ** 2 + n0 * k_g_high ** 2)
                             / (len(rs) + n0))
        per_city[ck] = {"n": len(rs), "k_hat": k_hat, "k": k_shrunk,
                        "mean_r": _mean(rs)}
    return k_g_high, k_g_low, per_city


def zstats(residuals, scale_of):
    """mean(z)/std-rms(z) with sigma = scale(city, is_high) * post_spread."""
    zs = []
    for ck, d, ih, r, _z in residuals:
        k = scale_of(ck, ih)
        if k and k > 0:
            zs.append(r / k)
    return _mean(zs), _rms(zs), len(zs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n0", type=float, default=14.0,
                    help="shrinkage prior strength in pseudo-cells")
    ap.add_argument("--score-only", action="store_true",
                    help="report z under current config; fit nothing")
    args = ap.parse_args()

    cells = load_cells()
    print(f"cells: {len(cells)} "
          f"({sum(1 for k in cells if k[2])} HIGH / "
          f"{sum(1 for k in cells if not k[2])} LOW) from "
          f"{len({k[0] for k in cells})} cities, "
          f"dates {min(k[1] for k in cells)}..{max(k[1] for k in cells)}")
    fps = sorted({r['config_fingerprint'] for r in cells.values()
                  if r['config_fingerprint']})
    print(f"config fingerprints present: {len(fps)}")

    actuals = backfill_actuals({(k[0], k[1]) for k in cells})
    residuals, excluded = build_residuals(cells, actuals)
    print(f"usable residuals: {len(residuals)}  excluded: {dict(excluded)}")

    cur = zstats(residuals, lambda ck, ih:
                 (C.SIGMA_SCALE_HIGH if ih else C.SIGMA_SCALE_LOW)
                 * (C.CONVECTIVE_STD_INFLATION if ck in C.CONVECTIVE_CITIES
                    else 1.0))
    print(f"\n(a) CURRENT scales      : mean(z)={cur[0]:+.3f}  rms(z)={cur[1]:.3f}  n={cur[2]}")
    if args.score_only:
        return

    k_g_high, k_g_low, per_city = fit(residuals, args.n0)
    glb = zstats(residuals, lambda ck, ih: k_g_high if ih else k_g_low)
    shr = zstats(residuals, lambda ck, ih:
                 (per_city[ck]["k"] if ih and ck in per_city
                  else (k_g_high if ih else k_g_low)))
    print(f"(b) fitted GLOBAL       : mean(z)={glb[0]:+.3f}  rms(z)={glb[1]:.3f}  n={glb[2]}")
    print(f"(c) shrunk PER-CITY     : mean(z)={shr[0]:+.3f}  rms(z)={shr[1]:.3f}  n={shr[2]}")

    # honest-but-weak holdout: last full target date
    last = max(k[1] for k in cells)
    train = [x for x in residuals if x[1] != last]
    test = [x for x in residuals if x[1] == last]
    if train and test:
        kgh, kgl, pc = fit(train, args.n0)
        ho = zstats(test, lambda ck, ih:
                    (pc[ck]["k"] if ih and ck in pc else (kgh if ih else kgl)))
        print(f"holdout {last}         : mean(z)={ho[0]:+.3f}  rms(z)={ho[1]:.3f}  n={ho[2]}")

    print(f"\nglobal HIGH k = {k_g_high:.3f}  (current SIGMA_SCALE_HIGH = {C.SIGMA_SCALE_HIGH})")
    print(f"global LOW  k = {k_g_low:.3f}  (current SIGMA_SCALE_LOW  = {C.SIGMA_SCALE_LOW}, "
          f"n={sum(1 for x in residuals if not x[2])} — global only, per-city LOW is noise)")

    print("\nper-city HIGH scales (k_hat raw -> k shrunk toward global, n0=%g):" % args.n0)
    for ck, v in sorted(per_city.items()):
        conv = "  [absorbs convective 1.3x]" if ck in C.CONVECTIVE_CITIES else ""
        print(f"  {ck:<14} n={v['n']:>2}  mean_r={v['mean_r']:+.2f}  "
              f"k_hat={v['k_hat']:.3f}  ->  k={v['k']:.3f}{conv}")

    env = ",".join(f"{ck}:{v['k']:.3f}:0" for ck, v in sorted(per_city.items()))
    print("\npaste-ready (city:high:low, 0 = no fitted value, global fallback):")
    print(f'CITY_SIGMA_SCALES="{env}"')


if __name__ == "__main__":
    main()
