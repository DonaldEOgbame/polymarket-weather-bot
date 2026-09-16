import csv
import json
import os
from collections import defaultdict
from datetime import datetime

# Load ledger_v2_ungated.csv and ledger_forecast_margin.csv
CACHE_DIR = "scripts/_backtest_cache"

# Let's inspect the date range in ledger_v2_ungated.csv
with open(os.path.join(CACHE_DIR, "ledger_v2_ungated.csv")) as f:
    v2_rows = list(csv.DictReader(f))

dates = sorted(list({r["target_date"] for r in v2_rows if r.get("target_date")}))
earliest_date, latest_date = dates[0], dates[-1]
dt_start = datetime.strptime(earliest_date, "%Y-%m-%d")
dt_end = datetime.strptime(latest_date, "%Y-%m-%d")
total_days = (dt_end - dt_start).days + 1
total_calendar_days = len(dates)

print(f"Backtest sample window: {earliest_date} to {latest_date} ({total_days} calendar days, {total_calendar_days} active trading days)")

# Analyze our strategy filters on ledger_v2_ungated.csv:
# Filters:
# 1. Price band: [0.89, 0.95] (or [0.90, 0.95])
# 2. Winning rate / PnL
# Notice ledger_v2_ungated already filtered for the 4-hour pre-lock-in window!
# Let's filter for price in [0.89, 0.95] across all cities

for band_low, band_high in [(0.89, 0.95), (0.90, 0.95), (0.88, 0.95), (0.90, 0.96)]:
    print(f"\n==================== PRICE BAND [{band_low:.2f} - {band_high:.2f}] ====================")
    qualifying = []
    by_city = defaultdict(list)
    for r in v2_rows:
        p = float(r["entry_price"])
        if band_low <= p <= band_high:
            qualifying.append(r)
            by_city[r["city"]].append(r)

    total_trades = len(qualifying)
    trades_per_day_all = total_trades / total_days
    trades_per_month_all = trades_per_day_all * 30

    london_trades = len(by_city["London"])
    london_trades_per_day = london_trades / total_days
    london_trades_per_month = london_trades_per_day * 30

    # Let's check win rate and PnL with $100 stake
    stake = 100.0
    total_pnl = 0.0
    wins = 0
    losses = 0
    for r in qualifying:
        p = float(r["entry_price"])
        won = (r["won"] == "True")
        shares = stake / p
        if won:
            wins += 1
            total_pnl += (shares * 1.0 - stake)
        else:
            losses += 1
            # Check exit price or loss
            exit_p = float(r.get("exit_price", 0.0))
            total_pnl += (shares * exit_p - stake)

    win_rate = (wins / total_trades * 100) if total_trades else 0
    avg_return_per_trade = (total_pnl / total_trades) if total_trades else 0

    print(f"ALL 10 CITIES:")
    print(f"  Total Trades: {total_trades} in {total_days} days")
    print(f"  Trade Frequency: {trades_per_day_all:.2f} trades/day ({trades_per_day_all*7:.1f} trades/week, {trades_per_month_all:.1f} trades/month)")
    print(f"  Win Rate: {wins} wins / {total_trades} total ({win_rate:.1f}%)")
    print(f"  Total PnL ($100/trade): ${total_pnl:,.2f}")
    print(f"  Avg Net Return per Trade: ${avg_return_per_trade:.2f} (+{avg_return_per_trade:.1f}%)")
    print(f"  Monthly Expected PnL ($100/trade): ${trades_per_month_all * avg_return_per_trade:,.2f}")

    print(f"\nLONDON ONLY:")
    print(f"  Total Trades: {london_trades} in {total_days} days")
    print(f"  Trade Frequency: {london_trades_per_day:.2f} trades/day ({london_trades_per_day*7:.1f} trades/week, {london_trades_per_month:.1f} trades/month)")
    lon_wins = sum(1 for r in by_city["London"] if r["won"] == "True")
    lon_pnl = sum((stake/float(r["entry_price"])*1.0 - stake) if r["won"] == "True" else (stake/float(r["entry_price"])*float(r.get("exit_price", 0.0)) - stake) for r in by_city["London"])
    lon_wr = lon_wins / london_trades * 100 if london_trades else 0
    lon_avg = lon_pnl / london_trades if london_trades else 0
    print(f"  Win Rate: {lon_wins} / {london_trades} ({lon_wr:.1f}%)")
    print(f"  Total PnL ($100/trade): ${lon_pnl:,.2f}")
    print(f"  Avg Net Return per Trade: ${lon_avg:.2f} (+{lon_avg:.1f}%)")
    print(f"  Monthly Expected PnL ($100/trade): ${london_trades_per_month * lon_avg:,.2f}")

    print("\nBreakdown by City:")
    for c, c_rows in sorted(by_city.items(), key=lambda x: len(x[1]), reverse=True):
        c_cnt = len(c_rows)
        c_w = sum(1 for r in c_rows if r["won"] == "True")
        c_pnl = sum((stake/float(r["entry_price"])*1.0 - stake) if r["won"] == "True" else (stake/float(r["entry_price"])*float(r.get("exit_price", 0.0)) - stake) for r in c_rows)
        print(f"  {c:15s}: {c_cnt:3d} trades ({c_cnt/total_days*30:4.1f}/mo) | Win: {c_w/c_cnt*100:5.1f}% | PnL: ${c_pnl:7.2f} | Avg: ${c_pnl/c_cnt:5.2f}")
