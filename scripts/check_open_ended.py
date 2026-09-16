import sys
sys.path.insert(0, '.')
from scripts.analyze_london_liquidity import markets, CACHE_DIR, TZ
import os, json

print("Checking open-ended buckets:")
open_ended_trades = []
for cid, m in markets.items():
    if not m.get("is_open_ended"): continue
    t_path = os.path.join(CACHE_DIR, f"{cid}.json")
    if not os.path.exists(t_path): continue
    with open(t_path) as f: raw = json.load(f)
    for t in raw:
        p = float(t.get("price", 0))
        if 0.90 <= p <= 0.96:
            open_ended_trades.append((m["target_date"], m["question"], p, float(t.get("size", 0)), t.get("timestamp")))

print(f"Open-ended trades in 0.90-0.96: {len(open_ended_trades)}")
for ot in open_ended_trades[:10]:
    print(ot)
