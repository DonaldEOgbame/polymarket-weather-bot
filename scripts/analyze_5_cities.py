import urllib.request, json, os, sqlite3
from collections import defaultdict, Counter
from datetime import datetime
from zoneinfo import ZoneInfo

CITIES = {
    "London": {"lat": 51.5048, "lon": 0.0495, "tz": "Europe/London", "icao": "EGLC", "climate": "Maritime (North Sea/Temperate)"},
    "Amsterdam": {"lat": 52.3105, "lon": 4.7683, "tz": "Europe/Amsterdam", "icao": "EHAM", "climate": "Maritime (North Sea/Lowlands)"},
    "Madrid": {"lat": 40.4936, "lon": -3.5668, "tz": "Europe/Madrid", "icao": "LEMD", "climate": "Continental Mediterranean (Plateau/High Diurnal)"},
    "Istanbul": {"lat": 41.2753, "lon": 28.7519, "tz": "Europe/Istanbul", "icao": "LTFM", "climate": "Transitional Maritime (Bosphorus/Black Sea)"},
    "Moscow": {"lat": 55.5915, "lon": 37.2615, "tz": "Europe/Moscow", "icao": "UUWW", "climate": "Humid Continental (High Latitude)"},
}

CACHE_DIR = "scripts/_backtest_cache"

print("=" * 80)
print("EMPIRICAL TIMING ANALYSIS: 974 DAYS (2024-01-01 to 2026-08-31)")
print("=" * 80)

city_timing = {}

for city, info in CITIES.items():
    cache_fn = os.path.join(CACHE_DIR, f"{city.lower()}_hourly_timing.json")
    if not os.path.exists(cache_fn):
        url = (f"https://archive-api.open-meteo.com/v1/archive?"
               f"latitude={info['lat']}&longitude={info['lon']}"
               f"&start_date=2024-01-01&end_date=2026-08-31"
               f"&hourly=temperature_2m&temperature_unit=fahrenheit&timezone={info['tz'].replace('/', '%2F')}")
        req = urllib.request.Request(url, headers={'User-Agent': 'weather-bot/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            with open(cache_fn, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Failed to fetch {city}: {e}")
            continue
    else:
        with open(cache_fn) as f:
            data = json.load(f)

    times = data['hourly']['time']
    temps = data['hourly']['temperature_2m']

    days = defaultdict(list)
    for t_str, temp in zip(times, temps):
        if temp is None: continue
        d_str = t_str[:10]
        hr = int(t_str[11:13])
        days[d_str].append((hr, temp))

    high_hours = []
    low_hours = []
    for d, recs in days.items():
        if len(recs) < 24: continue
        max_t = max(r[1] for r in recs)
        min_t = min(r[1] for r in recs)
        hr_max = [r[0] for r in recs if r[1] == max_t]
        hr_min = [r[0] for r in recs if r[1] == min_t]
        high_hours.append(hr_max[0])
        low_hours.append(hr_min[0])

    n_days = len(high_hours)

    # Find best 4h window for High
    best_high_win = None
    best_high_pct = 0
    for sh in range(24):
        win = [(sh + i) % 24 for i in range(4)]
        cnt = sum(1 for h in high_hours if h in win)
        pct = cnt / n_days * 100
        if pct > best_high_pct:
            best_high_pct = pct
            best_high_win = (sh, (sh + 4) % 24, pct)

    # Find best 4h window for Low (morning trough)
    best_low_win = None
    best_low_pct = 0
    for sh in range(24):
        win = [(sh + i) % 24 for i in range(4)]
        cnt = sum(1 for h in low_hours if h in win)
        pct = cnt / n_days * 100
        if pct > best_low_pct:
            best_low_pct = pct
            best_low_win = (sh, (sh + 4) % 24, pct)

    c_h = Counter(high_hours)
    c_l = Counter(low_hours)
    top_h = c_h.most_common(2)
    top_l = c_l.most_common(2)

    city_timing[city] = {
        "n_days": n_days,
        "high_win": best_high_win,
        "low_win": best_low_win,
        "top_high_hours": top_h,
        "top_low_hours": top_l
    }

    print(f"\n{city.upper()} ({info['icao']} - {info['tz']}) | Climate: {info['climate']}")
    print(f"  HIGH 4-Hour Window: {best_high_win[0]:02d}:00 – {best_high_win[1]:02d}:00 ({best_high_pct:.1f}% coverage) | Peak: {top_h[0][0]:02d}:00 ({top_h[0][1]/n_days*100:.1f}%)")
    print(f"  LOW  4-Hour Window: {best_low_win[0]:02d}:00 – {best_low_win[1]:02d}:00 ({best_low_pct:.1f}% coverage) | Peak: {top_l[0][0]:02d}:00 ({top_l[0][1]/n_days*100:.1f}%)")

