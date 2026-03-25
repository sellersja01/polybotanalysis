import sqlite3
from collections import defaultdict

conn = sqlite3.connect('/home/opc/wallet_trades.db')

rows = conn.execute("""
    SELECT time_utc, timestamp, side, outcome, price, size, usdc, market
    FROM trades
    WHERE wallet_name='boshbashbish'
      AND side='BUY' AND price > 0 AND size > 0
    ORDER BY timestamp ASC
""").fetchall()
conn.close()

print('Valid BUY trades:', len(rows))

candles = defaultdict(lambda: {'Up': [], 'Down': []})
for time_utc, ts, side, outcome, price, size, usdc, market in rows:
    if outcome in ('Up', 'Down') and market:
        candles[market][outcome].append((price, size, time_utc))

print('Unique candles:', len(candles))

results = []
for market, sides in candles.items():
    up = sides['Up']
    dn = sides['Down']
    if not up or not dn:
        continue

    up_shares = sum(s for p, s, t in up)
    dn_shares = sum(s for p, s, t in dn)
    up_cost   = sum(p*s for p, s, t in up)
    dn_cost   = sum(p*s for p, s, t in dn)
    avg_up    = up_cost / up_shares
    avg_dn    = dn_cost / dn_shares
    combined  = avg_up + avg_dn
    total_cost = up_cost + dn_cost

    all_times = sorted([t for p,s,t in up] + [t for p,s,t in dn])

    results.append({
        'market': market,
        'up_sh': up_shares, 'dn_sh': dn_shares,
        'avg_up': avg_up, 'avg_dn': avg_dn,
        'combined': combined,
        'total_cost': total_cost,
        'n_up': len(up), 'n_dn': len(dn),
        'first_trade': all_times[0],
        'last_trade': all_times[-1],
        'arb': combined < 1.0,
    })

arb    = [r for r in results if r['arb']]
no_arb = [r for r in results if not r['arb']]

print('\nCandles with BOTH sides:', len(results))
print('  Combined < 1.00 (profit guaranteed):', len(arb), '(%.1f%%)' % (100*len(arb)/len(results)))
print('  Combined >= 1.00 (no arb):          ', len(no_arb), '(%.1f%%)' % (100*len(no_arb)/len(results)))

print('\nARB candles (combined < 1.00):')
print('  Avg combined:      %.4f' % (sum(r['combined'] for r in arb)/len(arb)))
print('  Avg up shares:     %.0f' % (sum(r['up_sh'] for r in arb)/len(arb)))
print('  Avg dn shares:     %.0f' % (sum(r['dn_sh'] for r in arb)/len(arb)))
print('  Avg total cost:   $%.2f' % (sum(r['total_cost'] for r in arb)/len(arb)))
print('  Avg trades/candle: %.1f' % (sum(r['n_up']+r['n_dn'] for r in arb)/len(arb)))

print('\nNO-ARB candles (combined >= 1.00):')
print('  Avg combined:      %.4f' % (sum(r['combined'] for r in no_arb)/len(no_arb)))
print('  Avg total cost:   $%.2f' % (sum(r['total_cost'] for r in no_arb)/len(no_arb)))
print('  Avg trades/candle: %.1f' % (sum(r['n_up']+r['n_dn'] for r in no_arb)/len(no_arb)))

print('\nCombined distribution:')
buckets = [(0,0.90,'< 0.90'),(0.90,0.95,'0.90-0.95'),(0.95,1.00,'0.95-1.00'),(1.00,1.05,'1.00-1.05'),(1.05,2.0,'> 1.05')]
for lo, hi, label in buckets:
    grp = [r for r in results if lo <= r['combined'] < hi]
    if grp:
        print('  %-12s %3d candles  avg_combined=%.4f  avg_cost=$%.2f  avg_trades=%.1f' % (
            label, len(grp),
            sum(r['combined'] for r in grp)/len(grp),
            sum(r['total_cost'] for r in grp)/len(grp),
            sum(r['n_up']+r['n_dn'] for r in grp)/len(grp)
        ))

print('\nSample best ARB candles (lowest combined):')
for r in sorted(arb, key=lambda x: x['combined'])[:8]:
    print('  combined=%.4f  up=%.0fsh@%.3f  dn=%.0fsh@%.3f  trades=%d  %s' % (
        r['combined'], r['up_sh'], r['avg_up'], r['dn_sh'], r['avg_dn'],
        r['n_up']+r['n_dn'], r['market'][:55]))

print('\nSample worst NO-ARB candles (highest combined):')
for r in sorted(no_arb, key=lambda x: x['combined'], reverse=True)[:5]:
    print('  combined=%.4f  up=%.0fsh@%.3f  dn=%.0fsh@%.3f  trades=%d  %s' % (
        r['combined'], r['up_sh'], r['avg_up'], r['dn_sh'], r['avg_dn'],
        r['n_up']+r['n_dn'], r['market'][:55]))
