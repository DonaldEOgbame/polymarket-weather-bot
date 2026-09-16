import csv, os
from collections import defaultdict

CACHE_DIR = "scripts/_backtest_cache"
fn = os.path.join(CACHE_DIR, "ledger_forecast_margin.csv")

if os.path.exists(fn):
    with open(fn) as f:
        rows = list(csv.DictReader(f))
    print(f"Total rows in ledger_forecast_margin.csv: {len(rows)}")
    dates = sorted(list({r["target_date"] for r in rows if r.get("target_date")}))
    total_days = len(dates)
    print(f"Trading days: {total_days} ({dates[0]} to {dates[-1]})")

    # Group by city
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)

    stake = 100.0
    for c, c_rows in sorted(by_city.items(), key=lambda x: len(x[1]), reverse=True):
        cnt = len(c_rows)
        w = sum(1 for r in c_rows if r["won"] == "True")
        pnl = sum((stake/float(r["entry_price"])*1.0 - stake) if r["won"] == "True" else -stake for r in c_rows)
        # Average entry price
        avg_p = sum(float(r["entry_price"]) for r in c_rows) / cnt
        print(f"  {c:15s}: {cnt:3d} trades ({cnt/total_days*30:4.1f}/mo) | Win: {w/cnt*100:5.1f}% | Avg Entry: {avg_p:.3f} | Total PnL ($100): ${pnl:7.2f} | Avg/Trade: ${pnl/cnt:5.2f}")

    total_cnt = len(rows)
    total_w = sum(1 for r in rows if r["won"] == "True")
    tot_pnl = sum((stake/float(r["entry_price"])*1.0 - stake) if r["won"] == "True" else -stake for r in rows)
    print("-" * 80)
    print(f"  {'ALL CITIES':15s}: {total_cnt:3d} trades ({total_cnt/total_days*30:4.1f}/mo) | Win: {total_w/total_cnt*100:5.1f}% | Total PnL: ${tot_pnl:7.2f} | Avg/Trade: ${tot_pnl/total_cnt:5.2f}")
