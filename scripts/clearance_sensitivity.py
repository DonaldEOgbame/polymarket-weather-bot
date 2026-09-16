import sys
sys.path.insert(0, '.')
from scripts.analyze_london_liquidity import analyze_window_set

res = analyze_window_set("Check", (13, 17), (4, 8))

print("\nCLEARANCE THRESHOLD SENSITIVITY TABLE (London 0.90-0.96 band during 13:00-17:00 / 04:00-08:00):")
print(f"{'Min Clearance':<15} | {'Surviving Mkts':<15} | {'Pct of All':<10} | {'Win Rate':<10} | {'Vol >= $100':<12} | {'Avg Lowest Price':<18} | {'Min Lowest Price':<18}")
print("-" * 115)

for thresh in [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]:
    surv = [m for m in res if m["clearance_f"] is not None and m["clearance_f"] >= thresh]
    n_surv = len(surv)
    pct = n_surv / len(res) * 100
    wins = sum(1 for m in surv if m["is_winner"] is True)
    win_rate = (wins / n_surv * 100) if n_surv > 0 else 0.0
    vol_100 = sum(1 for m in surv if m["has_100_usd"])
    avg_low = (sum(m["min_subsequent_price"] for m in surv) / n_surv) if n_surv > 0 else 0.0
    min_low = min(m["min_subsequent_price"] for m in surv) if n_surv > 0 else 0.0
    print(f">= {thresh:3.1f}°F        | {n_surv:2d} / {len(res):2d}           | {pct:5.1f}%    | {win_rate:5.1f}%    | {vol_100:2d} / {n_surv:2d}      | {avg_low:6.3f}             | {min_low:6.3f}")

