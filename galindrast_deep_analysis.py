"""
galindrast_deep_analysis.py — Full behavioral analysis of Galindrast wallet
============================================================================
24,604 trades over 8.7 hours. Analyze EVERYTHING:
- Entry timing relative to candle start
- Size scaling patterns
- Direction selection (what triggers their directional bet?)
- Price level distribution over time within candles
- Win/loss patterns
- Sell behavior (when/why do they sell?)
- Comparison between winning and losing candles
"""
import sqlite3
import numpy as np
from collections import defaultdict, Counter
from datetime import datetime, timezone

DB = 'databases/galindrast_trades.db'
conn = sqlite3.connect(DB)
rows = conn.execute("""
    SELECT tx_hash, timestamp, side, outcome, price, size, usdc, market, slug
    FROM trades
    WHERE side != '' AND slug != ''
    ORDER BY timestamp
""").fetchall()
conn.close()

print(f"{'=' * 80}")
print(f"  GALINDRAST DEEP BEHAVIORAL ANALYSIS")
print(f"  {len(rows):,} trades over 8.7 hours")
print(f"{'=' * 80}")

# ── Group by candle (slug) ────────────────────────────────────────────────────
by_slug = defaultdict(list)
for tx, ts, side, outcome, price, size, usdc, market, slug in rows:
    by_slug[slug].append({
        'ts': ts, 'side': side, 'outcome': outcome,
        'price': price, 'size': size, 'usdc': usdc,
        'market': market, 'slug': slug,
    })

# Only analyze 5m candles (main activity)
candles_5m = {s: t for s, t in by_slug.items() if '-5m-' in s}
print(f"\n  5m candles: {len(candles_5m)}")

# ── 1. ENTRY TIMING ──────────────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  1. ENTRY TIMING (relative to candle start)")
print(f"{'=' * 80}")

all_offsets = []
first_entry_offsets = []
for slug, trades in candles_5m.items():
    try:
        candle_start = int(slug.split('-5m-')[1])
    except:
        continue

    buys = [t for t in trades if t['side'] == 'BUY']
    if not buys:
        continue

    first_ts = min(t['ts'] for t in buys)
    first_entry_offsets.append(first_ts - candle_start)

    for t in buys:
        all_offsets.append(t['ts'] - candle_start)

print(f"  First entry after candle start:")
print(f"    Avg: {np.mean(first_entry_offsets):.1f}s | Med: {np.median(first_entry_offsets):.1f}s")
print(f"    P10: {np.percentile(first_entry_offsets, 10):.0f}s | P25: {np.percentile(first_entry_offsets, 25):.0f}s")
print(f"    P75: {np.percentile(first_entry_offsets, 75):.0f}s | P90: {np.percentile(first_entry_offsets, 90):.0f}s")

print(f"\n  All entries timing distribution (5s buckets):")
for lo in range(0, 310, 15):
    c = sum(1 for o in all_offsets if lo <= o < lo + 15)
    bar = '#' * int(c / max(1, len(all_offsets)) * 80)
    print(f"    {lo:>3}-{lo+15:>3}s: {c:>5} ({c/len(all_offsets)*100:>4.1f}%) {bar}")

# ── 2. DIRECTION SELECTION ────────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  2. DIRECTION SELECTION PER CANDLE")
print(f"{'=' * 80}")

candle_dirs = []
for slug, trades in candles_5m.items():
    buys = [t for t in trades if t['side'] == 'BUY']
    if not buys:
        continue

    up_sh = sum(t['size'] for t in buys if t['outcome'] == 'Up')
    dn_sh = sum(t['size'] for t in buys if t['outcome'] == 'Down')
    up_usdc = sum(t['usdc'] for t in buys if t['outcome'] == 'Up')
    dn_usdc = sum(t['usdc'] for t in buys if t['outcome'] == 'Down')

    if up_sh + dn_sh == 0:
        continue

    total_sh = up_sh + dn_sh
    total_usdc = up_usdc + dn_usdc

    # Net direction
    if up_sh > dn_sh * 2:
        net = 'UP_heavy'
    elif dn_sh > up_sh * 2:
        net = 'DN_heavy'
    elif up_sh > 0 and dn_sh > 0:
        net = 'both'
    elif up_sh > 0:
        net = 'UP_only'
    else:
        net = 'DN_only'

    # Did they buy both sides?
    bought_both = up_sh > 0 and dn_sh > 0

    # Which side had more?
    primary = 'Up' if up_sh >= dn_sh else 'Down'
    ratio = max(up_sh, dn_sh) / min(up_sh, dn_sh) if min(up_sh, dn_sh) > 0 else 999

    candle_dirs.append({
        'slug': slug, 'net': net, 'primary': primary, 'ratio': ratio,
        'up_sh': up_sh, 'dn_sh': dn_sh, 'up_usdc': up_usdc, 'dn_usdc': dn_usdc,
        'total_sh': total_sh, 'total_usdc': total_usdc,
        'bought_both': bought_both, 'n_trades': len(buys),
    })

net_counts = Counter(c['net'] for c in candle_dirs)
print(f"  Direction breakdown:")
for k, v in net_counts.most_common():
    print(f"    {k:<12}: {v:>4} ({v/len(candle_dirs)*100:.1f}%)")

both_ct = sum(1 for c in candle_dirs if c['bought_both'])
print(f"\n  Bought BOTH sides: {both_ct}/{len(candle_dirs)} ({both_ct/len(candle_dirs)*100:.0f}%)")

ratios = [c['ratio'] for c in candle_dirs if c['bought_both']]
if ratios:
    print(f"  Share ratio (primary/secondary) when both:")
    print(f"    Avg: {np.mean(ratios):.1f}x | Med: {np.median(ratios):.1f}x")

# ── 3. SIZE SCALING WITHIN CANDLES ────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  3. SIZE SCALING WITHIN CANDLES")
print(f"{'=' * 80}")

# For each candle, track how size changes over time
early_sizes = []   # first 60s
mid_sizes = []     # 60-180s
late_sizes = []    # 180-300s

early_prices = []
mid_prices = []
late_prices = []

for slug, trades in candles_5m.items():
    try:
        candle_start = int(slug.split('-5m-')[1])
    except:
        continue

    for t in trades:
        if t['side'] != 'BUY':
            continue
        offset = t['ts'] - candle_start
        if 0 <= offset < 60:
            early_sizes.append(t['size'])
            early_prices.append(t['price'])
        elif 60 <= offset < 180:
            mid_sizes.append(t['size'])
            mid_prices.append(t['price'])
        elif 180 <= offset <= 300:
            late_sizes.append(t['size'])
            late_prices.append(t['price'])

print(f"  {'Phase':<15} {'Trades':>7} {'Avg Size':>9} {'Med Size':>9} {'Avg Price':>10}")
print(f"  {'-' * 55}")
for lbl, sz, pr in [('Early 0-60s', early_sizes, early_prices),
                      ('Mid 60-180s', mid_sizes, mid_prices),
                      ('Late 180-300s', late_sizes, late_prices)]:
    if sz:
        print(f"  {lbl:<15} {len(sz):>7} {np.mean(sz):>9.1f} {np.median(sz):>9.1f} {np.mean(pr):>10.3f}")

# Size by price level
print(f"\n  SIZE BY PRICE LEVEL (how much do they buy at each price?):")
print(f"  {'Price':>8} {'Trades':>7} {'Avg Size':>9} {'Total Sh':>10} {'Total USDC':>12}")
print(f"  {'-' * 50}")
for lo in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
    hi = lo + 0.1
    bucket = [(r[4], r[5], r[6]) for r in rows if r[2] == 'BUY' and r[4] and lo <= r[4] < hi]
    if bucket:
        sizes_b = [b[1] for b in bucket]
        usdc_b = [b[2] for b in bucket if b[2]]
        print(f"  {lo:.1f}-{hi:.1f}  {len(bucket):>7} {np.mean(sizes_b):>9.1f} {sum(sizes_b):>10,.0f} ${sum(usdc_b):>11,.0f}")

# ── 4. SELL BEHAVIOR ──────────────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  4. SELL BEHAVIOR (when and why do they sell?)")
print(f"{'=' * 80}")

sells = [r for r in rows if r[2] == 'SELL']
print(f"  Total sells: {len(sells)} ({len(sells)/len(rows)*100:.1f}% of all trades)")

if sells:
    sell_prices = [r[4] for r in sells if r[4]]
    sell_sizes = [r[5] for r in sells if r[5]]
    print(f"  Sell price: avg={np.mean(sell_prices):.3f} med={np.median(sell_prices):.3f}")
    print(f"  Sell size:  avg={np.mean(sell_sizes):.1f} med={np.median(sell_sizes):.1f}")

    # When in the candle do they sell?
    sell_offsets = []
    for r in sells:
        slug = r[8]
        if '-5m-' in slug:
            try:
                cs = int(slug.split('-5m-')[1])
                sell_offsets.append(r[1] - cs)
            except:
                pass
    if sell_offsets:
        print(f"  Sell timing: avg={np.mean(sell_offsets):.0f}s med={np.median(sell_offsets):.0f}s into candle")

    # Sell price distribution
    print(f"\n  Sell price distribution:")
    for lo in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        c = sum(1 for r in sells if r[4] and lo <= r[4] < lo + 0.1)
        if c:
            print(f"    {lo:.1f}-{lo+0.1:.1f}: {c:>4}")

# ── 5. TRADE-BY-TRADE SEQUENCING ──────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  5. TRADE-BY-TRADE SEQUENCING (how do entries flow within a candle?)")
print(f"{'=' * 80}")

# Pick a sample of candles and show the exact sequence
sample_candles = sorted(candles_5m.items(), key=lambda x: -len(x[1]))[:5]
for slug, trades in sample_candles:
    try:
        candle_start = int(slug.split('-5m-')[1])
    except:
        continue

    buys = sorted([t for t in trades if t['side'] == 'BUY'], key=lambda x: x['ts'])
    sells_c = sorted([t for t in trades if t['side'] == 'SELL'], key=lambda x: x['ts'])

    up_sh = sum(t['size'] for t in buys if t['outcome'] == 'Up')
    dn_sh = sum(t['size'] for t in buys if t['outcome'] == 'Down')

    print(f"\n  Candle: {slug} ({len(buys)} buys, {len(sells_c)} sells)")
    print(f"  Up shares: {up_sh:.0f} | Down shares: {dn_sh:.0f}")
    print(f"  {'Time':>6} {'Side':>5} {'Out':>5} {'Price':>6} {'Size':>7} {'USDC':>8}")
    print(f"  {'-' * 42}")

    all_sorted = sorted(trades, key=lambda x: x['ts'])
    for t in all_sorted[:30]:  # first 30 trades
        offset = t['ts'] - candle_start
        print(f"  {offset:>5}s {t['side']:>5} {t['outcome']:>5} {t['price']:>6.3f} {t['size']:>7.1f} ${t['usdc']:>7.2f}")
    if len(all_sorted) > 30:
        print(f"  ... ({len(all_sorted) - 30} more trades)")

# ── 6. INTER-TRADE TIMING ────────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  6. INTER-TRADE TIMING (gap between consecutive trades)")
print(f"{'=' * 80}")

gaps = []
for slug, trades in candles_5m.items():
    buys = sorted([t for t in trades if t['side'] == 'BUY'], key=lambda x: x['ts'])
    for i in range(1, len(buys)):
        gap = buys[i]['ts'] - buys[i-1]['ts']
        if 0 <= gap <= 300:
            gaps.append(gap)

if gaps:
    print(f"  Avg gap: {np.mean(gaps):.2f}s | Med: {np.median(gaps):.2f}s")
    print(f"  P10: {np.percentile(gaps, 10):.0f}s | P25: {np.percentile(gaps, 25):.0f}s")
    print(f"  P75: {np.percentile(gaps, 75):.0f}s | P90: {np.percentile(gaps, 90):.0f}s")

    # How many trades happen in the SAME second?
    same_sec = sum(1 for g in gaps if g == 0)
    within_1s = sum(1 for g in gaps if g <= 1)
    print(f"\n  Same second: {same_sec} ({same_sec/len(gaps)*100:.1f}%)")
    print(f"  Within 1s:   {within_1s} ({within_1s/len(gaps)*100:.1f}%)")
    print(f"  Within 5s:   {sum(1 for g in gaps if g <= 5)} ({sum(1 for g in gaps if g <= 5)/len(gaps)*100:.1f}%)")

# ── 7. DIRECTION SWITCHES ────────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  7. DIRECTION SWITCHES (do they flip direction mid-candle?)")
print(f"{'=' * 80}")

switch_count = 0
no_switch = 0
for slug, trades in candles_5m.items():
    buys = sorted([t for t in trades if t['side'] == 'BUY'], key=lambda x: x['ts'])
    if len(buys) < 2:
        continue

    prev_out = buys[0]['outcome']
    switched = False
    for t in buys[1:]:
        if t['outcome'] != prev_out:
            switched = True
            break
        prev_out = t['outcome']

    if switched:
        switch_count += 1
    else:
        no_switch += 1

print(f"  Candles with direction switch: {switch_count} ({switch_count/(switch_count+no_switch)*100:.1f}%)")
print(f"  Candles with single direction: {no_switch} ({no_switch/(switch_count+no_switch)*100:.1f}%)")

# ── 8. ENTRY PRICE PROGRESSION ───────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  8. ENTRY PRICE PROGRESSION (does price go up or down as they DCA?)")
print(f"{'=' * 80}")

price_trends = []
for slug, trades in candles_5m.items():
    buys = sorted([t for t in trades if t['side'] == 'BUY'], key=lambda x: x['ts'])
    if len(buys) < 5:
        continue

    prices = [t['price'] for t in buys]
    first_5_avg = np.mean(prices[:5])
    last_5_avg = np.mean(prices[-5:])
    trend = last_5_avg - first_5_avg
    price_trends.append(trend)

if price_trends:
    rising = sum(1 for t in price_trends if t > 0.05)
    falling = sum(1 for t in price_trends if t < -0.05)
    flat = len(price_trends) - rising - falling
    print(f"  Price rising (last > first by 5c+):  {rising} ({rising/len(price_trends)*100:.0f}%)")
    print(f"  Price falling (last < first by 5c+): {falling} ({falling/len(price_trends)*100:.0f}%)")
    print(f"  Flat (within 5c):                    {flat} ({flat/len(price_trends)*100:.0f}%)")
    print(f"  Avg price change (last5 - first5):   {np.mean(price_trends):+.3f}")

# ── 9. FIRST TRADE ANALYSIS ──────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  9. FIRST TRADE IN EACH CANDLE (what triggers them?)")
print(f"{'=' * 80}")

first_trades = []
for slug, trades in candles_5m.items():
    try:
        candle_start = int(slug.split('-5m-')[1])
    except:
        continue

    buys = sorted([t for t in trades if t['side'] == 'BUY'], key=lambda x: x['ts'])
    if not buys:
        continue

    first = buys[0]
    offset = first['ts'] - candle_start
    first_trades.append({
        'slug': slug, 'offset': offset, 'outcome': first['outcome'],
        'price': first['price'], 'size': first['size'],
    })

print(f"  First trade price distribution:")
for lo in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
    c = sum(1 for f in first_trades if lo <= f['price'] < lo + 0.1)
    bar = '#' * int(c / max(1, len(first_trades)) * 50)
    print(f"    {lo:.1f}-{lo+0.1:.1f}: {c:>4} {bar}")

print(f"\n  First trade timing vs price:")
early_first = [f for f in first_trades if f['offset'] < 30]
mid_first = [f for f in first_trades if 30 <= f['offset'] < 120]
late_first = [f for f in first_trades if f['offset'] >= 120]
for lbl, group in [('< 30s', early_first), ('30-120s', mid_first), ('>= 120s', late_first)]:
    if group:
        print(f"    {lbl}: n={len(group)} avg_price={np.mean([f['price'] for f in group]):.3f} avg_size={np.mean([f['size'] for f in group]):.1f}")

# ── 10. BURST DETECTION ───────────────────────────────────────────────────────
print(f"\n{'=' * 80}")
print(f"  10. BURST DETECTION (clusters of rapid trades)")
print(f"{'=' * 80}")

burst_sizes = []
for slug, trades in candles_5m.items():
    buys = sorted([t for t in trades if t['side'] == 'BUY'], key=lambda x: x['ts'])
    if len(buys) < 3:
        continue

    # Find bursts: 3+ trades within 3 seconds
    i = 0
    while i < len(buys):
        burst = [buys[i]]
        j = i + 1
        while j < len(buys) and buys[j]['ts'] - buys[i]['ts'] <= 3:
            burst.append(buys[j])
            j += 1
        if len(burst) >= 3:
            burst_sizes.append({
                'n': len(burst),
                'total_sh': sum(t['size'] for t in burst),
                'total_usdc': sum(t['usdc'] for t in burst),
                'avg_price': np.mean([t['price'] for t in burst]),
                'outcomes': Counter(t['outcome'] for t in burst),
            })
        i = j if j > i + 1 else i + 1

if burst_sizes:
    print(f"  Total bursts (3+ trades in 3s): {len(burst_sizes)}")
    print(f"  Avg trades per burst: {np.mean([b['n'] for b in burst_sizes]):.1f}")
    print(f"  Avg shares per burst: {np.mean([b['total_sh'] for b in burst_sizes]):.0f}")
    print(f"  Avg USDC per burst: ${np.mean([b['total_usdc'] for b in burst_sizes]):,.0f}")
    print(f"  Avg price during burst: {np.mean([b['avg_price'] for b in burst_sizes]):.3f}")

    # Are bursts single-direction or mixed?
    single_dir = sum(1 for b in burst_sizes if len(b['outcomes']) == 1)
    print(f"  Single direction bursts: {single_dir}/{len(burst_sizes)} ({single_dir/len(burst_sizes)*100:.0f}%)")

print(f"\n{'=' * 80}")
