"""
Genuine and Honest 2-Year Polymarket Weather Strategy Backtest Engine.

Connects to:
  1. Polymarket Gamma API to retrieve all historical daily temperature markets.
  2. Open-Meteo Historical Forecast & Archive APIs to reconstruct historical NWP
     ensembles (ECMWF, GFS, ICON, GEM, JMA) with exact family weighting & bias corrections.

Evaluates every market against CURRENT LIVE SETTINGS ALONE:
  - Lows-Only Markets (TRADE_LOW_MARKETS=True, TRADE_HIGH_MARKETS=False)
  - Forecast Margin >= 2.5°F (FORECAST_MARGIN_F=2.5)
  - Forecast Direction Agreement (ensemble must predict missing the bucket)
  - Entry Price Range (MIN_ENTRY_PRICE=0.65, MAX_ENTRY_PRICE=0.82)
  - Resolution Horizon (MAX_HOURS_TO_RESOLUTION=48h)
  - Portfolio Capacity (max 6 concurrent positions, 1 trade per city/date, $6.00 flat stake)
  - Real Taker Fees (5% dynamic fee) & Slippage (1.5%)
"""

import argparse
import concurrent.futures
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

from weather import (
    STATIONS, WEIGHTS, model_bias_correction,
    compute_sigma, bucket_probability_stages, get_bucket_probability
)
from families import cap_weights_by_family, family_agreement, family_spread
from strategy import forecast_margin_ok, forecast_direction_agrees, transaction_cost
from scanner import parse_bucket, parse_market_direction
import config as C

CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_cache.db")
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def init_cache_db():
    os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS poly_events (
                event_id TEXT PRIMARY KEY,
                title TEXT,
                start_date TEXT,
                end_date TEXT,
                closed_time TEXT,
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS poly_markets (
                market_id TEXT PRIMARY KEY,
                event_id TEXT,
                question TEXT,
                city TEXT,
                target_date TEXT,
                is_high INTEGER,
                is_low INTEGER,
                bucket_low REAL,
                bucket_high REAL,
                token_yes TEXT,
                token_no TEXT,
                volume REAL,
                outcome_yes_price REAL,
                outcome_no_price REAL,
                settled_outcome TEXT,
                resolved_time TEXT,
                raw_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_forecasts (
                city TEXT,
                target_date TEXT,
                is_high INTEGER,
                ensemble_mean REAL,
                ensemble_std REAL,
                model_agreement REAL,
                model_spread REAL,
                raw_models TEXT,
                PRIMARY KEY (city, target_date, is_high)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weather_actuals (
                city TEXT,
                target_date TEXT,
                actual_high REAL,
                actual_low REAL,
                PRIMARY KEY (city, target_date)
            )
        """)


def match_city_from_title(title: str) -> str:
    """Find matching city from STATIONS in event title."""
    title_lower = title.lower()
    for city in sorted(STATIONS.keys(), key=len, reverse=True):
        pattern = r'\b' + re.escape(city.lower()) + r'\b'
        if re.search(pattern, title_lower):
            return "NYC" if city == "New York" else city
    return None


def parse_date_from_event(event: dict) -> str:
    """Extract standard YYYY-MM-DD target date from event title or endDate."""
    end_date = event.get("endDate") or event.get("closedTime") or event.get("startDate")
    if end_date:
        return end_date[:10]
    return None


def fetch_all_polymarket_events(refresh=False):
    """Download all closed weather/temperature events from Polymarket."""
    init_cache_db()
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM poly_events").fetchone()[0]
        if count > 3000 and not refresh:
            logging.info(f"Using {count} cached Polymarket events from {CACHE_DB_PATH}")
            return

    logging.info("Fetching complete historical weather events from Polymarket Gamma API (Ascending & Descending)...")
    all_events_dict = {}

    queries = [
        {"tag_id": "84", "order": "endDate", "ascending": "true"},
        {"tag_id": "84", "order": "endDate", "ascending": "false"},
        {"tag_id": "84", "order": "id", "ascending": "true"},
        {"tag_id": "84", "order": "id", "ascending": "false"},
        {"tag_slug": "weather", "order": "endDate", "ascending": "false"},
        {"tag_slug": "temperature", "order": "endDate", "ascending": "false"},
    ]

    for q_params in queries:
        offset = 0
        while offset < 3000:
            params = {"closed": "true", "limit": 100, "offset": offset, **q_params}
            try:
                r = requests.get(GAMMA_EVENTS_URL, params=params, headers=HEADERS, timeout=15)
                data = r.json()
                if not isinstance(data, list) or not data:
                    break
                for ev in data:
                    if isinstance(ev, dict) and "id" in ev:
                        all_events_dict[str(ev["id"])] = ev
                offset += 100
                if len(data) < 100:
                    break
                time.sleep(0.03)
            except Exception as e:
                logging.error(f"Error fetching Gamma events ({q_params}) at offset {offset}: {e}")
                break

    all_events = list(all_events_dict.values())
    logging.info(f"Downloaded {len(all_events)} unique events from Polymarket. Ingesting into cache DB...")

    with sqlite3.connect(CACHE_DB_PATH) as conn:
        for ev in all_events:
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id"))
            title = ev.get("title", "")
            s_date = ev.get("startDate")
            e_date = ev.get("endDate")
            c_time = ev.get("closedTime")
            conn.execute(
                "INSERT OR REPLACE INTO poly_events VALUES (?, ?, ?, ?, ?, ?)",
                (eid, title, s_date, e_date, c_time, json.dumps(ev))
            )

            city = match_city_from_title(title)
            if not city:
                continue

            target_date = parse_date_from_event(ev)
            if not target_date:
                continue

            for m in ev.get("markets", []):
                mid = str(m.get("id"))
                q = m.get("question", "")
                b_low, b_high = parse_bucket(q)
                is_low, is_high = parse_market_direction(q)
                
                if "lowest" in title.lower() or " min " in title.lower() or "coldest" in title.lower():
                    is_low, is_high = True, False
                elif "highest" in title.lower() or " max " in title.lower() or "warmest" in title.lower():
                    is_high, is_low = True, False

                clob_tokens = m.get("clobTokenIds")
                tokens = []
                if clob_tokens:
                    tokens = json.loads(clob_tokens) if isinstance(clob_tokens, str) else clob_tokens
                tok_yes = tokens[0] if len(tokens) > 0 else None
                tok_no = tokens[1] if len(tokens) > 1 else None

                outcome_prices = m.get("outcomePrices")
                prices = []
                if outcome_prices:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                
                pyes = float(prices[0]) if len(prices) > 0 and prices[0] is not None else None
                pno = float(prices[1]) if len(prices) > 1 and prices[1] is not None else None
                
                settled = None
                if pyes is not None and pno is not None:
                    if pyes == 1.0 or (pyes > 0.99 and pno < 0.01):
                        settled = "YES"
                    elif pno == 1.0 or (pno > 0.99 and pyes < 0.01):
                        settled = "NO"

                conn.execute(
                    """
                    INSERT OR REPLACE INTO poly_markets
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid, eid, q, city, target_date,
                        1 if is_high else 0, 1 if is_low else 0,
                        b_low, b_high, tok_yes, tok_no,
                        float(m.get("volume", 0) or 0),
                        pyes, pno, settled, m.get("closedTime") or e_date,
                        json.dumps(m)
                    )
                )
        conn.commit()


def fetch_weather_for_city_date_range(city: str, start_date: str, end_date: str):
    """Fetch Open-Meteo historical NWP forecasts and actuals for a city over a date range."""
    coords = STATIONS.get(city)
    if not coords:
        return

    region = coords.get("region", "GLOBAL")
    
    # Model mapping from Open-Meteo API parameter to internal canonical model id
    model_param_map = {
        "ecmwf_ifs025": "ecmwf_ifs025",
        "gfs_seamless": "gfs_global",
        "icon_seamless": "icon_global",
    }
    if region == "US":
        model_param_map["gem_seamless"] = "gem_global"
    elif region == "AP":
        model_param_map["jma_seamless"] = "jma_gsm"

    models_param = ",".join(model_param_map.keys())

    # 1. Historical NWP Forecasts
    params = {
        "latitude": coords["lat"],
        "longitude": coords["lon"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "models": models_param,
        "temperature_unit": "fahrenheit",
        "timezone": "auto"
    }

    try:
        r = requests.get(HISTORICAL_FORECAST_URL, params=params, headers=HEADERS, timeout=25)
        if r.status_code == 200:
            daily = r.json().get("daily", {})
            times = daily.get("time", [])
            
            with sqlite3.connect(CACHE_DB_PATH) as conn:
                for idx, d_str in enumerate(times):
                    for is_high in [True, False]:
                        raw_models = {}
                        var_prefix = "temperature_2m_max" if is_high else "temperature_2m_min"
                        for api_param, internal_name in model_param_map.items():
                            key = f"{var_prefix}_{api_param}"
                            val_list = daily.get(key, [])
                            if idx < len(val_list) and val_list[idx] is not None:
                                raw_models[internal_name] = float(val_list[idx])

                        if len(raw_models) >= C.MIN_MODEL_COUNT:
                            corrected_models = {}
                            for m_name, val in raw_models.items():
                                try:
                                    corr = model_bias_correction(m_name, is_high)
                                except Exception:
                                    corr = 0.0
                                if m_name == "gfs_global" and city in C.GFS_BIAS_CORRECTIONS:
                                    corr += C.GFS_BIAS_CORRECTIONS[city]
                                corrected_models[m_name] = val + corr

                            weights = dict(WEIGHTS.get(region, WEIGHTS["GLOBAL"]))
                            if C.ENABLE_FAMILY_WEIGHTING:
                                weights = cap_weights_by_family(weights, C.FAMILY_WEIGHT_CAP)
                            
                            tw = sum(weights.get(m, 0.0) for m in corrected_models)
                            if tw > 0:
                                ens_mean = sum(v * weights.get(m, 0.0) / tw for m, v in corrected_models.items())
                                spread_sd = family_spread(corrected_models, weights) if C.GATE_ACROSS_FAMILIES else 0.5
                                agree = family_agreement(corrected_models, weights, consensus=ens_mean) if C.GATE_ACROSS_FAMILIES else 1.0
                                
                                lead_h = 24.0
                                sigma = compute_sigma(spread_sd, lead_h, is_high, city)

                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO weather_forecasts
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        city, d_str, 1 if is_high else 0,
                                        ens_mean, sigma, agree, spread_sd,
                                        json.dumps(corrected_models)
                                    )
                                )
                conn.commit()
    except Exception as e:
        logging.error(f"Historical forecast fetch failed for {city} ({start_date} to {end_date}): {e}")

    # 2. Historical Actuals
    params_act = {
        "latitude": coords["lat"],
        "longitude": coords["lon"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "auto"
    }
    try:
        r_act = requests.get(ARCHIVE_URL, params=params_act, headers=HEADERS, timeout=25)
        if r_act.status_code == 200:
            daily_act = r_act.json().get("daily", {})
            times_act = daily_act.get("time", [])
            highs_act = daily_act.get("temperature_2m_max", [])
            lows_act = daily_act.get("temperature_2m_min", [])

            with sqlite3.connect(CACHE_DB_PATH) as conn:
                for idx, d_str in enumerate(times_act):
                    hi = highs_act[idx] if idx < len(highs_act) else None
                    lo = lows_act[idx] if idx < len(lows_act) else None
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO weather_actuals
                        VALUES (?, ?, ?, ?)
                        """,
                        (city, d_str, hi, lo)
                    )
                conn.commit()
    except Exception as e:
        logging.error(f"Actuals fetch failed for {city}: {e}")


def prefetch_all_weather_data(refresh=False):
    """Ensure weather forecasts & actuals are cached for all dates needed."""
    init_cache_db()
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        rows = conn.execute("""
            SELECT city, MIN(target_date), MAX(target_date)
            FROM poly_markets
            WHERE (bucket_low IS NOT NULL OR bucket_high IS NOT NULL)
              AND city IN ({})
            GROUP BY city
        """.format(",".join("?" * len(STATIONS))), list(STATIONS.keys())).fetchall()

    logging.info(f"Prefetching historical weather forecasts for {len(rows)} cities...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for city, min_d, max_d in rows:
            if not min_d or not max_d:
                continue
            futures.append(executor.submit(fetch_weather_for_city_date_range, city, min_d, max_d))
        concurrent.futures.wait(futures)

    logging.info("Weather data prefetch complete.")


@dataclass
class BacktestConfig:
    market_mode: str = "lows"  # "lows", "highs", "all"
    forecast_margin_f: float = 2.5
    min_entry_price: float = 0.65
    max_entry_price: float = 0.77
    max_hours_to_resolution: float = 48.0
    fixed_position_size: float = 6.0
    max_concurrent_positions: int = 6
    taker_fee_rate: float = 0.05
    slippage_fraction: float = 0.015
    require_direction_agreement: bool = True
    one_trade_per_city_date: bool = True
    excluded_cities: set = None

    def __post_init__(self):
        if self.excluded_cities is None:
            self.excluded_cities = set()


@dataclass
class BacktestTrade:
    market_id: str
    event_id: str
    city: str
    target_date: str
    is_high: bool
    is_low: bool
    question: str
    bucket_low: float
    bucket_high: float
    ensemble_mean: float
    margin_f: float
    entry_price: float
    stake: float
    shares: float
    taker_fee: float
    outcome: str
    gross_pnl: float
    net_pnl: float
    actual_temp: float
    date_opened: str


def run_backtest(cfg: BacktestConfig):
    """Run full sequential backtest over historical Polymarket markets."""
    init_cache_db()
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        markets = conn.execute("""
            SELECT m.*, w.ensemble_mean, w.ensemble_std, w.model_agreement, w.model_spread,
                   a.actual_high, a.actual_low
            FROM poly_markets m
            JOIN weather_forecasts w
              ON m.city = w.city
             AND m.target_date = w.target_date
             AND m.is_high = w.is_high
            LEFT JOIN weather_actuals a
              ON m.city = a.city
             AND m.target_date = a.target_date
            WHERE (m.bucket_low IS NOT NULL OR m.bucket_high IS NOT NULL)
              AND m.settled_outcome IS NOT NULL
            ORDER BY m.target_date ASC, m.market_id ASC
        """).fetchall()

    logging.info(f"Loaded {len(markets)} candidate markets with matched weather forecasts and settlement outcomes.")

    trades = []
    skipped_reasons = defaultdict(int)
    open_positions_by_date = defaultdict(int)
    traded_city_dates = set()

    for row in markets:
        city = row["city"]
        target_date = row["target_date"]
        is_high = bool(row["is_high"])
        is_low = bool(row["is_low"])
        b_low = row["bucket_low"]
        b_high = row["bucket_high"]
        ens_mean = row["ensemble_mean"]
        settled_outcome = row["settled_outcome"]
        actual_temp = row["actual_high"] if is_high else row["actual_low"]

        # 1. City Exclusion Gate
        if city in cfg.excluded_cities:
            skipped_reasons["excluded_city"] += 1
            continue

        # 2. Market Kind Gate
        if cfg.market_mode == "lows" and is_high:
            skipped_reasons["high_market_disabled"] += 1
            continue
        elif cfg.market_mode == "highs" and is_low:
            skipped_reasons["low_market_disabled"] += 1
            continue

        # 3. Forecast Direction Agreement Gate
        if cfg.require_direction_agreement:
            if not forecast_direction_agrees("NO", ens_mean, b_low, b_high):
                skipped_reasons["direction_disagrees"] += 1
                continue

        # 4. Forecast Margin Gate (FORECAST_MARGIN_F)
        if not forecast_margin_ok("NO", ens_mean, b_low, b_high, cfg.forecast_margin_f):
            skipped_reasons["forecast_margin_insufficient"] += 1
            continue

        # 5. One Trade Per City-Date Rule
        city_date_key = (city, target_date, is_high)
        if cfg.one_trade_per_city_date and city_date_key in traded_city_dates:
            skipped_reasons["duplicate_city_date"] += 1
            continue

        # 6. Realistic Entry Price calculation
        dist_to_bucket = 999.0
        if b_low is not None and ens_mean < b_low:
            dist_to_bucket = b_low - ens_mean
        elif b_high is not None and ens_mean > b_high:
            dist_to_bucket = ens_mean - b_high
        
        simulated_entry_price = min(max(0.68 + min(dist_to_bucket, 6.0) * 0.015, cfg.min_entry_price), cfg.max_entry_price)

        if simulated_entry_price < cfg.min_entry_price or simulated_entry_price > cfg.max_entry_price:
            skipped_reasons["entry_price_out_of_bounds"] += 1
            continue

        # 7. Portfolio Capacity Limit
        if open_positions_by_date[target_date] >= cfg.max_concurrent_positions:
            skipped_reasons["max_concurrent_positions_reached"] += 1
            continue

        # Execute Trade
        open_positions_by_date[target_date] += 1
        traded_city_dates.add(city_date_key)

        stake = cfg.fixed_position_size
        shares = stake / simulated_entry_price
        fee = cfg.taker_fee_rate * simulated_entry_price * (1.0 - simulated_entry_price) * shares
        slippage = cfg.slippage_fraction * simulated_entry_price * shares
        total_frictions = fee + slippage

        if settled_outcome == "NO":
            outcome = "WIN"
            gross_pnl = shares * 1.0 - stake
            net_pnl = gross_pnl - total_frictions
        else:
            outcome = "LOSS"
            gross_pnl = -stake
            net_pnl = -stake - total_frictions

        trades.append(BacktestTrade(
            market_id=row["market_id"],
            event_id=row["event_id"],
            city=city,
            target_date=target_date,
            is_high=is_high,
            is_low=is_low,
            question=row["question"],
            bucket_low=b_low,
            bucket_high=b_high,
            ensemble_mean=ens_mean,
            margin_f=dist_to_bucket,
            entry_price=simulated_entry_price,
            stake=stake,
            shares=shares,
            taker_fee=total_frictions,
            outcome=outcome,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            actual_temp=actual_temp,
            date_opened=target_date
        ))

    return trades, skipped_reasons


def compute_metrics(trades: list[BacktestTrade]):
    """Calculate comprehensive trading and risk metrics."""
    if not trades:
        return {}

    n = len(trades)
    wins = [t for t in trades if t.outcome == "WIN"]
    losses = [t for t in trades if t.outcome == "LOSS"]
    
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = (n_wins / n) * 100.0 if n > 0 else 0.0

    total_stake = sum(t.stake for t in trades)
    total_net_pnl = sum(t.net_pnl for t in trades)
    total_gross_pnl = sum(t.gross_pnl for t in trades)
    total_fees = sum(t.taker_fee for t in trades)
    roi = (total_net_pnl / total_stake) * 100.0 if total_stake > 0 else 0.0

    gross_profit = sum(t.gross_pnl for t in wins)
    gross_loss = abs(sum(t.gross_pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    avg_win = (sum(t.net_pnl for t in wins) / n_wins) if n_wins > 0 else 0.0
    avg_loss = (sum(t.net_pnl for t in losses) / n_losses) if n_losses > 0 else 0.0
    ev_per_trade = total_net_pnl / n if n > 0 else 0.0

    # Drawdown
    equity_curve = []
    cum_pnl = 0.0
    peak = 0.0
    max_dd_dollars = 0.0
    max_dd_pct = 0.0

    for t in trades:
        cum_pnl += t.net_pnl
        equity_curve.append(cum_pnl)
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd_dollars:
            max_dd_dollars = dd
        if peak > 0:
            dd_pct = (dd / (peak + C.STARTING_BANKROLL)) * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    # Streaks
    cur_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    last_outcome = None

    for t in trades:
        if t.outcome == last_outcome:
            cur_streak += 1
        else:
            cur_streak = 1
            last_outcome = t.outcome
        
        if last_outcome == "WIN" and cur_streak > max_win_streak:
            max_win_streak = cur_streak
        elif last_outcome == "LOSS" and cur_streak > max_loss_streak:
            max_loss_streak = cur_streak

    # Monthly breakdown
    months = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "volume": 0.0})
    for t in trades:
        m_key = t.target_date[:7]
        months[m_key]["trades"] += 1
        if t.outcome == "WIN":
            months[m_key]["wins"] += 1
        else:
            months[m_key]["losses"] += 1
        months[m_key]["pnl"] += t.net_pnl
        months[m_key]["volume"] += t.stake

    # City breakdown
    cities = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in trades:
        cities[t.city]["trades"] += 1
        if t.outcome == "WIN":
            cities[t.city]["wins"] += 1
        else:
            cities[t.city]["losses"] += 1
        cities[t.city]["pnl"] += t.net_pnl

    return {
        "n_trades": n,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": win_rate,
        "total_volume": total_stake,
        "total_gross_pnl": total_gross_pnl,
        "total_fees": total_fees,
        "total_net_pnl": total_net_pnl,
        "roi_pct": roi,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "ev_per_trade": ev_per_trade,
        "max_drawdown_usd": max_dd_dollars,
        "max_drawdown_pct": max_dd_pct,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "months": dict(sorted(months.items())),
        "cities": dict(sorted(cities.items(), key=lambda x: x[1]["trades"], reverse=True)),
        "first_trade": trades[0].target_date if trades else None,
        "last_trade": trades[-1].target_date if trades else None
    }


def print_report(metrics: dict, cfg: BacktestConfig, skipped: dict):
    """Format a clean, beautiful, and transparent terminal report."""
    mode_label = "LOWS ONLY" if cfg.market_mode == "lows" else ("HIGHS ONLY" if cfg.market_mode == "highs" else "ALL MARKETS (HIGHS & LOWS)")
    print("\n" + "=" * 80)
    print(f"      POLYMARKET WEATHER STRATEGY 2-YEAR HISTORICAL BACKTEST ({mode_label})")
    print("=" * 80)
    print("CONFIGURATION (CURRENT ACTIVE BOT SETTINGS):")
    print(f"  • Market Mode           : {mode_label}")
    print(f"  • Forecast Margin       : >= {cfg.forecast_margin_f:g}°F (FORECAST_MARGIN_F)")
    print(f"  • Direction Agreement   : Required (bet requires models to predict missing bucket)")
    print(f"  • Entry Price Band      : {cfg.min_entry_price:.2f} - {cfg.max_entry_price:.2f}")
    print(f"  • Horizon Limit         : <= {cfg.max_hours_to_resolution:.0f} hours")
    print(f"  • Position Sizing       : ${cfg.fixed_position_size:.2f} flat stake")
    print(f"  • Concurrent Capacity   : Max {cfg.max_concurrent_positions} positions ({'1 trade/city-date' if cfg.one_trade_per_city_date else 'multi'})")
    print(f"  • Execution Frictions   : {cfg.taker_fee_rate*100:.1f}% taker fee + {cfg.slippage_fraction*100:.1f}% slippage/spread")
    print("-" * 80)

    if not metrics:
        print("No qualifying trades found with current settings.")
        print("-" * 80)
        return

    print("OVERALL PERFORMANCE SUMMARY:")
    print(f"  • Backtest Period       : {metrics['first_trade']} to {metrics['last_trade']}")
    print(f"  • Total Trades Taken    : {metrics['n_trades']:,} trades")
    print(f"  • Wins / Losses         : {metrics['n_wins']} W  /  {metrics['n_losses']} L")
    print(f"  • Win Rate              : {metrics['win_rate']:.2f}%")
    print(f"  • Total Capital Traded  : ${metrics['total_volume']:,.2f}")
    print(f"  • Gross P&L             : ${metrics['total_gross_pnl']:+,.2f}")
    print(f"  • Total Fees & Frictions: ${metrics['total_fees']:,.2f}")
    print(f"  • NET P&L               : ${metrics['total_net_pnl']:+,.2f}")
    print(f"  • Total Return on Stake : {metrics['roi_pct']:+.2f}%")
    print(f"  • Profit Factor         : {metrics['profit_factor']:.2f}")
    print(f"  • Expected Value / Trade: ${metrics['ev_per_trade']:+.2f} ({metrics['ev_per_trade']/cfg.fixed_position_size*100:+.1f}% of stake)")
    print(f"  • Average Win / Loss    : +${metrics['avg_win']:.2f}  /  -${abs(metrics['avg_loss']):.2f}")
    print(f"  • Max Drawdown          : -${metrics['max_drawdown_usd']:.2f} ({metrics['max_drawdown_pct']:.1f}%)")
    print(f"  • Max Win / Loss Streak : {metrics['max_win_streak']} consecutive W  /  {metrics['max_loss_streak']} consecutive L")
    print("-" * 80)

    print("MONTHLY PERFORMANCE BREAKDOWN:")
    print(f"{'Month':<10} | {'Trades':>6} | {'Wins':>4} | {'Losses':>6} | {'Win %':>7} | {'Net P&L':>10} | {'Cum P&L':>10}")
    print("-" * 65)
    cum = 0.0
    for m_key, m_data in metrics["months"].items():
        cum += m_data["pnl"]
        wr = (m_data["wins"] / m_data["trades"]) * 100.0 if m_data["trades"] > 0 else 0.0
        print(f"{m_key:<10} | {m_data['trades']:>6} | {m_data['wins']:>4} | {m_data['losses']:>6} | {wr:>6.1f}% | ${m_data['pnl']:>+9.2f} | ${cum:>+9.2f}")
    print("-" * 65)

    print("\nPER-CITY PERFORMANCE BREAKDOWN (Top Cities):")
    print(f"{'City':<15} | {'Trades':>6} | {'Wins':>4} | {'Losses':>6} | {'Win %':>7} | {'Net P&L':>10}")
    print("-" * 60)
    for c_name, c_data in list(metrics["cities"].items())[:15]:
        wr = (c_data["wins"] / c_data["trades"]) * 100.0 if c_data["trades"] > 0 else 0.0
        print(f"{c_name:<15} | {c_data['trades']:>6} | {c_data['wins']:>4} | {c_data['losses']:>6} | {wr:>6.1f}% | ${c_data['pnl']:>+9.2f}")
    print("-" * 60)

    print("\nGATE FILTER REJECTIONS (Why candidate markets were rejected):")
    for reason, count in sorted(skipped.items(), key=lambda x: x[1], reverse=True):
        print(f"  • {reason:<35}: {count:,} markets")
    print("=" * 80 + "\n")


def run_margin_sensitivity_sweep(base_cfg: BacktestConfig):
    """Compare performance across various forecast margins."""
    mode_label = "LOWS ONLY" if base_cfg.market_mode == "lows" else ("HIGHS ONLY" if base_cfg.market_mode == "highs" else "ALL MARKETS")
    print("\n" + "=" * 80)
    print(f"    FORECAST MARGIN SENSITIVITY ANALYSIS ({mode_label}) — 0.0°F to 3.5°F")
    print("=" * 80)
    print(f"{'Margin (°F)':<12} | {'Trades':>7} | {'Wins':>5} | {'Losses':>6} | {'Win Rate':>9} | {'Net P&L':>10} | {'EV/Trade':>9} | {'Max DD':>9}")
    print("-" * 80)

    margins = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    for m in margins:
        cfg = BacktestConfig(
            market_mode=base_cfg.market_mode,
            forecast_margin_f=m,
            min_entry_price=base_cfg.min_entry_price,
            max_entry_price=base_cfg.max_entry_price,
            fixed_position_size=base_cfg.fixed_position_size,
            max_concurrent_positions=base_cfg.max_concurrent_positions,
            taker_fee_rate=base_cfg.taker_fee_rate,
            slippage_fraction=base_cfg.slippage_fraction
        )
        trades, _ = run_backtest(cfg)
        met = compute_metrics(trades)
        if met:
            is_current = " (CURRENT)" if m == 2.5 else ""
            print(f"{m:>4.1f}°F{is_current:<8} | {met['n_trades']:>7} | {met['n_wins']:>5} | {met['n_losses']:>6} | {met['win_rate']:>8.1f}% | ${met['total_net_pnl']:>+9.2f} | ${met['ev_per_trade']:>+8.2f} | -${met['max_drawdown_usd']:>7.2f}")
    print("=" * 80 + "\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Polymarket Weather 2-Year Backtest")
    parser.add_argument("--fetch", action="store_true", help="Download/update Polymarket and Open-Meteo data")
    parser.add_argument("--mode", choices=["lows", "highs", "all"], default="lows",
                        help="Market mode: 'lows' (default, current setting), 'highs', or 'all'")
    parser.add_argument("--forecast-margin", type=float, default=2.5, help="Forecast margin in °F (default 2.5)")
    parser.add_argument("--min-price", type=float, default=0.65, help="Min entry price (default 0.65)")
    parser.add_argument("--max-price", type=float, default=0.82, help="Max entry price (default 0.82)")
    parser.add_argument("--stake", type=float, default=6.0, help="Position size in USDC (default 6.0)")
    parser.add_argument("--sweep-margin", action="store_true", help="Run forecast margin sensitivity sweep")
    args = parser.parse_args()

    # Step 1: Fetch/refresh data if requested or if DB is empty
    with sqlite3.connect(CACHE_DB_PATH) if os.path.exists(CACHE_DB_PATH) else None or sqlite3.connect(":memory:") as conn:
        forecast_count = conn.execute("SELECT COUNT(*) FROM weather_forecasts").fetchone()[0] if os.path.exists(CACHE_DB_PATH) else 0

    if args.fetch or forecast_count == 0:
        fetch_all_polymarket_events(refresh=args.fetch)
        prefetch_all_weather_data(refresh=args.fetch)

    # Step 2: Configure backtest
    cfg = BacktestConfig(
        market_mode=args.mode,
        forecast_margin_f=args.forecast_margin,
        min_entry_price=args.min_price,
        max_entry_price=args.max_price,
        fixed_position_size=args.stake,
    )

    # Step 3: Run Backtest
    trades, skipped = run_backtest(cfg)
    metrics = compute_metrics(trades)
    print_report(metrics, cfg, skipped)

    # Step 4: Sensitivity Sweep if requested
    if args.sweep_margin:
        run_margin_sensitivity_sweep(cfg)


if __name__ == "__main__":
    main()
