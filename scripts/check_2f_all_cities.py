import csv, os
from collections import defaultdict

CACHE_DIR = "scripts/_backtest_cache"

# Check ledger_forecast_margin for trades in 0.90 - 0.96
with open(os.path.join(CACHE_DIR, "ledger_forecast_margin.csv")) as f:
    fm_rows = list(csv.DictReader(f))

in_band_fm = [r for r in fm_rows if 0.90 <= float(r["entry_price"]) <= 0.96]
print(f"In ledger_forecast_margin (>= 4.5F gate), trades in 0.90-0.96: {len(in_band_fm)}")
for r in in_band_fm:
    print(f"  {r['city']} | {r['target_date']} | Entry: {r['entry_price']} | Won: {r['won']}")

# Check ledger_v2_ungated (which had 0.90-0.95)
with open(os.path.join(CACHE_DIR, "ledger_v2_ungated.csv")) as f:
    v2_rows = list(csv.DictReader(f))

# Let's see how many trades in 0.90-0.95 had winning outcome across cities
in_band_v2 = [r for r in v2_rows if 0.90 <= float(r["entry_price"]) <= 0.95]
print(f"\nIn ledger_v2_ungated, total trades in 0.90-0.95: {len(in_band_v2)}")

by_city = defaultdict(lambda: {"total": 0, "won": 0, "pnl": 0.0})
stake = 100.0
for r in in_band_v2:
    c = r["city"]
    p = float(r["entry_price"])
    won = (r["won"] == "True")
    by_city[c]["total"] += 1
    if won:
        by_city[c]["won"] += 1
        by_city[c]["pnl"] += (stake / p * 1.0 - stake)
    else:
        by_city[c]["pnl"] += -stake

print("\nCity performance in 0.90-0.95:")
for c, st in sorted(by_city.items(), key=lambda x: x[1]["pnl"], reverse=True):
    wr = st["won"] / st["total"] * 100
    print(f"  {c:15s}: {st['total']:3d} trades | Win: {wr:5.1f}% | Total PnL: ${st['pnl']:7.2f} | Avg/Trade: ${st['pnl']/st['total']:5.2f}")
