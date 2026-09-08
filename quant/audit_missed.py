"""
quant/audit_missed.py - Diagnostic CLI tool for sniper opportunity analysis.

Analyzes the sniper_audit database table to provide a full breakdown of:
1. Total physical breach events detected in real-time.
2. Fills vs. Missed trades.
3. Breakdown of missed trades:
   - REPRICED_ABOVE_CEILING: The market beat our bot or was already priced efficiently.
   - ZERO_DEPTH: No resting orders existed at or below ceiling.
   - INSUFFICIENT_FUNDS: Available capital constrained entry.
   - EXECUTION_REJECTED: Polymarket matching engine or risk limits rejected the trade.
   - NO_ASKS / BOOK_UNAVAILABLE: Empty book on CLOB.
4. Microsecond reaction latencies and price distributions at the exact millisecond of breach.
"""

import sys
import argparse
from datetime import datetime, timezone
from db import init_db, fetch_query


def analyze_sniper_audit(limit: int = 100, filter_outcome: str = None):
    init_db()
    
    where_clause = ""
    params = [limit]
    if filter_outcome:
        where_clause = "WHERE outcome = ?"
        params = [filter_outcome, limit]

    rows = fetch_query(
        f"""
        SELECT 
            id, timestamp, station_icao, city, target_date, bucket_label, side,
            observed_temp_f, best_ask, best_bid, ask_depth_usd, stake_usd,
            outcome, detail, latency_ms
        FROM sniper_audit
        {where_clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(params)
    )

    print("=" * 100)
    print("🎯 POLYMARKET WEATHER SNIPER — EXECUTION & MISSED TRADE DIAGNOSTIC AUDIT")
    print("=" * 100)

    if not rows:
        print("\nℹ️  No physical breach events recorded in the sniper_audit table yet.")
        print("   The background EventDrivenSniper will automatically log every physical breach")
        print("   as soon as NOAA weather stations report a bucket threshold crossing.\n")
        print("=" * 100)
        return

    total = len(rows)
    outcome_counts = {}
    latencies = []
    repriced_asks = []

    for r in rows:
        oc = r["outcome"]
        outcome_counts[oc] = outcome_counts.get(oc, 0) + 1
        if r["latency_ms"] is not None:
            latencies.append(r["latency_ms"])
        if oc == "REPRICED_ABOVE_CEILING" and r["best_ask"] is not None:
            repriced_asks.append(r["best_ask"])

    filled = outcome_counts.get("FILLED", 0)
    repriced = outcome_counts.get("REPRICED_ABOVE_CEILING", 0)
    zero_depth = outcome_counts.get("ZERO_DEPTH", 0)
    insufficient = outcome_counts.get("INSUFFICIENT_FUNDS", 0)
    rejected = outcome_counts.get("EXECUTION_REJECTED", 0)
    other = total - (filled + repriced + zero_depth + insufficient + rejected)

    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    min_lat = min(latencies) if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0

    print(f"\n📊 AGGREGATE SUMMARY (Last {total} events):")
    print(f"  • Total Physical Breaches: {total}")
    print(f"  • ✅ Fills Executed:        {filled:<4} ({filled/total*100:.1f}%)")
    print(f"  • ❌ Missed (Repriced):     {repriced:<4} ({repriced/total*100:.1f}%) [Market beat sniper]")
    print(f"  • ❌ Missed (Zero Depth):   {zero_depth:<4} ({zero_depth/total*100:.1f}%) [Book dry]")
    print(f"  • ❌ Missed (No Capital):   {insufficient:<4} ({insufficient/total*100:.1f}%)")
    print(f"  • ⚠️  Rejected / Other:     {rejected + other:<4} ({(rejected+other)/total*100:.1f}%)")

    print(f"\n⚡ INTERNAL REACTION SPEED:")
    print(f"  • Avg Latency (Opcode -> CLOB Read): {avg_lat:.2f} ms")
    print(f"  • Min Latency:                       {min_lat:.2f} ms")
    print(f"  • Max Latency:                       {max_lat:.2f} ms")

    if repriced_asks:
        avg_ask = sum(repriced_asks) / len(repriced_asks)
        print(f"\n📉 REPRICING FORENSICS:")
        print(f"  • Average Best Ask on Repriced Breaches: ${avg_ask:.3f}")

    print("\n" + "-" * 100)
    print(f"{'Time (UTC)':<20} {'Station':<7} {'City':<11} {'Bucket':<12} {'Temp':<7} {'Ask':<6} {'Bid':<6} {'Depth':<8} {'Lat(ms)':<8} {'Outcome':<22}")
    print("-" * 100)

    for r in rows[:limit]:
        ts = r["timestamp"][:19].replace("T", " ")
        stn = r["station_icao"] or "N/A"
        city = r["city"][:10]
        bucket = r["bucket_label"][:11]
        temp = f"{r['observed_temp_f']:.1f}°F"
        ask = f"${r['best_ask']:.2f}" if r["best_ask"] is not None else "-"
        bid = f"${r['best_bid']:.2f}" if r["best_bid"] is not None else "-"
        depth = f"${r['ask_depth_usd']:.0f}" if r["ask_depth_usd"] is not None else "-"
        lat = f"{r['latency_ms']:.1f}" if r["latency_ms"] is not None else "-"
        oc = r["outcome"]

        print(f"{ts:<20} {stn:<7} {city:<11} {bucket:<12} {temp:<7} {ask:<6} {bid:<6} {depth:<8} {lat:<8} {oc:<22}")
        if r["detail"]:
            print(f"   ↳ Detail: {r['detail']}")

    print("=" * 100 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze missed trades and sniper execution forensics.")
    parser.add_argument("--limit", type=int, default=50, help="Number of recent records to display.")
    parser.add_argument("--outcome", type=str, default=None, help="Filter by outcome (e.g., REPRICED_ABOVE_CEILING, FILLED, ZERO_DEPTH).")
    args = parser.parse_args()

    analyze_sniper_audit(limit=args.limit, filter_outcome=args.outcome)
