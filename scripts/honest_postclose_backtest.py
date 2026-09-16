#!/usr/bin/env python3
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from metar import STATION_ICAO
from strategy import transaction_cost

DB_PATH = 'backups/bot-20260908T110859Z.db'

def run_honest_backtest(max_entry_price=0.96, dissemination_lag_sec=60):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print('Loading settled markets from index...')
    query = (
        'SELECT DISTINCT market_id, city, target_date, is_high, bucket_low, bucket_high, settled_value, settled_outcome '
        'FROM replay_signals WHERE settled_outcome IS NOT NULL AND settled_value IS NOT NULL'
    )
    markets = cur.execute(query).fetchall()
    print(f'Found {len(markets)} settled market records.')

    trades = []
    seen_buckets = set()
    skipped_no_depth = 0
    skipped_repriced = 0
    skipped_no_postclose_ticks = 0

    for m in markets:
        m_id = m['market_id']
        city = m['city']
        td = m['target_date']
        is_high = bool(m['is_high'])
        b_lo = m['bucket_low']
        b_hi = m['bucket_high']
        s_out = m['settled_outcome']
        s_val = m['settled_value']

        stn_info = STATION_ICAO.get(city)
        if not stn_info:
            continue
        icao, tz_name = stn_info

        try:
            yr, mon, dy = map(int, td.split('-'))
            local_day_end = datetime(yr, mon, dy, 23, 59, 59, tzinfo=ZoneInfo(tz_name))
            t_day_end_utc = local_day_end.astimezone(timezone.utc)
        except Exception:
            continue

        t_earliest_exec = t_day_end_utc + timedelta(seconds=dissemination_lag_sec)
        winning_side = s_out

        bucket_key = (city, td, is_high, b_lo, b_hi)
        if bucket_key in seen_buckets:
            continue

        tick_query = (
            'SELECT id, timestamp, best_ask, yes_price, no_price, walked_vwap, '
            'usable_depth_usd, ask_depth_usd, spread_fraction '
            'FROM replay_signals WHERE market_id = ? AND timestamp >= ? '
            'ORDER BY timestamp ASC'
        )
        ticks = cur.execute(tick_query, (m_id, t_earliest_exec.isoformat())).fetchall()

        if not ticks:
            skipped_no_postclose_ticks += 1
            continue

        for tick in ticks:
            if winning_side == 'NO':
                vwap = tick['walked_vwap']
                px = float(vwap if (vwap and vwap > 0) else (tick['no_price'] or 0.0))
            else:
                px = float(tick['yes_price'] or (tick['best_ask'] or 0.0))

            depth = float(tick['usable_depth_usd'] or tick['ask_depth_usd'] or 0.0)

            if px <= 0.0 or px > max_entry_price:
                skipped_repriced += 1
                continue

            if depth <= 0.0:
                skipped_no_depth += 1
                continue

            stake = min(depth, 10.0)
            if stake < 0.50:
                continue

            shares = stake / px
            spread_f = float(tick['spread_fraction'] or 0.02)
            fee = transaction_cost(px, spread_f) * shares

            is_win = (s_out == winning_side)
            gross_pnl = (shares * 1.0 - stake) if is_win else (-stake)
            net_pnl = gross_pnl - fee

            seen_buckets.add(bucket_key)
            ts_str = tick['timestamp'].replace('+00:00','Z').replace('Z','+00:00')
            tick_dt = datetime.fromisoformat(ts_str)
            if tick_dt.tzinfo is None:
                tick_dt = tick_dt.replace(tzinfo=timezone.utc)
            mins_post = (tick_dt - t_day_end_utc).total_seconds() / 60.0

            trades.append({
                'city': city,
                'target_date': td,
                'is_high': is_high,
                'bucket': f'{b_lo}-{b_hi}',
                'side': winning_side,
                'tick_time_utc': tick['timestamp'],
                'local_day_end_utc': t_day_end_utc.isoformat(),
                'mins_post_close': mins_post,
                'executed_price': px,
                'depth_available': depth,
                'stake_usd': stake,
                'fee_usd': fee,
                'net_pnl_usd': net_pnl,
                'roi_pct': (net_pnl / stake) * 100.0,
                'won': is_win
            })
            break

    conn.close()

    print('=' * 95)
    print('🎯 HONEST POST-CLOSE STALE LIQUIDITY ARBITRAGE BACKTEST REPORT')
    print('=' * 95)
    print(f'Total markets checked post-close: {len(markets)}')
    print(f'Markets without post-close ticks: {skipped_no_postclose_ticks}')
    print(f'Total trades executed:            {len(trades)}')
    
    if not trades:
        print('No trades met the strict post-close criteria.')
        return

    wins = [t for t in trades if t['won']]
    losses = [t for t in trades if not t['won']]
    total_staked = sum(t['stake_usd'] for t in trades)
    total_net_pnl = sum(t['net_pnl_usd'] for t in trades)
    total_fees = sum(t['fee_usd'] for t in trades)
    avg_price = sum(t['executed_price'] for t in trades) / len(trades)
    avg_mins_post = sum(t['mins_post_close'] for t in trades) / len(trades)

    print(f'Win count / Loss count:           {len(wins)}W / {len(losses)}L')
    print(f'Win Rate:                         {(len(wins) / len(trades))*100:.1f}%')
    print(f'Total Staked:                     ${total_staked:,.2f}')
    print(f'Total Net Profit:                 ${total_net_pnl:+,.2f}')
    print(f'Total Fees Paid:                  ${total_fees:,.2f}')
    print(f'Overall Portfolio ROI:            {(total_net_pnl / total_staked)*100:+.2f}%')
    print(f'Average Executed Price (VWAP):    ${avg_price:.3f}')
    print(f'Average Time Post Local Midnight: {avg_mins_post:.1f} minutes')
    print('-' * 95)
    header = f'{"City":<14} {"Date":<11} {"Side":<5} {"Bucket":<11} {"Exec Px":<8} {"Stake":<8} {"Post(m)":<8} {"Net P&L":<10} {"ROI%":<8}'
    print(header)
    print('-' * 95)
    for t in trades[:40]:
        line = f"{t['city']:<14} {t['target_date']:<11} {t['side']:<5} {t['bucket']:<11} ${t['executed_price']:<7.3f} ${t['stake_usd']:<7.2f} {t['mins_post_close']:<8.1f} ${t['net_pnl_usd']:<+9.2f} {t['roi_pct']:<+7.1f}%"
        print(line)
    if len(trades) > 40:
        print(f'... and {len(trades) - 40} more trades.')
    print('=' * 95)

if __name__ == '__main__':
    run_honest_backtest()
