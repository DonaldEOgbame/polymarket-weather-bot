import os, json, glob, csv, re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import sqlite3

# Let's inspect all cities available in bot.db and backtest ledgers
con = sqlite3.connect("data/bot.db")
cur = con.cursor()
cur.execute("SELECT DISTINCT city, COUNT(*) FROM markets GROUP BY city ORDER BY COUNT(*) DESC;")
city_counts = cur.fetchall()
print("Top cities by market count in bot.db:")
for c, cnt in city_counts[:15]:
    print(f"  {c:20s}: {cnt}")

# Let's inspect ledger_v2_ungated.csv to see trade frequency and returns across all cities
ledger_file = "scripts/_backtest_cache/ledger_v2_ungated.csv"
if os.path.exists(ledger_file):
    with open(ledger_file) as f:
        reader = list(csv.DictReader(f))
    print(f"\nTotal rows in ledger_v2_ungated.csv: {len(reader)}")
    cities = defaultdict(list)
    for r in reader:
        cities[r.get("city")].append(r)
    for c, rows in sorted(cities.items(), key=lambda x: len(x[1]), reverse=True)[:15]:
        print(f"  {c:20s}: {len(rows)} trades")
