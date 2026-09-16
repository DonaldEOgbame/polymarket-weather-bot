#!/usr/bin/env python3
"""
Backtest of the owner's 2026-09-15 LOW-market specification:

  1. LOW markets only (daily minimum temperature).
  2. Entry price band 0.50-0.60.
  3. Forecast clears the bucket by >= FORECAST_CLEAR_F (4.5F default).
  4. Margin at entry still >= ENTRY_MARGIN_F (4.0F default).
  5. Forecast used must be issued FOR THAT DAY (same-day run).
  6. Same-day settlement.

NO-LOOK-AHEAD DESIGN
--------------------
The bot's own replay_signals/signals tables were wiped in the sniper-era DB
reset, so there are no stored historical forecasts to replay. Everything here
is rebuilt from primary sources instead, which is actually stricter -- none of
it was selected by the old gates, so there is no in-sample contamination:

  * FORECAST: Open-Meteo previous-runs API, hourly `temperature_2m_previous_day1`
    -- the forecast as it stood ONE DAY BEFORE the target date. The daily min is
    derived from the hours of the station-local civil day. This is the causal,
    as-issued forecast; it is NOT the reanalysis value.
  * TRUTH: IEM ASOS observations for the station-local day, quantised through
    the repo's own lattice.quantise_c so "what settled" matches what the bot
    would compute (same ruler, same rounding).
  * PRICE: Polymarket's Data API trade tape. Entry price is a REAL PRINT in the
    0.50-0.60 band, not a mid or a model. We take the first qualifying print
    after the entry cutoff.

The known gap, stated plainly: this reconstructs a single-model (best-available
Open-Meteo blend) forecast, not the bot's weighted multi-model ensemble with
Platt calibration. It therefore measures whether the RULE SET selects winners,
not whether the bot's specific ensemble would. Treat the win rate as indicative
of the gate geometry, and re-measure in paper before trusting the number.
"""
import os
import sys
import json
import csv
import io
import math
import statistics
from datetime import datetime, timezone, timedelta, date as _date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import safe_get                      # noqa: E402
from metar import STATION_ICAO, _iem_station, MESONET_URL   # noqa: E402
from lattice import quantise_c                  # noqa: E402
from scanner import parse_bucket, parse_market_direction    # noqa: E402

FORECAST_CLEAR_F = float(os.environ.get("FORECAST_CLEAR_F", "4.5"))
ENTRY_MARGIN_F = float(os.environ.get("ENTRY_MARGIN_F", "4.0"))
MIN_PX = float(os.environ.get("MIN_PX", "0.50"))
MAX_PX = float(os.environ.get("MAX_PX", "0.60"))
PAD_F = 0.5          # BUCKET_EDGE_PAD_F
STAKE = 3.0

OM_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
TRADES = "https://data-api.polymarket.com/trades"

_COORD = {}


def station_coords(city):
    """Lat/lon for a city, from the repo's own station table."""
    if city in _COORD:
        return _COORD[city]
    from weather import STATIONS
    s = STATIONS.get(city)
    if not s:
        _COORD[city] = (None, None)
    else:
        _COORD[city] = (s.get("lat"), s.get("lon"))
    return _COORD[city]


def forecast_min_asof(city, target_date, tz):
    """Daily MIN forecast for target_date as issued ~1 day earlier (causal)."""
    lat, lon = station_coords(city)
    if lat is None:
        return None
    r = safe_get(OM_URL, params={
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m_previous_day1",
        "start_date": target_date, "end_date": target_date,
        "temperature_unit": "fahrenheit", "timezone": tz,
    }, timeout=40)
    if r.status_code != 200:
        return None
    h = r.json().get("hourly", {})
    vals = [v for v in (h.get("temperature_2m_previous_day1") or []) if v is not None]
    return min(vals) if vals else None


def actual_min_f(icao, tz, target_date, city):
    """Settled daily minimum, on the repo's own quantisation grid."""
    y, m, d = (int(x) for x in target_date.split("-"))
    nd = _date(y, m, d) + timedelta(days=1)
    r = safe_get(MESONET_URL, params={
        "station": _iem_station(icao), "data": "tmpc",
        "year1": y, "month1": m, "day1": d,
        "year2": nd.year, "month2": nd.month, "day2": nd.day,
        "tz": tz, "format": "onlycomma", "latlon": "no", "missing": "M",
    }, timeout=60)
    if r.status_code != 200:
        return None
    temps = []
    for row in csv.DictReader(io.StringIO(r.text)):
        if row.get("valid", "")[:10] != target_date:
            continue
        v = row.get("tmpc", "M")
        if v not in ("M", ""):
            try:
                temps.append(float(v))
            except ValueError:
                pass
    if not temps:
        return None
    return quantise_c(min(temps), city)


def bucket_clear_f(fmin, lo, hi):
    """How many °F the forecast min sits OUTSIDE the padded bucket.

    Positive = forecast says the min misses the bucket (a NO bet). Returns the
    distance to the NEAREST padded edge, which is the binding one -- the same
    quantity strategy.forecast_margin_ok gates on.
    """
    plo = (lo - PAD_F) if lo is not None else None
    phi = (hi + PAD_F) if hi is not None else None
    if plo is not None and phi is not None:
        if fmin < plo:
            return plo - fmin
        if fmin > phi:
            return fmin - phi
        return -min(fmin - plo, phi - fmin)     # inside the bucket
    if phi is not None:                          # "below X" market
        return (fmin - phi) if fmin > phi else -(phi - fmin)
    if plo is not None:                          # "above X" market
        return (plo - fmin) if fmin < plo else -(fmin - plo)
    return None


def settled_no_wins(actual, lo, hi):
    plo = (lo - PAD_F) if lo is not None else float("-inf")
    phi = (hi + PAD_F) if hi is not None else float("inf")
    return not (plo <= actual <= phi)


def load_markets(db_path, days):
    import sqlite3
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    q = ("SELECT DISTINCT market_id,question,city,target_date,bucket_low,bucket_high "
         "FROM markets WHERE target_date IN (%s)" % ",".join("?" * len(days)))
    return c.execute(q, days).fetchall()


def first_print_in_band(cond_id, token_no, t_from, t_to):
    """First real NO-side print inside the price band within the window."""
    r = safe_get(TRADES, params={"market": cond_id, "limit": 500}, timeout=40)
    if r.status_code != 200:
        return None
    try:
        rows = r.json()
    except Exception:
        return None
    best = None
    for t in rows:
        if t.get("conditionId") != cond_id:
            continue
        ts = t.get("timestamp")
        if ts is None or not (t_from <= ts <= t_to):
            continue
        # Price is quoted for the asset traded; only count the NO token.
        if t.get("asset") != token_no:
            continue
        px = float(t.get("price") or 0)
        if MIN_PX <= px <= MAX_PX:
            if best is None or ts < best[0]:
                best = (ts, px, float(t.get("size") or 0))
    return best


def main():
    import sqlite3
    db = os.environ.get("DB", "backups/bot-20260913T164201Z.db")
    ndays = int(os.environ.get("NDAYS", "8"))
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(2, 2 + ndays)]

    rows = load_markets(db, days)
    print(f"markets in window ({days[-1]}..{days[0]}): {len(rows)}", flush=True)

    # LOW markets only -- owner instruction 2026-09-15.
    lows = []
    for r in rows:
        is_low, is_high = parse_market_direction(r["question"] or "")
        if is_low and not is_high:
            lows.append(r)
    print(f"LOW markets: {len(lows)}", flush=True)

    fc_cache, act_cache = {}, {}
    stats = {"no_station": 0, "no_forecast": 0, "no_actual": 0,
             "clear_fail": 0, "no_print": 0, "traded": 0}
    trades = []
    near_misses = []

    for i, r in enumerate(lows):
        city, td = r["city"], r["target_date"]
        lo, hi = r["bucket_low"], r["bucket_high"]
        st = STATION_ICAO.get(city)
        if not st:
            stats["no_station"] += 1
            continue
        icao, tz = st

        if (city, td) not in fc_cache:
            fc_cache[(city, td)] = forecast_min_asof(city, td, tz)
        fmin = fc_cache[(city, td)]
        if fmin is None:
            stats["no_forecast"] += 1
            continue

        clear = bucket_clear_f(fmin, lo, hi)
        if clear is None:
            continue
        if clear < FORECAST_CLEAR_F:
            stats["clear_fail"] += 1
            if clear >= 0:
                near_misses.append(clear)
            continue

        if (city, td) not in act_cache:
            act_cache[(city, td)] = actual_min_f(icao, tz, td, city)
        actual = act_cache[(city, td)]
        if actual is None:
            stats["no_actual"] += 1
            continue

        # Same-day settlement window, station-local civil day.
        try:
            y, m, d = (int(x) for x in td.split("-"))
            day_start = datetime(y, m, d, 0, 0, tzinfo=ZoneInfo(tz))
            day_end = day_start + timedelta(days=1)
        except Exception:
            continue
        t_from = int(day_start.timestamp())
        t_to = int(day_end.timestamp())

        g = safe_get(f"{CLOB}/markets/{r['market_id']}", timeout=25)
        if g.status_code != 200:
            stats["no_print"] += 1
            continue
        md = g.json()
        toks = {t.get("outcome"): t for t in md.get("tokens", [])}
        no_tok = toks.get("No")
        if not no_tok:
            stats["no_print"] += 1
            continue

        pr = first_print_in_band(r["market_id"], no_tok["token_id"], t_from, t_to)
        if not pr:
            stats["no_print"] += 1
            continue
        ts, px, sz = pr

        no_wins = settled_no_wins(actual, lo, hi)
        shares = STAKE / px
        pnl = (shares - STAKE) if no_wins else -STAKE
        stats["traded"] += 1
        trades.append({"city": city, "date": td, "bucket": f"{lo}-{hi}",
                       "fmin": fmin, "actual": actual, "clear": clear,
                       "px": px, "win": no_wins, "pnl": pnl})
        if i % 25 == 0:
            print(f"  ...{i}/{len(lows)} scanned, {stats['traded']} trades", flush=True)

    print()
    print("=" * 78)
    print("LOW-MARKET SPEC BACKTEST  (entry %.2f-%.2f, clear>=%.1fF, same-day)"
          % (MIN_PX, MAX_PX, FORECAST_CLEAR_F))
    print("=" * 78)
    for k, v in stats.items():
        print(f"  {k:14s} {v}")
    if not trades:
        if near_misses:
            near_misses.sort(reverse=True)
            print(f"\n  best clearances seen (all < {FORECAST_CLEAR_F}F): "
                  + ", ".join(f"{x:.1f}" for x in near_misses[:12]))
        print("\nNo qualifying trades.")
        return
    w = [t for t in trades if t["win"]]
    pnl = sum(t["pnl"] for t in trades)
    staked = STAKE * len(trades)
    print(f"\n  trades {len(trades)}  wins {len(w)}  win rate {100*len(w)/len(trades):.1f}%")
    print(f"  P&L ${pnl:+.2f} on ${staked:.2f} staked  ({100*pnl/staked:+.1f}%)")
    avg_w = statistics.mean([t["pnl"] for t in w]) if w else 0
    ls = [t["pnl"] for t in trades if not t["win"]]
    avg_l = statistics.mean(ls) if ls else 0
    print(f"  avg win ${avg_w:+.2f}   avg loss ${avg_l:+.2f}")
    print(f"  median entry {statistics.median([t['px'] for t in trades]):.3f}")
    print()
    print("%-13s %-11s %-13s %7s %7s %6s %6s %s" %
          ("city", "date", "bucket", "fmin", "actual", "clear", "px", "W/L"))
    for t in sorted(trades, key=lambda x: (x["date"], x["city"]))[:40]:
        print("%-13s %-11s %-13s %7.1f %7.1f %6.1f %6.3f %s" %
              (t["city"][:13], t["date"], t["bucket"], t["fmin"], t["actual"],
               t["clear"], t["px"], "W" if t["win"] else "L"))


if __name__ == "__main__":
    main()
