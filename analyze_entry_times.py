"""
Entry timing deep-dive for wallet_1.

Looks at:
- Time of FIRST buy relative to candle open
- Interval between consecutive buys (bot cadence?)
- Are buys uniform throughout the candle or clustered?
- What hours of day do they trade?
- Do they trade every candle or skip some?
"""

import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

WALLETS_DIR = os.path.join(os.path.dirname(__file__), 'Wallets_new')


def load_csv(wallet_name):
    path = os.path.join(WALLETS_DIR, f'{wallet_name}.csv')
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def parse_candle_open_ts(market_title, trade_ts):
    """Parse candle open time from market title, return UTC timestamp."""
    match = re.search(r'(\d+):(\d+)(AM|PM)', market_title, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()
    if ampm == 'PM' and hour != 12:
        hour += 12
    if ampm == 'AM' and hour == 12:
        hour = 0

    trade_dt = datetime.fromtimestamp(int(float(trade_ts)), tz=timezone.utc)
    # ET is UTC-4 (EDT) — candle time is ET, convert to UTC
    candle_open_utc = trade_dt.replace(hour=(hour + 4) % 24, minute=minute, second=0, microsecond=0)
    return int(candle_open_utc.timestamp())


def analyze_entry_times(wallet_name):
    rows = load_csv(wallet_name)
    buys = [r for r in rows if r['side'].upper() == 'BUY']

    # Group by candle
    candles = defaultdict(list)
    for r in buys:
        candles[r['market']].append(r)
    for m in candles:
        candles[m].sort(key=lambda r: int(float(r['timestamp'])))

    print(f"\n{'='*60}")
    print(f"  {wallet_name.upper()} — Entry Timing Deep Dive")
    print(f"  Candles: {len(candles)}")
    print(f"{'='*60}")

    # ── 1. First buy delay from candle open ───────────────────────
    print("\n--- 1. How quickly does first buy happen after candle open? ---")
    first_delays = []
    for market, trades in candles.items():
        ts = int(float(trades[0]['timestamp']))
        candle_open = parse_candle_open_ts(market, ts)
        if candle_open is None:
            continue
        delay = ts - candle_open
        if -30 <= delay <= 600:
            first_delays.append(delay)

    if first_delays:
        first_delays.sort()
        n = len(first_delays)
        print(f"  Candles analyzed : {n}")
        print(f"  Median first buy : {first_delays[n//2]}s after open")
        print(f"  Mean first buy   : {sum(first_delays)/n:.1f}s after open")
        print(f"  Min              : {min(first_delays)}s")
        print(f"  Max              : {max(first_delays)}s")

        buckets = {'0-5s': 0, '6-15s': 0, '16-30s': 0, '31-60s': 0, '60s+': 0}
        for d in first_delays:
            if d <= 5:      buckets['0-5s'] += 1
            elif d <= 15:   buckets['6-15s'] += 1
            elif d <= 30:   buckets['16-30s'] += 1
            elif d <= 60:   buckets['31-60s'] += 1
            else:           buckets['60s+'] += 1
        print(f"\n  Distribution of first buy delay:")
        for b, c in buckets.items():
            pct = c/n*100
            bar = '#' * int(pct/2)
            print(f"    {b:10s} {c:4d} ({pct:5.1f}%) {bar}")

    # ── 2. Interval between consecutive buys (bot cadence) ────────
    print("\n--- 2. Interval between consecutive buys (same candle) ---")
    intervals = []
    for market, trades in candles.items():
        if len(trades) < 2:
            continue
        for i in range(1, len(trades)):
            dt = int(float(trades[i]['timestamp'])) - int(float(trades[i-1]['timestamp']))
            if 0 <= dt <= 300:
                intervals.append(dt)

    if intervals:
        intervals.sort()
        n = len(intervals)
        print(f"  Intervals analyzed : {n}")
        print(f"  Median interval    : {intervals[n//2]}s")
        print(f"  Mean interval      : {sum(intervals)/n:.2f}s")
        print(f"  Min interval       : {min(intervals)}s")

        # Count most common intervals
        from collections import Counter
        common = Counter(intervals).most_common(15)
        print(f"\n  Most common intervals (seconds):")
        for interval, count in common:
            pct = count/n*100
            bar = '#' * int(pct/1.5)
            print(f"    {interval:4d}s  {count:5d} ({pct:5.1f}%) {bar}")

    # ── 3. Buy distribution within candle (uniform or clustered?) ─
    print("\n--- 3. Where in the candle do buys cluster? (0-300s window) ---")
    all_delays = []
    for market, trades in candles.items():
        for t in trades:
            ts = int(float(t['timestamp']))
            candle_open = parse_candle_open_ts(market, ts)
            if candle_open is None:
                continue
            delay = ts - candle_open
            if 0 <= delay <= 300:
                all_delays.append(delay)

    if all_delays:
        n = len(all_delays)
        buckets = {
            '0-30s':   0,
            '31-60s':  0,
            '61-120s': 0,
            '121-180s':0,
            '181-240s':0,
            '241-300s':0,
        }
        for d in all_delays:
            if d <= 30:       buckets['0-30s'] += 1
            elif d <= 60:     buckets['31-60s'] += 1
            elif d <= 120:    buckets['61-120s'] += 1
            elif d <= 180:    buckets['121-180s'] += 1
            elif d <= 240:    buckets['181-240s'] += 1
            else:             buckets['241-300s'] += 1
        print(f"  Total buy timestamps : {n}")
        for b, c in buckets.items():
            pct = c/n*100
            bar = '#' * int(pct/1.5)
            print(f"    {b:12s} {c:5d} ({pct:5.1f}%) {bar}")

    # ── 4. Hour of day preference (ET) ────────────────────────────
    print("\n--- 4. What hours do they trade? (ET) ---")
    hour_counts = defaultdict(int)
    for market, trades in candles.items():
        ts = int(float(trades[0]['timestamp']))
        # Convert UTC to ET (UTC-4)
        dt_et = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour_et = (dt_et.hour - 4) % 24
        hour_counts[hour_et] += 1

    print(f"  (candles per hour)")
    for hour in range(24):
        count = hour_counts.get(hour, 0)
        bar = '#' * count
        label = f"{hour:02d}:00 ET"
        print(f"    {label}  {count:3d} {bar}")

    # ── 5. Do they skip candles? ───────────────────────────────────
    print("\n--- 5. Do they trade every candle or skip some? ---")
    # Look at gaps between candle open times
    candle_opens = []
    for market, trades in candles.items():
        ts = int(float(trades[0]['timestamp']))
        open_ts = parse_candle_open_ts(market, ts)
        if open_ts:
            candle_opens.append(open_ts)
    candle_opens.sort()

    if len(candle_opens) > 1:
        gaps = []
        for i in range(1, len(candle_opens)):
            gap = candle_opens[i] - candle_opens[i-1]
            gaps.append(gap)

        skipped = sum(1 for g in gaps if g > 360)  # >6min = skipped a 5m candle
        print(f"  Total candle gaps   : {len(gaps)}")
        print(f"  Consecutive candles : {len(gaps) - skipped} ({(len(gaps)-skipped)/len(gaps)*100:.1f}%)")
        print(f"  Skipped candles     : {skipped} ({skipped/len(gaps)*100:.1f}%)")
        common_gaps = sorted(set(gaps), key=gaps.count, reverse=True)[:5]
        print(f"  Most common gaps (s): {common_gaps}")


if __name__ == '__main__':
    analyze_entry_times('wallet_1')
