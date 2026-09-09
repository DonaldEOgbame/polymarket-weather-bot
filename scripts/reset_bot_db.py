#!/usr/bin/env python3
"""
scripts/reset_bot_db.py - Reset bot database, clear archives, and seed $10 paper bankroll.
"""
import os
import sys
import sqlite3
from datetime import datetime, timezone

def reset_database(db_path: str, starting_bankroll: float = 10.0):
    print(f'Connecting to database at {db_path}...')
    if not os.path.exists(db_path):
        print(f'Database at {db_path} does not exist.')
        return
    
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    print(f'Found tables: {tables}')
    
    tables_to_clear = [
        'trades', 'positions', 'bankroll', 'signals', 'sniper_audit', 'scan_log',
        'orderbook_snapshots', 'replay_signals', 'replay_gates', 'position_trail',
        'position_trail_rules', 'notifications', 'armed_signals', 'impossible_buckets',
        'model_accuracy', 'resolutions', 'settings', 'eras'
    ]
    
    for t in tables_to_clear:
        if t in tables:
            cur.execute(f'DELETE FROM {t}')
            print(f'Cleared table {t}')
            
    now = datetime.now(timezone.utc).isoformat()
    if 'bankroll' in tables:
        cur.execute(
            'INSERT INTO bankroll (timestamp, event, amount, balance, trade_id, mode) VALUES (?, ?, ?, ?, ?, ?)',
            (now, 'SEED', starting_bankroll, starting_bankroll, None, 'paper')
        )
        print(f'Seeded bankroll table with ${starting_bankroll:.2f} in paper mode.')

    if 'settings' in tables:
        cur.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('PAPER_MODE', 'true', ?)", (now,))
        cur.execute(f"INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('STARTING_BANKROLL', '{starting_bankroll:.1f}', ?)", (now,))
        cur.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('MAX_ENTRY_PRICE', '0.96', ?)", (now,))
        cur.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('PAUSE_SCANNING', 'false', ?)", (now,))
        print('Initialized settings with PAPER_MODE=true, STARTING_BANKROLL=10.0, MAX_ENTRY_PRICE=0.96')

    conn.commit()
    print('Running VACUUM to reclaim disk space...')
    cur.execute('VACUUM')
    conn.commit()
    conn.close()
    print(f'Database {db_path} successfully reset and vacuumed!')

if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else 'data/bot.db'
    bankroll = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    reset_database(target, bankroll)
