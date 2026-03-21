import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

# Use test databases
DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m_test.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m_test.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m_test.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m_test.db',
}

CANDLE_INTERVALS = {'5m': 300, '15m': 900}
OPEN_WINDOW = 30      # seconds after candle open to look for arb
MIN_EDGE    = 0.02    # minimum edge (combined must be < 1.00 - this)

def get_candles(db_path, interval):
    """Load all rows and group by candle"""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute('''
            SELECT unix_time, market_id, outcome, bid, ask, mid
            FROM polymarket_odds
            ORDER BY unix_time ASC
        ''').fetchall()
        conn.close()
    except Exception as e:
        print(f"  Error: {e}")
        return {}

    # Group by candle bucket
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, market_id, outcome, bid, ask, mid in rows:
        if outcome not in ('Up', 'Down'): continue
        if not ask or float(ask) <= 0: continue
        candle_start = (int(float(ts)) // interval) * interval
        key = (candle_start, market_id)
        candles[key][outcome].append({
            'ts': float(ts),
            'bid': float(bid) if bid else 0,
            'ask': float(ask),
            'mid': float(mid) if mid else 0,
        })

    return candles

results = []

for label, db_path in DBS.items():
    tf = label.split('_')[1]
    interval = CANDLE_INTERVALS[tf]
    
    print(f"\nScanning {label}...")
    candles = get_candles(db_path, interval)
    print(f"  Loaded {len(candles)} candles")

    opps = 0
    total = 0

    for (candle_start, market_id), sides in candles.items():
        up_rows = sides['Up']
        down_rows = sides['Down']
        if not up_rows or not down_rows: continue

        # Get resolution
        final_up = up_rows[-1]['mid']
        resolved = None
        if final_up >= 0.85:
            resolved = 'Up'
        elif final_up <= 0.15:
            resolved = 'Down'
        if not resolved: continue

        total += 1

        # Find minimum combined ask in first OPEN_WINDOW seconds
        open_end = candle_start + OPEN_WINDOW

        up_open = [r for r in up_rows if r['ts'] <= open_end]
        down_open = [r for r in down_rows if r['ts'] <= open_end]

        if not up_open or not down_open: continue

        # Find the moment with lowest combined ask in open window
        # Match up and down rows by closest timestamp
        best_combined = 999
        best_up_ask = None
        best_down_ask = None
        best_ts = None

        for up_r in up_open:
            # Find closest down row within 2 seconds
            close_down = [d for d in down_open if abs(d['ts'] - up_r['ts']) <= 2]
            if not close_down: continue
            closest_down = min(close_down, key=lambda d: abs(d['ts'] - up_r['ts']))
            combined = up_r['ask'] + closest_down['ask']
            if combined < best_combined:
                best_combined = combined
                best_up_ask = up_r['ask']
                best_down_ask = closest_down['ask']
                best_ts = up_r['ts']

        if best_combined >= 1.0 - MIN_EDGE: continue

        opps += 1
        edge = 1.0 - best_combined
        offset = best_ts - candle_start if best_ts else 0

        candle_time = datetime.fromtimestamp(candle_start, tz=timezone.utc).strftime('%H:%M')
        results.append({
            'label': label,
            'candle_time': candle_time,
            'candle_start': candle_start,
            'up_ask': best_up_ask,
            'down_ask': best_down_ask,
            'combined': best_combined,
            'edge': edge,
            'offset_s': round(offset),
            'resolved': resolved,
        })

    pct = 100*opps//max(total,1)
    print(f"  {opps}/{total} candles ({pct}%) had combined < {1.0-MIN_EDGE:.2f} in first {OPEN_WINDOW}s")

# Print results
print(f"\n{'='*80}")
print(f"ARB OPPORTUNITIES AT CANDLE OPEN (first {OPEN_WINDOW}s, edge > {MIN_EDGE*100:.0f}¢)")
print(f"{'='*80}")
print(f"\n{'Market':<10} {'Time':<7} {'Up$':<7} {'Dn$':<7} {'Comb':<7} {'Edge':<7} {'Offset':<8} {'Result'}")
print("-"*65)

for r in sorted(results, key=lambda x: x['candle_start']):
    print(f"{r['label']:<10} {r['candle_time']:<7} {r['up_ask']:<7.3f} {r['down_ask']:<7.3f} {r['combined']:<7.3f} {r['edge']*100:+.1f}¢  +{r['offset_s']:<7}s {r['resolved']}")

# Summary stats
if results:
    total_opps = len(results)
    avg_edge = sum(r['edge'] for r in results) / total_opps
    avg_offset = sum(r['offset_s'] for r in results) / total_opps
    under_10s = sum(1 for r in results if r['offset_s'] <= 10)
    
    print(f"\n{'='*50}")
    print(f"Total opportunities: {total_opps}")
    print(f"Avg edge: {avg_edge*100:.2f}¢")
    print(f"Avg offset from open: {avg_offset:.1f}s")
    print(f"Under 10s: {under_10s} ({100*under_10s//total_opps}%)")
    
    # Simulate profit
    BET = 100  # $100 per leg
    total_profit = sum(r['edge'] * BET for r in results)
    print(f"\nSimulated profit at $100/leg: ${total_profit:.2f} over {total_opps} opportunities")
    print(f"That's ${total_profit/max(1, (results[-1]['candle_start']-results[0]['candle_start'])/3600):.2f}/hour")
else:
    print("\nNo opportunities found — market is too efficient at candle open")
    print("Try increasing OPEN_WINDOW or decreasing MIN_EDGE")
