import sqlite3, os, json, re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

CACHE_DIR = "scripts/_backtest_cache"

CITY_SPECS = {
    "London": {"tz": "Europe/London", "high_win": (13, 16), "low_win": (5, 8), "icao": "EGLC", "region": "EU"},
    "Amsterdam": {"tz": "Europe/Amsterdam", "high_win": (14, 17), "low_win": (5, 8), "icao": "EHAM", "region": "EU"},
    "Madrid": {"tz": "Europe/Madrid", "high_win": (15, 18), "low_win": (6, 9), "icao": "LEMD", "region": "EU"},
    "Istanbul": {"tz": "Europe/Istanbul", "high_win": (13, 16), "low_win": (3, 6), "icao": "LTFM", "region": "EU"},
    "Moscow": {"tz": "Europe/Moscow", "high_win": (13, 16), "low_win": (3, 6), "icao": "UUWW", "region": "EU"},
}

con = sqlite3.connect("data/bot.db")
cur = con.cursor()

city_stats = {}

for city, spec in CITY_SPECS.items():
    tz = ZoneInfo(spec["tz"])
    cur.execute("SELECT market_id, question, target_date, bucket_low, bucket_high FROM markets WHERE city LIKE ?;", (f"%{city}%",))
    rows = cur.fetchall()
    
    # Check trade tapes
    cached_tapes = []
    for r in rows:
        cid = r[0]
        p = os.path.join(CACHE_DIR, f"{cid}.json")
        if os.path.exists(p):
            cached_tapes.append((r, p))
            
    city_stats[city] = {
        "total_markets": len(rows),
        "cached_tapes": len(cached_tapes),
        "markets": rows
    }

print("5-City Market Inventory in bot.db:")
for c, st in city_stats.items():
    print(f"  {c:12s}: {st['total_markets']:3d} markets | {st['cached_tapes']:3d} trade tapes cached")

