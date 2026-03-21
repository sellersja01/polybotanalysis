import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DBS = {
    'BTC_5m':   r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m':  r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':   r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m':  r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}

CANDLE_INTERVALS = {'5m': 300, '15m': 900}

# Strategy params
SHARES_PER_ORDER = 20      # shares per limit order
LIMIT_OFFSET     = 0.02    # place limit BID this far below current mid
                            # e.g. mid=0.50 → limit bid at 0.48
MAX_ORDERS       = 10      # max orders per side per candle
MIN_FILL_PRICE   = 0.05    # don't place limits below this
MAX_FILL_PRICE   = 0.95    # don't place limits above this

def run_strategy(db_path, label, limit_offset=LIMIT_OFFSET):
    tf = '15m' if '15m' in label else '5m'
    interval = CANDLE_INTERVALS[tf]

    try:
        conn = sqlite3.connect(db_path)
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
            'bid': float(bid) if bid else 0,
            'ask': float(ask),
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
            continue

        # Simulate placing limit BID orders below current mid
        # A limit bid gets filled when the ask drops to our bid price
        # i.e. when someone wants to sell at our price
        
        up_fills = []
        down_fills = []
        
        up_order_count = 0
        down_order_count = 0

        candle_end = candle_start + interval
        
        # Place a new limit order every 30 seconds
        place_times = list(range(candle_start + 5, candle_end - 10, 30))

        for place_ts in place_times:
            # Get current market state
            up_now = min(up_rows, key=lambda r: abs(r['ts'] - place_ts), default=None)
            down_now = min(down_rows, key=lambda r: abs(r['ts'] - place_ts), default=None)
            if not up_now or not down_now: continue
            if abs(up_now['ts'] - place_ts) > 30: continue

            up_mid = up_now['mid']
            down_mid = down_now['mid']

            # Don't place orders near resolution
            if up_mid >= MAX_FILL_PRICE or down_mid >= MAX_FILL_PRICE: continue
            if up_mid <= MIN_FILL_PRICE or down_mid <= MIN_FILL_PRICE: continue

            # Place limit bids BELOW current mid
            up_limit = round(up_mid - limit_offset, 3)
            down_limit = round(down_mid - limit_offset, 3)

            if up_limit <= MIN_FILL_PRICE or down_limit <= MIN_FILL_PRICE: continue

            # Check if limit gets filled — scan forward in time to see if
            # the bid price ever becomes the ask price (i.e. market moves to us)
            # A limit bid fills when the ask drops to our limit price or below
            
            if up_order_count < MAX_ORDERS:
                # Look for fill in next 60 seconds
                fill_window = [r for r in up_rows if place_ts <= r['ts'] <= place_ts + 60]
                filled = any(r['ask'] <= up_limit for r in fill_window)
                if filled:
                    up_fills.append(up_limit)
                    up_order_count += 1

            if down_order_count < MAX_ORDERS:
                fill_window = [r for r in down_rows if place_ts <= r['ts'] <= place_ts + 60]
                filled = any(r['ask'] <= down_limit for r in fill_window)
                if filled:
                    down_fills.append(down_limit)
                    down_order_count += 1

        # Track single leg outcomes
        if not up_fills and not down_fills: continue

        EXIT_THRESHOLD = 0.90  # if either side crosses this with only one leg, exit loser

        if not up_fills or not down_fills:
            # Single leg — odds-based exit
            # Find the moment when the winning side crossed 0.90
            # and check what price the losing side was at that moment
            if up_fills:
                # We have Up fills, no Down fills
                # Find when Down crossed 0.90 (meaning Down won = Up lost)
                # or when Up crossed 0.90 (meaning Up won = we're fine)
                avg_price = sum(up_fills) / len(up_fills)
                shares = len(up_fills) * SHARES_PER_ORDER
                cost = avg_price * shares

                # Find when either side crossed 0.90
                down_won_ts = next((r['ts'] for r in down_rows if r['mid'] >= EXIT_THRESHOLD), None)
                up_won_ts = next((r['ts'] for r in up_rows if r['mid'] >= EXIT_THRESHOLD), None)

                if down_won_ts:
                    # Down is winning — exit Up at whatever Up mid is at that moment
                    up_at_exit = next((r['mid'] for r in up_rows if r['ts'] >= down_won_ts), 0.05)
                    pnl = (up_at_exit - avg_price) * shares
                elif up_won_ts:
                    # Up won — we collect
                    pnl = shares * 1.0 - cost
                else:
                    payout = shares * 1.0 if resolved == 'Up' else 0
                    pnl = payout - cost
            else:
                # We have Down fills, no Up fills
                avg_price = sum(down_fills) / len(down_fills)
                shares = len(down_fills) * SHARES_PER_ORDER
                cost = avg_price * shares

                up_won_ts = next((r['ts'] for r in up_rows if r['mid'] >= EXIT_THRESHOLD), None)
                down_won_ts = next((r['ts'] for r in down_rows if r['mid'] >= EXIT_THRESHOLD), None)

                if up_won_ts:
                    # Up is winning — exit Down at whatever Down mid is at that moment
                    down_at_exit = next((r['mid'] for r in down_rows if r['ts'] >= up_won_ts), 0.05)
                    pnl = (down_at_exit - avg_price) * shares
                elif down_won_ts:
                    pnl = shares * 1.0 - cost
                else:
                    payout = shares * 1.0 if resolved == 'Down' else 0
                    pnl = payout - cost

            if pnl > 0: wins += 1
            else: losses += 1
            total_pnl += pnl
            candle_results.append({
                'candle_start': candle_start, 'resolved': resolved,
                'avg_up': sum(up_fills)/len(up_fills) if up_fills else 0,
                'avg_down': sum(down_fills)/len(down_fills) if down_fills else 0,
                'combined': 999, 'n_up_fills': len(up_fills),
                'n_down_fills': len(down_fills), 'pnl': pnl, 'single_leg': True,
            })
            continue

        # Equal shares on both sides — match to minimum fill count
        n_shares = min(len(up_fills), len(down_fills)) * SHARES_PER_ORDER

        avg_up_fill = sum(up_fills) / len(up_fills)
        avg_down_fill = sum(down_fills) / len(down_fills)
        combined_avg = avg_up_fill + avg_down_fill

        total_up_cost = avg_up_fill * n_shares
        total_down_cost = avg_down_fill * n_shares
        total_cost = total_up_cost + total_down_cost

        # Payout = n_shares * $1.00 on winning side
        payout = n_shares * 1.0
        pnl = payout - total_cost

        if pnl > 0:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl

        candle_results.append({
            'candle_start': candle_start,
            'resolved': resolved,
            'avg_up': avg_up_fill,
            'avg_down': avg_down_fill,
            'combined': combined_avg,
            'n_up_fills': len(up_fills),
            'n_down_fills': len(down_fills),
            'pnl': pnl,
        })

    return {
        'label': label,
        'candles': len(candle_results),
        'wins': wins,
        'losses': losses,
        'total_pnl': total_pnl,
        'results': candle_results,
    }

OFFSETS_TO_TEST = [0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15]

print(f"LIMIT ORDER MARKET MAKING — OFFSET OPTIMIZER")
print(f"Testing offsets: {OFFSETS_TO_TEST}")
print(f"{SHARES_PER_ORDER} shares/order | max {MAX_ORDERS} orders/side")
print(f"{'='*100}\n")

print(f"{'Offset':<8} {'Market':<12} {'Candles':>8} {'WR':>6} {'Net PnL':>10} {'Both PnL':>10} {'Single PnL':>11} {'Avg Comb':>10} {'Fills':>6}")
print(f"-"*90)

best_by_offset = {}

for offset in OFFSETS_TO_TEST:
    offset_total = 0.0
    for label, db_path in DBS.items():
        result = run_strategy(db_path, label, limit_offset=offset)
        if not result:
            continue
        total = result['wins'] + result['losses']
        wr = 100 * result['wins'] // max(total, 1)
        both_leg = [x for x in result['results'] if not x.get('single_leg')]
        single_leg = [x for x in result['results'] if x.get('single_leg')]
        avg_combined = sum(x['combined'] for x in both_leg) / max(len(both_leg), 1)
        avg_fills = sum(x['n_up_fills'] + x['n_down_fills'] for x in result['results']) / max(len(result['results']), 1)
        both_pnl = sum(x['pnl'] for x in both_leg)
        single_pnl = sum(x['pnl'] for x in single_leg)
        offset_total += result['total_pnl']
        marker = " ✓" if result['total_pnl'] > 0 else ""
        print(f"{offset:<8} {label:<12} {total:>8} {wr:>5}% {result['total_pnl']:>+10.2f} {both_pnl:>+10.2f} {single_pnl:>+11.2f} {avg_combined:>10.3f} {avg_fills:>6.1f}{marker}")

    best_by_offset[offset] = offset_total
    print(f"{'':8} {'TOTAL':<12} {'':>8} {'':>6} {offset_total:>+10.2f}")
    print()

print(f"\n{'='*50}")
print(f"BEST OFFSET SUMMARY")
print(f"{'='*50}")
for offset, total_pnl in sorted(best_by_offset.items(), key=lambda x: x[1], reverse=True):
    marker = " ← BEST" if offset == max(best_by_offset, key=best_by_offset.get) else ""
    print(f"  Offset {offset:.2f}: ${total_pnl:+.2f}{marker}")
