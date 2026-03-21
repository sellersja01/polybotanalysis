import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

# All available databases
DBS = {
    'BTC_5m':   r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m':  r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':   r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m':  r'C:\Users\James\polybotanalysis\market_eth_15m.db',
    'SOL_5m':   r'C:\Users\James\polybotanalysis\market_sol_5m.db',
    'SOL_15m':  r'C:\Users\James\polybotanalysis\market_sol_15m.db',
    'XRP_5m':   r'C:\Users\James\polybotanalysis\market_xrp_5m.db',
    'XRP_15m':  r'C:\Users\James\polybotanalysis\market_xrp_15m.db',
    # Test databases (recent data)
    'BTC_5m_test':  r'C:\Users\James\polybotanalysis\market_btc_5m_test.db',
    'BTC_15m_test': r'C:\Users\James\polybotanalysis\market_btc_15m_test.db',
    'ETH_5m_test':  r'C:\Users\James\polybotanalysis\market_eth_5m_test.db',
    'ETH_15m_test': r'C:\Users\James\polybotanalysis\market_eth_15m_test.db',
}

CANDLE_INTERVALS = {
    '5m': 300, '15m': 900,
    '5m_test': 300, '15m_test': 900,
}

# Strategy params
BET_PER_ORDER = 10.0      # $10 per individual buy order
BUY_INTERVAL  = 30        # buy every 30 seconds
SIZE_CHEAP_MULT = 3.0     # buy 3x more of cheap side when it drops below 0.30
CHEAP_THRESHOLD = 0.30    # when to start buying more of losing side

def get_tf(label):
    parts = label.lower().split('_')
    if 'test' in parts:
        return parts[1] + '_test'
    return parts[1]

def run_strategy(db_path, label):
    tf_key = get_tf(label)
    interval = CANDLE_INTERVALS.get(tf_key, 300)

    try:
        conn = sqlite3.connect(db_path)
        # Only load last 48h to avoid OOM
        max_ts = conn.execute('SELECT MAX(unix_time) FROM polymarket_odds').fetchone()[0]
        if not max_ts:
            conn.close()
            return None
        rows = conn.execute('''
            SELECT unix_time, market_id, outcome, bid, ask, mid
            FROM polymarket_odds
            WHERE unix_time >= ? AND outcome IN ('Up','Down')
            ORDER BY unix_time ASC
        ''', (float(max_ts) - 172800,)).fetchall()
        conn.close()
    except Exception as e:
        print(f"  {label}: SKIP — {e}")
        return None

    if not rows:
        return None

    # Group by candle
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, market_id, outcome, bid, ask, mid in rows:
        if not ask or float(ask) <= 0: continue
        candle_start = (int(float(ts)) // interval) * interval
        key = (candle_start, market_id)
        candles[key][outcome].append({
            'ts': float(ts),
            'ask': float(ask),
            'bid': float(bid) if bid else 0,
            'mid': float(mid) if mid else 0,
        })

    wins = losses = 0
    total_pnl = 0.0
    candle_results = []

    for (candle_start, market_id), sides in candles.items():
        up_rows = sides['Up']
        down_rows = sides['Down']
        if not up_rows or not down_rows: continue

        # Check resolution
        final_up = up_rows[-1]['mid']
        if final_up >= 0.85:
            resolved = 'Up'
        elif final_up <= 0.15:
            resolved = 'Down'
        else:
            continue  # unresolved

        # Simulate continuous buying every BUY_INTERVAL seconds
        # Buy both sides throughout the candle
        # Size up the cheap side when it drops below CHEAP_THRESHOLD
        
        up_buys = []    # list of (price, shares, cost)
        down_buys = []

        candle_end = candle_start + interval
        buy_times = list(range(candle_start + 5, candle_end - 10, BUY_INTERVAL))

        for buy_ts in buy_times:
            # Find closest Up row
            up_close = min(up_rows, key=lambda r: abs(r['ts'] - buy_ts), default=None)
            down_close = min(down_rows, key=lambda r: abs(r['ts'] - buy_ts), default=None)
            if not up_close or not down_close: continue
            if abs(up_close['ts'] - buy_ts) > 30: continue
            if abs(down_close['ts'] - buy_ts) > 30: continue

            up_ask = up_close['ask']
            down_ask = down_close['ask']

            # Skip if already nearly resolved
            if up_ask >= 0.95 or down_ask >= 0.95: continue
            if up_ask <= 0.05 or down_ask <= 0.05: continue

            # Buy equal SHARES on both sides every interval
            # 20 shares per order (BET_PER_ORDER / 0.50 fair value)
            target_shares = BET_PER_ORDER / 0.50

            # Buy Up — fixed shares, variable cost
            up_cost = target_shares * up_ask
            up_buys.append((up_ask, target_shares, up_cost))

            # Buy Down — same number of shares
            down_cost = target_shares * down_ask
            down_buys.append((down_ask, target_shares, down_cost))

        if not up_buys or not down_buys: continue

        # Calculate averages
        total_up_cost = sum(b[2] for b in up_buys)
        total_down_cost = sum(b[2] for b in down_buys)
        total_up_shares = sum(b[1] for b in up_buys)
        total_down_shares = sum(b[1] for b in down_buys)

        avg_up = total_up_cost / total_up_shares
        avg_down = total_down_cost / total_down_shares
        combined_avg = avg_up + avg_down

        # Payout
        if resolved == 'Up':
            payout = total_up_shares * 1.0
            cost = total_up_cost + total_down_cost
        else:
            payout = total_down_shares * 1.0
            cost = total_up_cost + total_down_cost

        pnl = payout - cost

        if pnl > 0:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl

        candle_results.append({
            'candle_start': candle_start,
            'resolved': resolved,
            'avg_up': avg_up,
            'avg_down': avg_down,
            'combined': combined_avg,
            'total_cost': cost,
            'payout': payout,
            'pnl': pnl,
            'n_up_buys': len(up_buys),
            'n_down_buys': len(down_buys),
        })

    return {
        'label': label,
        'candles': len(candle_results),
        'wins': wins,
        'losses': losses,
        'total_pnl': total_pnl,
        'results': candle_results,
    }

# Run on all databases
print(f"CONTINUOUS AVERAGING STRATEGY BACKTEST")
print(f"Buy every {BUY_INTERVAL}s | ${BET_PER_ORDER}/order | {SIZE_CHEAP_MULT}x size when side < {CHEAP_THRESHOLD}")
print(f"{'='*70}\n")

all_results = []
for label, db_path in DBS.items():
    print(f"Running {label}...", end=" ", flush=True)
    result = run_strategy(db_path, label)
    if not result:
        print("SKIP")
        continue
    
    total = result['wins'] + result['losses']
    wr = 100 * result['wins'] // max(total, 1)
    avg_pnl = result['total_pnl'] / max(total, 1)
    
    # Avg combined
    avg_combined = sum(r['combined'] for r in result['results']) / max(len(result['results']), 1)
    
    print(f"{result['candles']} candles | WR={wr}% | PnL=${result['total_pnl']:+.2f} | avg/trade=${avg_pnl:+.2f} | avg_combined={avg_combined:.3f}")
    all_results.append(result)

# Overall summary
print(f"\n{'='*70}")
print(f"SUMMARY")
print(f"{'='*70}")
print(f"\n{'Market':<15} {'Candles':>8} {'WR':>6} {'Total PnL':>12} {'Avg/trade':>10} {'Avg Combined':>14}")
print(f"-"*65)
for r in all_results:
    total = r['wins'] + r['losses']
    wr = 100 * r['wins'] // max(total, 1)
    avg_pnl = r['total_pnl'] / max(total, 1)
    avg_combined = sum(x['combined'] for x in r['results']) / max(len(r['results']), 1)
    marker = " ✓" if r['total_pnl'] > 0 else ""
    print(f"{r['label']:<15} {total:>8} {wr:>5}% {r['total_pnl']:>+12.2f} {avg_pnl:>+10.2f} {avg_combined:>14.3f}{marker}")

print(f"\nKey insight: if avg_combined < 1.000 the strategy is mathematically profitable")
print(f"If avg_combined > 1.000 the continuous buying approach loses money on average")
