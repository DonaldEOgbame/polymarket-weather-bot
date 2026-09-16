import sys
sys.path.insert(0, '.')
from scripts.analyze_london_liquidity import analyze_window_set
res = analyze_window_set('Check', (13, 17), (4, 8))
print('\nDETAILED BREAKDOWN OF THE 31 TRADES:')
for m in res:
    clr = f"{m['clearance_f']:.1f}F" if m['clearance_f'] is not None else 'None'
    print(f"{m['target_date']} | {'HIGH' if m['is_high'] else 'LOW '} | {m['bucket_label']:16s} | Vol:${m['vol_usd']:7.1f} | Entry:{m['entry_price']:.3f} | Min:{m['min_subsequent_price']:.3f} | DD:{m['max_drawdown_cents']*100:4.1f}c | Clear:{clr:6s} | Pass:{str(m['gate_passed']):5s} | Won:{str(m['is_winner'])}")
