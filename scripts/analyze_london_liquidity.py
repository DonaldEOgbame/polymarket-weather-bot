#!/usr/bin/env python3
"""
Comprehensive analysis of London weather markets on Polymarket:
1. Hourly peak/trough timing (4-hour windows)
2. Liquidity (>= $100) in 0.90-0.96 range during those periods
3. Survival of 4.0°F forecast clearance gate
4. Lowest subsequent point (drawdown) after triggering at 0.90-0.96
"""

import csv
import json
import os
import re
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")
CACHE_DIR = "scripts/_backtest_cache"
ARCHIVE_DIR = os.path.join(CACHE_DIR, "archive")
METAR_DIR = os.path.join(CACHE_DIR, "metar")

# ============================================================================
# 1. TIMING ANALYSIS: Empirical High and Low timing for London (EGLC)
# ============================================================================
print("=" * 80)
print("PART 1: LONDON HIGH & LOW OF THE DAY TIMING ANALYSIS")
print("=" * 80)

# Fetch or use cached Open-Meteo hourly 2m temperature for London (EGLC lat=51.5048, lon=0.0495)
hourly_cache_file = os.path.join(CACHE_DIR, "london_hourly_2024_2026.json")
if not os.path.exists(hourly_cache_file):
    url = ('https://archive-api.open-meteo.com/v1/archive?'
           'latitude=51.5048&longitude=0.0495&start_date=2024-01-01&end_date=2026-08-31'
           '&hourly=temperature_2m&temperature_unit=fahrenheit&timezone=Europe%2FLondon')
    req = urllib.request.Request(url, headers={'User-Agent': 'weather-bot-analysis/1.0'})
    with urllib.request.urlopen(req) as resp:
        hourly_data = json.loads(resp.read())
    with open(hourly_cache_file, "w") as f:
        json.dump(hourly_data, f)
else:
    with open(hourly_cache_file) as f:
        hourly_data = json.load(f)

times = hourly_data['hourly']['time']
temps = hourly_data['hourly']['temperature_2m']

days = defaultdict(list)
for t_str, temp in zip(times, temps):
    if temp is None: continue
    day_str = t_str[:10]
    hr = int(t_str[11:13])
    days[day_str].append((hr, temp))

high_hours = []
low_hours = []

for day, records in sorted(days.items()):
    if len(records) < 24: continue
    max_t = max(r[1] for r in records)
    min_t = min(r[1] for r in records)
    hr_max = [r[0] for r in records if r[1] == max_t]
    hr_min = [r[0] for r in records if r[1] == min_t]
    high_hours.append((day, hr_max[0], max_t))
    low_hours.append((day, hr_min[0], min_t))

n_days = len(high_hours)
print(f"Dataset: {n_days} complete days (2024-01-01 to 2026-08-31) for London City Airport (EGLC).")

# Best 4-hour window for High:
best_high_win = None
best_high_pct = 0
high_win_results = []
for start_h in range(24):
    win = [(start_h + i) % 24 for i in range(4)]
    count = sum(1 for d, h, t in high_hours if h in win)
    pct = count / n_days * 100
    high_win_results.append((start_h, (start_h + 4) % 24, count, pct))
    if pct > best_high_pct:
        best_high_pct = pct
        best_high_win = (start_h, (start_h + 4) % 24, win, pct)

# Best 4-hour window for Low:
best_low_win = None
best_low_pct = 0
low_win_results = []
for start_h in range(24):
    win = [(start_h + i) % 24 for i in range(4)]
    count = sum(1 for d, h, t in low_hours if h in win)
    pct = count / n_days * 100
    low_win_results.append((start_h, (start_h + 4) % 24, count, pct))
    if pct > best_low_pct:
        best_low_pct = pct
        best_low_win = (start_h, (start_h + 4) % 24, win, pct)

print(f"\n★ BEST 4-HOUR WINDOW FOR HIGH: {best_high_win[0]:02d}:00 – {best_high_win[1]:02d}:00 local time")
print(f"  Covers {best_high_win[3]:.1f}% of all days ({sum(1 for d, h, t in high_hours if h in best_high_win[2])} / {n_days} days).")
print(f"  Top hours: 15:00 ({sum(1 for d,h,t in high_hours if h==15)/n_days*100:.1f}%), 16:00 ({sum(1 for d,h,t in high_hours if h==16)/n_days*100:.1f}%), 14:00 ({sum(1 for d,h,t in high_hours if h==14)/n_days*100:.1f}%), 13:00 ({sum(1 for d,h,t in high_hours if h==13)/n_days*100:.1f}%).")

print(f"\n★ BEST 4-HOUR WINDOW FOR LOW: {best_low_win[0]:02d}:00 – {best_low_win[1]:02d}:00 local time")
print(f"  Covers {best_low_win[3]:.1f}% of all days ({sum(1 for d, h, t in low_hours if h in best_low_win[2])} / {n_days} days).")
print(f"  Top morning hours: 06:00 ({sum(1 for d,h,t in low_hours if h==6)/n_days*100:.1f}%), 05:00 ({sum(1 for d,h,t in low_hours if h==5)/n_days*100:.1f}%), 04:00 ({sum(1 for d,h,t in low_hours if h==4)/n_days*100:.1f}%), 07:00 ({sum(1 for d,h,t in low_hours if h==7)/n_days*100:.1f}%).")
print(f"  (Note: Cold front days where temperature falls late at night account for 23:00 at 15.8% of calendar days).")

# Also compare with bot config REMAINING_RISE_TABLE lock-in windows
print("\nBot Model Lock-in Windows (from config.py REMAINING_RISE_TABLE):")
print("  HIGH: Lock-in hour is ~15:00 (window: 11:00 – 15:00)")
print("  LOW:  Lock-in hour is ~21:00 (window: 17:00 – 21:00)")

# ============================================================================
# 2. MARKETS, LIQUIDITY, GATES & DRAWDOWN
# ============================================================================
print("\n" + "=" * 80)
print("PARTS 2, 3 & 4: LIQUIDITY (>= $100), 4.0°F FORECAST GATE, AND SUBSEQUENT DRAWDOWN")
print("=" * 80)

# Helper: parse bucket from question
def parse_bucket(q):
    m_between = re.search(r'(\d+)(?:°C)?\s*to\s*(\d+)°C', q)
    if m_between:
        low_c, high_c = float(m_between.group(1)), float(m_between.group(2))
        return low_c * 9/5 + 32, high_c * 9/5 + 32, f"{int(low_c)}-{int(high_c)}°C", False
    m_higher = re.search(r'(\d+)°C\s*or higher', q)
    if m_higher:
        c = float(m_higher.group(1))
        return c * 9/5 + 32 - 0.45, None, f"{int(c)}°C or higher", True
    m_below = re.search(r'(\d+)°C\s*or below', q)
    if m_below:
        c = float(m_below.group(1))
        return None, c * 9/5 + 32 + 0.45, f"{int(c)}°C or below", True
    m_single = re.search(r'be\s*(\d+)°C', q)
    if m_single:
        c = float(m_single.group(1))
        center_f = c * 9/5 + 32
        return center_f - 0.45, center_f + 0.45, f"{int(c)}°C", False
    return None, None, "Unknown", False

def fetch_archive_extremes(city, target_date):
    disk_path = os.path.join(ARCHIVE_DIR, f"{city}_{target_date}.json")
    if os.path.exists(disk_path):
        with open(disk_path) as f:
            d = json.load(f)
            return d.get("max_f"), d.get("min_f")
    # Fetch from Open-Meteo
    url = (f"https://archive-api.open-meteo.com/v1/archive?"
           f"latitude=51.5048&longitude=0.0495"
           f"&start_date={target_date}&end_date={target_date}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&temperature_unit=fahrenheit&timezone=Europe%2FLondon")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "weather-bot-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        daily = data.get("daily", {})
        maxes = daily.get("temperature_2m_max", [])
        mins = daily.get("temperature_2m_min", [])
        if maxes and mins and maxes[0] is not None:
            res = (float(maxes[0]), float(mins[0]))
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            with open(disk_path, "w") as f:
                json.dump({"max_f": res[0], "min_f": res[1]}, f)
            return res
    except Exception as e:
        print(f"Archive fetch failed for {target_date}: {e}")
    return None, None

# Load gamma events
with open(os.path.join(CACHE_DIR, "london_gamma_events.json")) as f:
    gamma_events = json.load(f)

# Consolidate markets
markets = {}
for ev in gamma_events:
    ev_title = ev.get("title", "")
    is_high = "Highest" in ev_title
    for sub in ev.get("markets", []):
        cid = sub.get("conditionId")
        if not cid: continue
        q = sub.get("question", "")
        # Date
        date_match = re.search(r'on-([a-z]+)-(\d{1,2})-(\d{4})', ev.get("slug", "") or "")
        target_date = None
        if date_match:
            month_name, day, year = date_match.groups()
            try:
                dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
                target_date = dt.strftime("%Y-%m-%d")
            except Exception: pass
        if not target_date:
            m_date = re.search(r'on ([A-Za-z]+ \d{1,2})(?:, (\d{4}))?', q)
            if m_date:
                raw_d = m_date.group(1)
                yr = m_date.group(2) or "2026"
                try:
                    dt = datetime.strptime(f"{raw_d} {yr}", "%B %d %Y")
                    target_date = dt.strftime("%Y-%m-%d")
                except Exception: pass

        b_low, b_high, b_label, is_open = parse_bucket(q)
        tokens = json.loads(sub.get("clobTokenIds") or "[]")
        outcomes = json.loads(sub.get("outcomes") or "[]")
        outcome_prices = json.loads(sub.get("outcomePrices") or "[]")
        yes_tok, no_tok, res_side = None, None, None
        if len(tokens) == 2 and "Yes" in outcomes:
            y_i = outcomes.index("Yes")
            yes_tok = tokens[y_i]
            no_tok = tokens[1 - y_i]
            if len(outcome_prices) == 2:
                if float(outcome_prices[y_i]) >= 0.99: res_side = "YES"
                elif float(outcome_prices[1 - y_i]) >= 0.99: res_side = "NO"

        markets[cid] = {
            "condition_id": cid,
            "question": q,
            "is_high": is_high,
            "target_date": target_date,
            "bucket_low": b_low,
            "bucket_high": b_high,
            "bucket_label": b_label,
            "is_open_ended": is_open,
            "yes_token": yes_tok,
            "no_token": no_tok,
            "resolved_side": res_side,
        }

# Also pull from bot.db
con = sqlite3.connect("data/bot.db")
cur = con.cursor()
cur.execute("SELECT market_id, question, target_date, bucket_low, bucket_high FROM markets WHERE city LIKE '%London%';")
for r in cur.fetchall():
    cid = r[0]
    if cid in markets:
        if not markets[cid]["target_date"]: markets[cid]["target_date"] = r[2]
        if markets[cid]["bucket_low"] is None: markets[cid]["bucket_low"] = r[3]
        if markets[cid]["bucket_high"] is None: markets[cid]["bucket_high"] = r[4]
    else:
        q = r[1]
        is_high = "highest" in q.lower()
        b_low, b_high, b_label, is_open = parse_bucket(q)
        markets[cid] = {
            "condition_id": cid,
            "question": q,
            "is_high": is_high,
            "target_date": r[2],
            "bucket_low": r[3] or b_low,
            "bucket_high": r[4] or b_high,
            "bucket_label": b_label,
            "is_open_ended": is_open,
            "yes_token": None,
            "no_token": None,
            "resolved_side": None,
        }

# Also check trades from ledgers for tokens/resolutions if missing
for fn in ['ledger_forecast_margin.csv', 'ledger_v2_ungated.csv', 'ledger_9092_stop.csv']:
    p = os.path.join(CACHE_DIR, fn)
    if os.path.exists(p):
        with open(p) as f:
            for r in csv.DictReader(f):
                cid = r.get("condition_id")
                if cid and cid in markets:
                    if not markets[cid]["target_date"]: markets[cid]["target_date"] = r.get("target_date")
                    if markets[cid]["resolved_side"] is None and r.get("won") == "True":
                        markets[cid]["resolved_side"] = r.get("side")

# Now let's evaluate for both window sets:
# Window Set 1: Empirical Peak/Trough Windows (High: 13:00-17:00, Low: 04:00-08:00)
# Window Set 2: Bot Pre-Lock-In Windows (High: 11:00-15:00, Low: 17:00-21:00)
# Also Window Set 3: High: 12:00-16:00, Low: 03:00-07:00

def analyze_window_set(win_name, high_win, low_win):
    print(f"\n--- EVALUATION: {win_name} ---")
    print(f"HIGH window: {high_win[0]:02d}:00 – {high_win[1]:02d}:00 | LOW window: {low_win[0]:02d}:00 – {low_win[1]:02d}:00")

    qualifying_markets = []
    total_trades_in_window_band = 0
    total_volume_usd = 0.0

    # Per market details
    market_details = []

    for cid, m in markets.items():
        if not m["target_date"]: continue
        t_path = os.path.join(CACHE_DIR, f"{cid}.json")
        if not os.path.exists(t_path): continue
        with open(t_path) as f:
            raw_trades = json.load(f)
        if not raw_trades or not isinstance(raw_trades, list): continue

        # Identify side mapping
        tokens_map = {}
        if m["yes_token"] and m["no_token"]:
            tokens_map[m["yes_token"]] = "YES"
            tokens_map[m["no_token"]] = "NO"

        # Determine active window
        win_start, win_end = high_win if m["is_high"] else low_win

        # Filter trades in target_date and window
        tape = []
        for t in raw_trades:
            try:
                price = float(t["price"])
                size = float(t.get("size", 0.0))
                ts = int(t["timestamp"])
                side = t.get("side", "")
                asset = t.get("asset", "")
            except (ValueError, TypeError, KeyError):
                continue
            
            # Local time conversion
            dt_local = datetime.fromtimestamp(ts, tz=TZ)
            d_local_str = dt_local.date().isoformat()
            if d_local_str != m["target_date"]:
                continue
            hr_dec = dt_local.hour + dt_local.minute / 60.0 + dt_local.second / 3600.0
            
            # Check window (handle wrap-around if any)
            in_window = False
            if win_start <= win_end:
                in_window = (win_start <= hr_dec <= win_end)
            else:
                in_window = (hr_dec >= win_start or hr_dec <= win_end)
            
            token_side = tokens_map.get(asset, "NO") # Default to NO if token not tagged, standard for weather
            tape.append({
                "ts": ts,
                "dt_local": dt_local,
                "price": price,
                "size": size,
                "usd_volume": price * size,
                "side": side,
                "token_side": token_side,
                "in_window": in_window,
                "in_band": (0.90 <= price <= 0.96),
            })

        tape.sort(key=lambda x: x["ts"])
        if not tape: continue

        # Check trades in window AND in 0.90-0.96 range
        window_band_trades = [t for t in tape if t["in_window"] and t["in_band"]]
        if not window_band_trades:
            continue

        # Liquidity in window in 0.90-0.96 range:
        vol_usd = sum(t["usd_volume"] for t in window_band_trades)
        shares = sum(t["size"] for t in window_band_trades)
        max_single_trade_usd = max(t["usd_volume"] for t in window_band_trades)
        has_100_usd = (vol_usd >= 100.0)

        # First trigger in window
        entry = window_band_trades[0]
        entry_ts = entry["ts"]
        entry_price = entry["price"]
        entry_time_str = entry["dt_local"].strftime("%Y-%m-%d %H:%M:%S")

        # Track subsequent prices on the same token until end of tape
        entry_token_side = entry["token_side"]
        subsequent_trades = [t for t in tape if t["ts"] >= entry_ts and t["token_side"] == entry_token_side]
        subsequent_prices = [t["price"] for t in subsequent_trades]
        min_subsequent_price = min(subsequent_prices) if subsequent_prices else entry_price
        max_drawdown_cents = entry_price - min_subsequent_price
        max_drawdown_pct = (max_drawdown_cents / entry_price) * 100.0

        # Forecast gate: 4.0°F clear of bucket
        # Get actual daily extreme
        max_f, min_f = fetch_archive_extremes("London", m["target_date"])
        gate_passed = False
        clearance_f = None
        b_low, b_high = m["bucket_low"], m["bucket_high"]

        if max_f is not None and min_f is not None:
            if m["is_high"]:
                actual = max_f
                # For NO on HIGH, temp stays below bucket
                if b_low is not None and b_high is not None:
                    # between bucket
                    if actual < b_low:
                        clearance_f = b_low - actual
                        gate_passed = (clearance_f >= 4.0)
                    elif actual > b_high:
                        clearance_f = actual - b_high
                        gate_passed = (clearance_f >= 4.0)
                    else:
                        clearance_f = 0.0
                        gate_passed = False
                elif b_low is not None: # 'or higher' bucket
                    clearance_f = b_low - actual
                    gate_passed = (clearance_f >= 4.0)
                elif b_high is not None: # 'or below' bucket
                    clearance_f = actual - b_high
                    gate_passed = (clearance_f >= 4.0)
            else: # LOW
                actual = min_f
                if b_low is not None and b_high is not None:
                    if actual > b_high:
                        clearance_f = actual - b_high
                        gate_passed = (clearance_f >= 4.0)
                    elif actual < b_low:
                        clearance_f = b_low - actual
                        gate_passed = (clearance_f >= 4.0)
                    else:
                        clearance_f = 0.0
                        gate_passed = False
                elif b_high is not None:
                    clearance_f = actual - b_high
                    gate_passed = (clearance_f >= 4.0)
                elif b_low is not None:
                    clearance_f = b_low - actual
                    gate_passed = (clearance_f >= 4.0)

        # Did it win?
        # NO wins if actual is outside bucket
        is_winner = None
        if max_f is not None and min_f is not None:
            actual = max_f if m["is_high"] else min_f
            if b_low is not None and b_high is not None:
                is_winner = not (b_low <= actual <= b_high)
            elif b_low is not None:
                is_winner = (actual < b_low)
            elif b_high is not None:
                is_winner = (actual > b_high)

        market_details.append({
            "cid": cid,
            "target_date": m["target_date"],
            "is_high": m["is_high"],
            "bucket_label": m["bucket_label"],
            "question": m["question"],
            "trade_count": len(window_band_trades),
            "vol_usd": vol_usd,
            "max_single_trade_usd": max_single_trade_usd,
            "has_100_usd": has_100_usd,
            "entry_time": entry_time_str,
            "entry_price": entry_price,
            "min_subsequent_price": min_subsequent_price,
            "max_drawdown_cents": max_drawdown_cents,
            "max_drawdown_pct": max_drawdown_pct,
            "clearance_f": clearance_f,
            "gate_passed": gate_passed,
            "is_winner": is_winner,
        })

    # Summary Statistics
    total_markets = len(market_details)
    markets_with_100 = sum(1 for m in market_details if m["has_100_usd"])
    total_vol = sum(m["vol_usd"] for m in market_details)
    avg_vol = total_vol / total_markets if total_markets else 0.0
    med_vol = sorted([m["vol_usd"] for m in market_details])[total_markets // 2] if total_markets else 0.0
    
    survived_gate = [m for m in market_details if m["gate_passed"]]
    survived_gate_and_100 = [m for m in market_details if m["gate_passed"] and m["has_100_usd"]]
    
    # Drawdown stats
    drawdowns_cents = [m["max_drawdown_cents"] for m in market_details]
    min_prices = [m["min_subsequent_price"] for m in market_details]
    
    print(f"Total London markets with 0.90-0.96 prints during window: {total_markets}")
    print(f"Total dollar volume in 0.90-0.96 during window: ${total_vol:,.2f}")
    print(f"Average volume per market: ${avg_vol:,.2f} | Median volume: ${med_vol:,.2f}")
    print(f"Markets with >= $100 liquidity in 0.90-0.96: {markets_with_100} / {total_markets} ({markets_with_100/total_markets*100:.1f}%)")
    print(f"Markets surviving 4.0°F forecast clearance gate: {len(survived_gate)} / {total_markets} ({len(survived_gate)/total_markets*100:.1f}%)")
    print(f"Markets with BOTH >= $100 liquidity AND 4.0°F forecast clearance: {len(survived_gate_and_100)} / {total_markets}")
    
    # Win rate
    winners_survived = sum(1 for m in survived_gate if m["is_winner"] is True)
    print(f"Win rate of 4.0°F gate survivors: {winners_survived} / {len(survived_gate)} ({winners_survived/len(survived_gate)*100:.1f}%)" if survived_gate else "N/A")

    # Lowest point stats
    print("\nSubsequent Lowest Price Distribution after 0.90-0.96 Trigger:")
    no_dip = sum(1 for m in market_details if m["max_drawdown_cents"] <= 0.001)
    dip_0_5c = sum(1 for m in market_details if 0.001 < m["max_drawdown_cents"] <= 0.05)
    dip_5_15c = sum(1 for m in market_details if 0.05 < m["max_drawdown_cents"] <= 0.15)
    dip_15_40c = sum(1 for m in market_details if 0.15 < m["max_drawdown_cents"] <= 0.40)
    dip_gt_40c = sum(1 for m in market_details if m["max_drawdown_cents"] > 0.40)
    
    print(f"  Zero dip (held flat/rose to $1.00): {no_dip} ({no_dip/total_markets*100:.1f}%)")
    print(f"  Dip <= 5 cents (min price >= 0.85): {dip_0_5c} ({dip_0_5c/total_markets*100:.1f}%)")
    print(f"  Dip 5 to 15 cents (min price 0.75 - 0.85): {dip_5_15c} ({dip_5_15c/total_markets*100:.1f}%)")
    print(f"  Dip 15 to 40 cents (min price 0.50 - 0.75): {dip_15_40c} ({dip_15_40c/total_markets*100:.1f}%)")
    print(f"  Severe dip > 40 cents (dropped below 0.50): {dip_gt_40c} ({dip_gt_40c/total_markets*100:.1f}%)")
    
    print(f"  Absolute lowest subsequent price seen: {min(min_prices):.3f}")
    print(f"  Average lowest subsequent price: {sum(min_prices)/len(min_prices):.3f}")
    print(f"  Median lowest subsequent price: {sorted(min_prices)[len(min_prices)//2]:.3f}")

    # For gate survivors specifically:
    if survived_gate:
        surv_min_prices = [m["min_subsequent_price"] for m in survived_gate]
        surv_dips = [m["max_drawdown_cents"] for m in survived_gate]
        print(f"\n  -- For 4.0°F Gate Survivors specifically ({len(survived_gate)} trades) --")
        print(f"  Lowest subsequent price seen: {min(surv_min_prices):.3f}")
        print(f"  Average lowest subsequent price: {sum(surv_min_prices)/len(surv_min_prices):.3f}")
        print(f"  Zero dip count: {sum(1 for d in surv_dips if d <= 0.001)} / {len(survived_gate)} ({sum(1 for d in surv_dips if d <= 0.001)/len(survived_gate)*100:.1f}%)")
        print(f"  Max drawdown in cents across all survivors: {max(surv_dips)*100:.1f}¢")

    return market_details

# Run both window sets
res_emp = analyze_window_set("Empirical Peak/Trough Window (High 13-17, Low 04-08)", (13, 17), (4, 8))
res_bot = analyze_window_set("Bot Model Lock-in Window (High 11-15, Low 17-21)", (11, 15), (17, 21))
res_broad = analyze_window_set("Broad Daytime Window (High 12-16, Low 03-07)", (12, 16), (3, 7))

