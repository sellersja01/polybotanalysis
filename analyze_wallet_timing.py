"""
Wallet Timing & Entry Price Analysis (Points 1 & 2)

Analyzes:
  1. TIMING  — when in the candle do they buy? (seconds after candle open)
  2. ENTRY   — what price do they buy at? one side or both? layers?

Usage:
  python analyze_wallet_timing.py                  # wallet_1 only
  python analyze_wallet_timing.py all              # all wallets
"""

import csv
import sys
import os
from datetime import datetime, timezone
from collections import defaultdict

WALLETS_DIR = os.path.join(os.path.dirname(__file__), 'Wallets_new')

# Candle durations in seconds
CANDLE_DURATIONS = {
    '5m':  5 * 60,
    '15m': 15 * 60,
}

def parse_candle_info(market_title):
    """Extract asset, candle size, and candle open time from market title."""
    title = market_title.lower()
    if 'bitcoin' in title or 'btc' in title:
        asset = 'BTC'
    elif 'ethereum' in title or 'eth' in title:
        asset = 'ETH'
    else:
        asset = 'OTHER'

    if '5m' in title or '5-min' in title or ':00pm' in title or ':05' in title or ':10' in title:
        # Try to detect 5m vs 15m from the time window in title
        pass

    # Detect candle size from title pattern e.g. "6:35PM-6:40PM" = 5m, "6:30PM-6:45PM" = 15m
    import re
    match = re.search(r'(\d+:\d+(?:AM|PM))-(\d+:\d+(?:AM|PM))', market_title, re.IGNORECASE)
    if match:
        fmt = '%I:%M%p'
        try:
            t1 = datetime.strptime(match.group(1).upper(), fmt)
            t2 = datetime.strptime(match.group(2).upper(), fmt)
            diff = int((t2 - t1).total_seconds())
            if diff < 0:
                diff += 86400
            candle_size = f"{diff//60}m"
        except:
            candle_size = 'unknown'
    else:
        candle_size = 'unknown'

    return asset, candle_size


def parse_candle_open(market_title, trade_ts):
    """Estimate candle open timestamp from market title."""
    import re
    match = re.search(r'(\d+:\d+(?:AM|PM))', market_title, re.IGNORECASE)
    if not match:
        return None

    trade_dt = datetime.fromtimestamp(trade_ts, tz=timezone.utc)
    fmt = '%I:%M%p'
    try:
        t = datetime.strptime(match.group(1).upper(), fmt)
        # Build candle open as same date as trade (ET = UTC-4 or UTC-5, approximate)
        candle_open = trade_dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        # Adjust for ET offset (approximate: use UTC-4)
        candle_open_ts = int(candle_open.timestamp()) + 4 * 3600
        return candle_open_ts
    except:
        return None


def load_csv(wallet_name):
    path = os.path.join(WALLETS_DIR, f'{wallet_name}.csv')
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return []
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def analyze(wallet_name):
    rows = load_csv(wallet_name)
    if not rows:
        return

    buys = [r for r in rows if r['side'].upper() == 'BUY']
    print(f"\n{'='*60}")
    print(f"  {wallet_name.upper()} — {len(rows)} total trades, {len(buys)} buys")
    print(f"{'='*60}")

    # ── 1. TIMING ANALYSIS ────────────────────────────────────────
    print("\n--- 1. TIMING: Seconds after candle open ---")

    delays = []
    for r in buys:
        try:
            ts = int(float(r['timestamp']))
            candle_open = parse_candle_open(r['market'], ts)
            if candle_open is None:
                continue
            delay = ts - candle_open
            if -60 <= delay <= 900:  # sanity check
                delays.append(delay)
        except:
            continue

    if delays:
        delays.sort()
        n = len(delays)
        print(f"  Trades with timing data : {n}")
        print(f"  Median delay            : {sorted(delays)[n//2]}s")
        print(f"  Mean delay              : {sum(delays)/n:.1f}s")
        print(f"  Min delay               : {min(delays)}s")
        print(f"  Max delay               : {max(delays)}s")

        buckets = {'0-10s': 0, '11-30s': 0, '31-60s': 0, '61-120s': 0, '120s+': 0}
        for d in delays:
            if d <= 10:     buckets['0-10s'] += 1
            elif d <= 30:   buckets['11-30s'] += 1
            elif d <= 60:   buckets['31-60s'] += 1
            elif d <= 120:  buckets['61-120s'] += 1
            else:           buckets['120s+'] += 1

        print(f"\n  Distribution:")
        for bucket, count in buckets.items():
            pct = count / n * 100
            bar = '#' * int(pct / 2)
            print(f"    {bucket:10s} {count:5d} ({pct:5.1f}%) {bar}")
    else:
        print("  Could not compute timing (market title format may differ)")

    # ── 2. ENTRY PRICE ANALYSIS ───────────────────────────────────
    print("\n--- 2. ENTRY PRICE: What price do they buy at? ---")

    prices = []
    for r in buys:
        try:
            prices.append(float(r['price']))
        except:
            continue

    if prices:
        n = len(prices)
        prices_sorted = sorted(prices)
        print(f"  Trades analyzed         : {n}")
        print(f"  Median entry price      : {sorted(prices)[n//2]:.3f}")
        print(f"  Mean entry price        : {sum(prices)/n:.3f}")
        print(f"  Min price               : {min(prices):.3f}")
        print(f"  Max price               : {max(prices):.3f}")

        buckets = {
            '0.01-0.10': 0,
            '0.10-0.20': 0,
            '0.20-0.30': 0,
            '0.30-0.40': 0,
            '0.40-0.50': 0,
            '0.50+':     0,
        }
        for p in prices:
            if p < 0.10:    buckets['0.01-0.10'] += 1
            elif p < 0.20:  buckets['0.10-0.20'] += 1
            elif p < 0.30:  buckets['0.20-0.30'] += 1
            elif p < 0.40:  buckets['0.30-0.40'] += 1
            elif p < 0.50:  buckets['0.40-0.50'] += 1
            else:           buckets['0.50+'] += 1

        print(f"\n  Price distribution:")
        for bucket, count in buckets.items():
            pct = count / n * 100
            bar = '#' * int(pct / 2)
            print(f"    {bucket:12s} {count:5d} ({pct:5.1f}%) {bar}")

    # ── 2b. ONE SIDE OR BOTH? ─────────────────────────────────────
    print("\n--- 2b. Do they buy one side or both (Up+Down)? ---")

    # Group buys by market (candle)
    by_market = defaultdict(list)
    for r in buys:
        by_market[r['market']].append(r)

    one_side = 0
    both_sides = 0
    for market, trades in by_market.items():
        outcomes = set(t['outcome'].lower() for t in trades)
        if 'up' in outcomes and 'down' in outcomes:
            both_sides += 1
        else:
            one_side += 1

    total = one_side + both_sides
    print(f"  Candles traded          : {total}")
    print(f"  Both sides (Up+Down)    : {both_sides} ({both_sides/total*100:.1f}%)" if total else "")
    print(f"  One side only           : {one_side} ({one_side/total*100:.1f}%)" if total else "")

    # ── 2c. LAYERED OR SINGLE? ────────────────────────────────────
    print("\n--- 2c. Single entry or layered (multiple buys per candle)? ---")

    layer_counts = defaultdict(int)
    for market, trades in by_market.items():
        layer_counts[len(trades)] += 1

    print(f"  Buys per candle:")
    for n_buys in sorted(layer_counts.keys()):
        count = layer_counts[n_buys]
        pct = count / total * 100 if total else 0
        bar = '#' * int(pct / 2)
        print(f"    {n_buys} buy(s)      {count:5d} ({pct:5.1f}%) {bar}")

    # ── 2d. MARKET PREFERENCE ─────────────────────────────────────
    print("\n--- 2d. Market preference (BTC vs ETH, 5m vs 15m) ---")

    market_counts = defaultdict(int)
    for r in buys:
        asset, candle = parse_candle_info(r['market'])
        market_counts[f"{asset}_{candle}"] += 1

    total_buys = sum(market_counts.values())
    for mkt, count in sorted(market_counts.items(), key=lambda x: -x[1]):
        pct = count / total_buys * 100 if total_buys else 0
        print(f"    {mkt:15s} {count:5d} ({pct:5.1f}%)")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'all':
        for i in range(1, 11):
            analyze(f'wallet_{i}')
    else:
        analyze('wallet_1')
