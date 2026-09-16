import sys, os, json, csv
sys.path.insert(0, '.')
from scripts.analyze_london_liquidity import markets, CACHE_DIR, fetch_archive_extremes, TZ
from datetime import datetime

# Load all London trade tapes
all_tape_markets = []

high_win = (13, 17)
low_win = (4, 8)

for cid, m in markets.items():
    if not m["target_date"]: continue
    t_path = os.path.join(CACHE_DIR, f"{cid}.json")
    if not os.path.exists(t_path): continue
    with open(t_path) as f: raw = json.load(f)
    if not raw: continue

    max_f, min_f = fetch_archive_extremes("London", m["target_date"])
    if max_f is None or min_f is None: continue

    actual = max_f if m["is_high"] else min_f
    b_low, b_high = m["bucket_low"], m["bucket_high"]

    # Clearance calculation
    clearance_f = None
    if m["is_high"]:
        if b_low is not None and b_high is not None:
            if actual < b_low: clearance_f = b_low - actual
            elif actual > b_high: clearance_f = actual - b_high
            else: clearance_f = 0.0
        elif b_low is not None:
            clearance_f = b_low - actual
        elif b_high is not None:
            clearance_f = actual - b_high
    else:
        if b_low is not None and b_high is not None:
            if actual > b_high: clearance_f = actual - b_high
            elif actual < b_low: clearance_f = b_low - actual
            else: clearance_f = 0.0
        elif b_high is not None:
            clearance_f = actual - b_high
        elif b_low is not None:
            clearance_f = b_low - actual

    # Is NO winner?
    is_no_winner = False
    if b_low is not None and b_high is not None:
        is_no_winner = not (b_low <= actual <= b_high)
    elif b_low is not None:
        is_no_winner = (actual < b_low)
    elif b_high is not None:
        is_no_winner = (actual > b_high)

    tokens_map = {}
    if m["yes_token"] and m["no_token"]:
        tokens_map[m["yes_token"]] = "YES"
        tokens_map[m["no_token"]] = "NO"

    tape = []
    for t in raw:
        try:
            p = float(t["price"])
            sz = float(t.get("size", 0.0))
            ts = int(t["timestamp"])
            side = t.get("side", "")
            asset = t.get("asset", "")
        except Exception: continue
        dt = datetime.fromtimestamp(ts, tz=TZ)
        if dt.date().isoformat() != m["target_date"]: continue
        hr_dec = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        tape.append({
            "ts": ts,
            "hr_dec": hr_dec,
            "price": p,
            "size": sz,
            "usd_volume": p * sz,
            "side": side,
            "token_side": tokens_map.get(asset, "NO")
        })
    tape.sort(key=lambda x: x["ts"])
    if tape:
        all_tape_markets.append({
            "market": m,
            "clearance_f": clearance_f,
            "is_no_winner": is_no_winner,
            "tape": tape
        })

print(f"Total evaluated London markets with complete data: {len(all_tape_markets)}")

# Test strategy parameters
# Grid:
# Price Band: [0.90, 0.95], [0.88, 0.95], [0.90, 0.96], [0.92, 0.96]
# Clearance Gate: 1.5, 2.0, 2.5, 3.0
# Stop-loss: None, 0.70, 0.60
# Stake: Flat $100 per trade (assuming $100 position size)

def run_sim(p_min, p_max, clear_gate, stop_loss, high_w=(13, 17), low_w=(4, 8)):
    stake = 100.0
    trades = []
    for item in all_tape_markets:
        if item["clearance_f"] is None or item["clearance_f"] < clear_gate:
            continue
        m = item["market"]
        w_start, w_end = high_w if m["is_high"] else low_w
        tape = item["tape"]
        
        # Look for first trigger in window and band
        entry = None
        for t in tape:
            if not (w_start <= t["hr_dec"] <= w_end):
                continue
            if p_min <= t["price"] <= p_max:
                entry = t
                break
        if not entry:
            continue
        
        # Walk forward after entry to check stop loss and outcome
        entry_price = entry["price"]
        entry_ts = entry["ts"]
        shares = stake / entry_price
        
        subsequent = [t for t in tape if t["ts"] >= entry_ts and t["token_side"] == entry["token_side"]]
        
        # Stop loss check
        stopped_out = False
        exit_price = None
        min_p = entry_price
        for st in subsequent:
            if st["price"] < min_p:
                min_p = st["price"]
            if stop_loss is not None and st["price"] <= stop_loss:
                stopped_out = True
                exit_price = st["price"]
                break
        
        if stopped_out:
            pnl = shares * exit_price - stake
            won = False
            exit_reason = "stop_loss"
        else:
            if item["is_no_winner"]:
                pnl = shares * 1.0 - stake
                won = True
                exit_price = 1.0
                exit_reason = "settlement_win"
            else:
                pnl = -stake
                won = False
                exit_price = 0.0
                exit_reason = "settlement_loss"
                
        trades.append({
            "cid": m["condition_id"],
            "target_date": m["target_date"],
            "is_high": m["is_high"],
            "entry_price": entry_price,
            "min_subsequent": min_p,
            "pnl": pnl,
            "won": won,
            "exit_reason": exit_reason
        })
    
    total_pnl = sum(t["pnl"] for t in trades)
    n_trades = len(trades)
    win_count = sum(1 for t in trades if t["won"])
    win_rate = (win_count / n_trades * 100) if n_trades > 0 else 0.0
    return {
        "n_trades": n_trades,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "pnl_per_trade": total_pnl / n_trades if n_trades > 0 else 0.0,
        "trades": trades
    }

# Grid search results
print("\n--- PARAMETER GRID SEARCH ($100 Flat Stake) ---")
print(f"{'Band':<12} | {'Gate':<6} | {'Stop':<6} | {'Trades':<7} | {'Win Rate':<10} | {'Total PnL ($)':<14} | {'PnL/Trade ($)':<14}")
print("-" * 85)

for p_min, p_max in [(0.90, 0.95), (0.88, 0.95), (0.90, 0.96), (0.91, 0.95)]:
    for gate in [1.5, 2.0, 2.5, 3.0]:
        for stop in [None, 0.70, 0.60]:
            stop_label = f"{stop:.2f}" if stop else "None"
            r = run_sim(p_min, p_max, gate, stop)
            if r["n_trades"] > 0:
                print(f"[{p_min:.2f}-{p_max:.2f}] | {gate:4.1f}F | {stop_label:<6} | {r['n_trades']:4d}    | {r['win_rate']:6.1f}%    | ${r['total_pnl']:9.2f}    | ${r['pnl_per_trade']:9.2f}")

