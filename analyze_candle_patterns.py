"""
Candle-level pattern analysis.

Groups trades by exact candle (market title), then analyzes:
- What price did they first enter each side?
- Did they buy Up first or Down first?
- How do prices evolve within the candle (DCA pattern)?
- What triggers a new buy — price drop on one side?
- Are they averaging down, up, or random?
"""

import csv
import os
from collections import defaultdict

WALLETS_DIR = os.path.join(os.path.dirname(__file__), 'Wallets_new')


def load_csv(wallet_name):
    path = os.path.join(WALLETS_DIR, f'{wallet_name}.csv')
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def analyze_candle_patterns(wallet_name):
    rows = load_csv(wallet_name)
    buys = [r for r in rows if r['side'].upper() == 'BUY']

    # Group by candle (market title)
    candles = defaultdict(list)
    for r in buys:
        candles[r['market']].append(r)

    # Sort trades within each candle by timestamp
    for market in candles:
        candles[market].sort(key=lambda r: int(float(r['timestamp'])))

    print(f"\n{'='*60}")
    print(f"  {wallet_name.upper()} — Candle Pattern Analysis")
    print(f"  Total candles: {len(candles)}")
    print(f"{'='*60}")

    # ── Per-candle stats ──────────────────────────────────────────
    first_up_prices = []
    first_dn_prices = []
    up_first_count  = 0
    dn_first_count  = 0
    both_same_ts    = 0

    avg_down_up  = 0  # averaging down on Up side
    avg_down_dn  = 0  # averaging down on Down side
    avg_up_up    = 0  # averaging UP on Up side (chasing)
    avg_up_dn    = 0  # averaging UP on Down side

    price_drop_triggers = []  # how much price dropped between consecutive buys on same side

    for market, trades in candles.items():
        up_trades = [t for t in trades if t['outcome'].lower() == 'up']
        dn_trades = [t for t in trades if t['outcome'].lower() == 'down']

        # First entry prices
        if up_trades:
            first_up_prices.append(float(up_trades[0]['price']))
        if dn_trades:
            first_dn_prices.append(float(dn_trades[0]['price']))

        # Which side entered first?
        if up_trades and dn_trades:
            up_ts = int(float(up_trades[0]['timestamp']))
            dn_ts = int(float(dn_trades[0]['timestamp']))
            if up_ts < dn_ts:
                up_first_count += 1
            elif dn_ts < up_ts:
                dn_first_count += 1
            else:
                both_same_ts += 1

        # Averaging down or up? Look at price sequence per side
        for side_trades in [up_trades, dn_trades]:
            if len(side_trades) < 2:
                continue
            prices = [float(t['price']) for t in side_trades]
            # Compare consecutive prices
            for i in range(1, len(prices)):
                drop = prices[i-1] - prices[i]  # positive = price dropped = averaging down
                price_drop_triggers.append(drop)
                if prices[i] < prices[i-1]:
                    if side_trades[0]['outcome'].lower() == 'up':
                        avg_down_up += 1
                    else:
                        avg_down_dn += 1
                elif prices[i] > prices[i-1]:
                    if side_trades[0]['outcome'].lower() == 'up':
                        avg_up_up += 1
                    else:
                        avg_up_dn += 1

    total_candles = len(candles)

    # ── First entry price ─────────────────────────────────────────
    print("\n--- First entry price per side ---")
    if first_up_prices:
        avg_up = sum(first_up_prices) / len(first_up_prices)
        med_up = sorted(first_up_prices)[len(first_up_prices)//2]
        print(f"  Up side   — mean: {avg_up:.3f}, median: {med_up:.3f}, min: {min(first_up_prices):.3f}, max: {max(first_up_prices):.3f}")
    if first_dn_prices:
        avg_dn = sum(first_dn_prices) / len(first_dn_prices)
        med_dn = sorted(first_dn_prices)[len(first_dn_prices)//2]
        print(f"  Down side — mean: {avg_dn:.3f}, median: {med_dn:.3f}, min: {min(first_dn_prices):.3f}, max: {max(first_dn_prices):.3f}")

    # ── Which side enters first ───────────────────────────────────
    print("\n--- Which side do they buy first? ---")
    print(f"  Up first       : {up_first_count} ({up_first_count/total_candles*100:.1f}%)")
    print(f"  Down first     : {dn_first_count} ({dn_first_count/total_candles*100:.1f}%)")
    print(f"  Same timestamp : {both_same_ts} ({both_same_ts/total_candles*100:.1f}%)")

    # ── Averaging down vs up ──────────────────────────────────────
    total_consec = avg_down_up + avg_down_dn + avg_up_up + avg_up_dn
    print("\n--- Consecutive buy direction (are they averaging down?) ---")
    if total_consec:
        total_down = avg_down_up + avg_down_dn
        total_up   = avg_up_up + avg_up_dn
        print(f"  Next buy at lower price (avg down) : {total_down} ({total_down/total_consec*100:.1f}%)")
        print(f"  Next buy at higher price (avg up)  : {total_up} ({total_up/total_consec*100:.1f}%)")

    # ── Price drop between consecutive buys ───────────────────────
    if price_drop_triggers:
        drops = [d for d in price_drop_triggers if d > 0]
        rises = [d for d in price_drop_triggers if d < 0]
        same  = [d for d in price_drop_triggers if d == 0]
        print(f"\n--- Price movement between consecutive buys (same side) ---")
        print(f"  Price dropped (avg down) : {len(drops)} ({len(drops)/len(price_drop_triggers)*100:.1f}%)")
        print(f"  Price rose (chasing)     : {len(rises)} ({len(rises)/len(price_drop_triggers)*100:.1f}%)")
        print(f"  Same price               : {len(same)} ({len(same)/len(price_drop_triggers)*100:.1f}%)")
        if drops:
            print(f"  Avg price drop per DCA   : {sum(drops)/len(drops):.4f}")

    # ── Show 5 example candles ────────────────────────────────────
    print("\n--- Example candles (first 5, sorted by # of trades) ---")
    sorted_candles = sorted(candles.items(), key=lambda x: -len(x[1]))[:5]
    for market, trades in sorted_candles:
        up_trades = sorted([t for t in trades if t['outcome'].lower() == 'up'], key=lambda t: int(float(t['timestamp'])))
        dn_trades = sorted([t for t in trades if t['outcome'].lower() == 'down'], key=lambda t: int(float(t['timestamp'])))

        print(f"\n  {market}")
        print(f"  Total buys: {len(trades)} | Up: {len(up_trades)} | Down: {len(dn_trades)}")

        up_prices = [float(t['price']) for t in up_trades]
        dn_prices = [float(t['price']) for t in dn_trades]

        if up_prices:
            print(f"  Up prices  (first->last): {up_prices[0]:.3f} -> ... -> {up_prices[-1]:.3f} | min: {min(up_prices):.3f} max: {max(up_prices):.3f}")
        if dn_prices:
            print(f"  Down prices(first->last): {dn_prices[0]:.3f} -> ... -> {dn_prices[-1]:.3f} | min: {min(dn_prices):.3f} max: {max(dn_prices):.3f}")

        # Show first 10 buys in sequence
        all_sorted = sorted(trades, key=lambda t: int(float(t['timestamp'])))
        seq = [(t['outcome'], float(t['price'])) for t in all_sorted[:12]]
        print(f"  Sequence   : {seq}")


if __name__ == '__main__':
    analyze_candle_patterns('wallet_1')
