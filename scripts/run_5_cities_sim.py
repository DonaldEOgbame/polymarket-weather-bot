import os, json, re, sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import urllib.request

CACHE_DIR = "scripts/_backtest_cache"
ARCHIVE_DIR = os.path.join(CACHE_DIR, "archive")

CITY_SPECS = {
    "London":    {"tz": "Europe/London",    "high_win": (13, 17), "low_win": (4, 8),  "icao": "EGLC", "lat": 51.5048, "lon": 0.0495},
    "Amsterdam": {"tz": "Europe/Amsterdam", "high_win": (14, 18), "low_win": (5, 9),  "icao": "EHAM", "lat": 52.3105, "lon": 4.7683},
    "Madrid":    {"tz": "Europe/Madrid",    "high_win": (15, 19), "low_win": (6, 10), "icao": "LEMD", "lat": 40.4936, "lon": -3.5668},
    "Istanbul":  {"tz": "Europe/Istanbul",  "high_win": (13, 17), "low_win": (3, 7),  "icao": "LTFM", "lat": 41.2753, "lon": 28.7519},
    "Moscow":    {"tz": "Europe/Moscow",    "high_win": (13, 17), "low_win": (3, 7),  "icao": "UUWW", "lat": 55.5915, "lon": 37.2615},
}

def parse_bucket(q):
    m_between = re.search(r'(\d+)(?:°C)?\s*to\s*(\d+)°C', q)
    if m_between:
        low_c, high_c = float(m_between.group(1)), float(m_between.group(2))
        return low_c * 9/5 + 32, high_c * 9/5 + 32, f"{int(low_c)}-{int(high_c)}°C"
    m_higher = re.search(r'(\d+)°C\s*or higher', q)
    if m_higher:
        c = float(m_higher.group(1))
        return c * 9/5 + 32 - 0.45, None, f"{int(c)}°C or higher"
    m_below = re.search(r'(\d+)°C\s*or below', q)
    if m_below:
        c = float(m_below.group(1))
        return None, c * 9/5 + 32 + 0.45, f"{int(c)}°C or below"
    m_single = re.search(r'be\s*(\d+)°C', q)
    if m_single:
        c = float(m_single.group(1))
        center_f = c * 9/5 + 32
        return center_f - 0.45, center_f + 0.45, f"{int(c)}°C"
    return None, None, "Unknown"

def fetch_archive(city, lat, lon, tz_str, target_date):
    disk_path = os.path.join(ARCHIVE_DIR, f"{city}_{target_date}.json")
    if os.path.exists(disk_path):
        with open(disk_path) as f:
            d = json.load(f)
            return d.get("max_f"), d.get("min_f")
    url = (f"https://archive-api.open-meteo.com/v1/archive?"
           f"latitude={lat}&longitude={lon}"
           f"&start_date={target_date}&end_date={target_date}"
           f"&daily=temperature_2m_max,temperature_2m_min"
           f"&temperature_unit=fahrenheit&timezone={tz_str.replace('/', '%2F')}")
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
    except Exception:
        pass
    return None, None

con = sqlite3.connect("data/bot.db")
cur = con.cursor()

portfolio_results = {}

for city, spec in CITY_SPECS.items():
    tz = ZoneInfo(spec["tz"])
    cur.execute("SELECT market_id, question, target_date, bucket_low, bucket_high FROM markets WHERE city LIKE ?;", (f"%{city}%",))
    db_rows = cur.fetchall()
    
    unique_dates = sorted(list({r[2] for r in db_rows if r[2]}))
    n_days = len(unique_dates) if unique_dates else 1
    
    city_trades = []
    total_vol_090_096 = 0.0
    markets_with_100 = 0
    markets_with_band_trades = 0
    
    for r in db_rows:
        cid, q, t_date, b_low, b_high = r
        if not t_date: continue
        t_path = os.path.join(CACHE_DIR, f"{cid}.json")
        if not os.path.exists(t_path): continue
        with open(t_path) as f:
            raw = json.load(f)
        if not raw or not isinstance(raw, list): continue

        is_high = "highest" in q.lower()
        if b_low is None or b_high is None:
            bl, bh, _ = parse_bucket(q)
            b_low = b_low or bl
            b_high = b_high or bh
            
        w_start, w_end = spec["high_win"] if is_high else spec["low_win"]
        
        # Tape filter
        in_band_trades = []
        for t in raw:
            try:
                p = float(t["price"])
                sz = float(t.get("size", 0.0))
                ts = int(t["timestamp"])
            except Exception: continue
            dt_local = datetime.fromtimestamp(ts, tz=tz)
            if dt_local.date().isoformat() != t_date: continue
            hr_dec = dt_local.hour + dt_local.minute / 60.0 + dt_local.second / 3600.0
            if w_start <= hr_dec <= w_end:
                if 0.90 <= p <= 0.96:
                    in_band_trades.append({"p": p, "sz": sz, "vol": p * sz, "ts": ts})

        if in_band_trades:
            markets_with_band_trades += 1
            mkt_vol = sum(t["vol"] for t in in_band_trades)
            total_vol_090_096 += mkt_vol
            if mkt_vol >= 100.0:
                markets_with_100 += 1
                
            # Check entry in 0.90 - 0.95
            entry_trades = [t for t in in_band_trades if 0.90 <= t["p"] <= 0.95]
            if entry_trades:
                entry = entry_trades[0]
                # Check clearance
                max_f, min_f = fetch_archive(city, spec["lat"], spec["lon"], spec["tz"], t_date)
                clearance = None
                won = None
                if max_f is not None and min_f is not None:
                    actual = max_f if is_high else min_f
                    if b_low is not None and b_high is not None:
                        if actual < b_low: clearance = b_low - actual
                        elif actual > b_high: clearance = actual - b_high
                        else: clearance = 0.0
                        won = not (b_low <= actual <= b_high)
                    elif b_low is not None:
                        clearance = b_low - actual
                        won = (actual < b_low)
                    elif b_high is not None:
                        clearance = actual - b_high
                        won = (actual > b_high)
                
                city_trades.append({
                    "cid": cid,
                    "target_date": t_date,
                    "entry_p": entry["p"],
                    "vol": mkt_vol,
                    "clearance": clearance,
                    "won": won
                })

    portfolio_results[city] = {
        "n_days": n_days,
        "date_range": (unique_dates[0], unique_dates[-1]) if unique_dates else ("N/A", "N/A"),
        "markets_with_band_trades": markets_with_band_trades,
        "markets_with_100": markets_with_100,
        "total_vol": total_vol_090_096,
        "trades": city_trades
    }

print("\n" + "=" * 95)
print(f"{'City':<12} | {'Days':<5} | {'Mkts 0.90-0.96':<14} | {'Mkts >= $100':<12} | {'Total Vol ($)':<14} | {'0.90-0.95 Trades':<16} | {'Win Rate (>=2F)':<15}")
print("=" * 95)

total_sample_days = 7  # The active overlap window in bot.db is ~7-8 days (Sep 9 - Sep 16)
grand_trades_2f = 0
grand_pnl = 0.0
stake = 100.0

for city, res in portfolio_results.items():
    tr = res["trades"]
    tr_2f = [t for t in tr if t["clearance"] is not None and t["clearance"] >= 2.0]
    wins_2f = sum(1 for t in tr_2f if t["won"] is True)
    wr_2f = (wins_2f / len(tr_2f) * 100) if tr_2f else 100.0
    pnl_2f = sum((stake / t["entry_p"] * 1.0 - stake) for t in tr_2f if t["won"])
    grand_trades_2f += len(tr_2f)
    grand_pnl += pnl_2f
    print(f"{city:<12} | {res['n_days']:<5} | {res['markets_with_band_trades']:2d} markets     | {res['markets_with_100']:2d} markets   | ${res['total_vol']:11.2f}  | {len(tr):2d} (qual: {len(tr_2f):2d})     | {wr_2f:5.1f}% ({wins_2f}/{len(tr_2f)})")

print("-" * 95)
print(f"5-CITY PORTFOLIO TOTALS (Sample period: ~7-8 days):")
print(f"  Total Qualifying Trades (>= 2.0°F gate): {grand_trades_2f} trades across 5 cities")
print(f"  Trade Frequency: ~{grand_trades_2f / 7.0:.1f} trades / day (~{grand_trades_2f / 7.0 * 7:.0f} trades / week, ~{grand_trades_2f / 7.0 * 30:.0f} trades / month)")
print(f"  Total Net PnL ($100 stake): ${grand_pnl:,.2f}")
print(f"  Average Net Return per Trade: ${grand_pnl / grand_trades_2f:.2f} (+{grand_pnl / grand_trades_2f:.1f}%)" if grand_trades_2f else "N/A")
print(f"  Expected Monthly PnL ($100 stake): ${(grand_trades_2f / 7.0 * 30) * (grand_pnl / grand_trades_2f):,.2f}" if grand_trades_2f else "N/A")

