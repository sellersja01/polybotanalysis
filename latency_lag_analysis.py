"""
latency_lag_analysis.py — Measure Polymarket's lag behind Binance BTC price
============================================================================
For each 5-minute candle:
  1. Detect when BTC makes a significant move on Binance
  2. Measure how long until Polymarket odds shift to reflect it
  3. Calculate if buying the "correct" side during the lag would be profitable

Uses local market_btc_5m.db which has both:
  - asset_price: Binance BTC ticks (~2.66M rows)
  - polymarket_odds: Up/Down bid/ask/mid per tick
"""

import sqlite3
import numpy as np
from collections import defaultdict
from datetime import datetime, timezone

DB = 'databases/market_btc_5m.db'
conn = sqlite3.connect(DB)

# ── Load data range ──────────────────────────────────────────────────────────
r = conn.execute("SELECT MIN(unix_time), MAX(unix_time) FROM asset_price").fetchone()
hours = (r[1] - r[0]) / 3600
print(f"{'='*70}")
print(f"  LATENCY LAG ANALYSIS — Binance vs Polymarket")
print(f"{'='*70}")
print(f"  BTC price ticks : {conn.execute('SELECT COUNT(*) FROM asset_price').fetchone()[0]:,}")
print(f"  Poly odds ticks : {conn.execute('SELECT COUNT(*) FROM polymarket_odds').fetchone()[0]:,}")
print(f"  Duration        : {hours:.1f} hours")
print(f"  Period           : {datetime.fromtimestamp(r[0], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} -> {datetime.fromtimestamp(r[1], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

# ── Get distinct candles (questions) ──────────────────────────────────────────
candles = conn.execute("""
    SELECT DISTINCT question FROM polymarket_odds
    WHERE outcome = 'Up'
    ORDER BY question
""").fetchall()
candle_list = [c[0] for c in candles]
print(f"  Candles          : {len(candle_list)}")

# ── For each candle, load BTC price + Poly odds, detect lag ───────────────────
print(f"\n  Processing candles...", flush=True)

lag_results = []
trade_results = []

for i, question in enumerate(candle_list):
    if i % 100 == 0:
        print(f"    [{i}/{len(candle_list)}]...", flush=True)

    # Get candle time range from poly odds
    tr = conn.execute("""
        SELECT MIN(unix_time), MAX(unix_time) FROM polymarket_odds
        WHERE question = ?
    """, (question,)).fetchone()
    if not tr or not tr[0]:
        continue
    t_start, t_end = tr

    # Load BTC prices during this candle
    btc_rows = conn.execute("""
        SELECT unix_time, price FROM asset_price
        WHERE unix_time BETWEEN ? AND ?
        ORDER BY unix_time
    """, (t_start, t_end)).fetchall()

    if len(btc_rows) < 10:
        continue

    # Load Poly Up mid during this candle
    poly_rows = conn.execute("""
        SELECT unix_time, mid FROM polymarket_odds
        WHERE question = ? AND outcome = 'Up'
        ORDER BY unix_time
    """, (question,)).fetchall()

    if len(poly_rows) < 5:
        continue

    btc_times  = [r[0] for r in btc_rows]
    btc_prices = [r[1] for r in btc_rows]
    poly_times = [r[0] for r in poly_rows]
    poly_mids  = [r[1] for r in poly_rows]

    # Reference BTC price at candle start
    btc_start = btc_prices[0]

    # Find significant BTC moves (>0.1% in a rolling window)
    for j in range(len(btc_times)):
        # Compare current price to price 10-30 seconds ago
        lookback_t = btc_times[j] - 15  # 15 second lookback
        # Find price ~15s ago
        prev_idx = None
        for k in range(j-1, -1, -1):
            if btc_times[k] <= lookback_t:
                prev_idx = k
                break
        if prev_idx is None:
            continue

        move_pct = (btc_prices[j] - btc_prices[prev_idx]) / btc_prices[prev_idx] * 100

        if abs(move_pct) < 0.05:  # need at least 0.05% move
            continue

        btc_move_time = btc_times[j]
        direction = "up" if move_pct > 0 else "down"

        # What should Poly do? If BTC went up, Up mid should increase
        # Find Poly mid at the time of the BTC move
        poly_mid_at_move = None
        for k in range(len(poly_times) - 1, -1, -1):
            if poly_times[k] <= btc_move_time:
                poly_mid_at_move = poly_mids[k]
                break
        if poly_mid_at_move is None:
            continue

        # Find when Poly mid moves by at least 2 cents in the right direction
        expected_dir = 1 if direction == "up" else -1
        response_time = None
        poly_mid_after = None

        for k in range(len(poly_times)):
            if poly_times[k] <= btc_move_time:
                continue
            delta = poly_mids[k] - poly_mid_at_move
            if delta * expected_dir >= 0.02:  # 2 cent move in right direction
                response_time = poly_times[k] - btc_move_time
                poly_mid_after = poly_mids[k]
                break

        if response_time is not None and response_time < 120:  # within 2 min
            lag_results.append({
                'question': question,
                'btc_move_time': btc_move_time,
                'move_pct': move_pct,
                'direction': direction,
                'poly_mid_before': poly_mid_at_move,
                'poly_mid_after': poly_mid_after,
                'lag_seconds': response_time,
            })

            # Simulate trade: buy the correct side at stale odds
            # If BTC went up, buy Up at current ask (mid + spread/2)
            # Cost = poly_mid_at_move + 0.005 (approx half spread)
            entry_price = poly_mid_at_move + 0.005 if direction == "up" else (1 - poly_mid_at_move) + 0.005
            # If we're right, this resolves favorably
            # Conservative: sell after odds move (take the repricing profit)
            exit_price = poly_mid_after if direction == "up" else (1 - poly_mid_after)
            profit = exit_price - entry_price

            trade_results.append({
                'move_pct': abs(move_pct),
                'lag': response_time,
                'direction': direction,
                'entry': entry_price,
                'exit': exit_price,
                'profit': profit,
            })

conn.close()

# ── Results ───────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  LAG MEASUREMENT RESULTS")
print(f"{'='*70}")
print(f"  Significant BTC moves detected    : {len(lag_results):,}")

if not lag_results:
    print("  No lag events detected!")
    exit()

lags = [r['lag_seconds'] for r in lag_results]
lags_sorted = sorted(lags)

print(f"  Avg lag (BTC move -> Poly reprices): {np.mean(lags):.2f}s")
print(f"  Median lag                         : {np.median(lags):.2f}s")
print(f"  P10 lag                            : {lags_sorted[int(len(lags)*0.10)]:.2f}s")
print(f"  P25 lag                            : {lags_sorted[int(len(lags)*0.25)]:.2f}s")
print(f"  P75 lag                            : {lags_sorted[int(len(lags)*0.75)]:.2f}s")
print(f"  P90 lag                            : {lags_sorted[int(len(lags)*0.90)]:.2f}s")
print(f"  Min lag                            : {min(lags):.2f}s")
print(f"  Max lag                            : {max(lags):.2f}s")

# Distribution
print(f"\n  Lag distribution:")
for thresh in [0.5, 1, 2, 3, 5, 10, 15, 30, 60]:
    count = sum(1 for l in lags if l <= thresh)
    print(f"    <= {thresh:>4}s : {count:>6} ({count/len(lags)*100:>5.1f}%)")

# By move size
print(f"\n  Lag by BTC move size:")
print(f"  {'Move %':>8} {'Count':>6} {'Avg Lag':>8} {'Med Lag':>8}")
print(f"  {'-'*36}")
for lo, hi, lbl in [(0.05, 0.10, '0.05-0.10'), (0.10, 0.20, '0.10-0.20'),
                     (0.20, 0.50, '0.20-0.50'), (0.50, 1.0, '0.50-1.0'),
                     (1.0, 99, '>1.0%')]:
    bucket = [r for r in lag_results if lo <= abs(r['move_pct']) < hi]
    if bucket:
        bl = [r['lag_seconds'] for r in bucket]
        print(f"  {lbl:>8} {len(bucket):>6} {np.mean(bl):>7.2f}s {np.median(bl):>7.2f}s")

# ── Trade simulation results ─────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  LATENCY ARB TRADE SIMULATION")
print(f"{'='*70}")

if trade_results:
    profits = [t['profit'] for t in trade_results]
    wins = [t for t in trade_results if t['profit'] > 0]
    losses = [t for t in trade_results if t['profit'] <= 0]

    print(f"  Total trades       : {len(trade_results):,}")
    print(f"  Wins               : {len(wins):,} ({len(wins)/len(trade_results)*100:.1f}%)")
    print(f"  Losses             : {len(losses):,}")
    print(f"  Avg profit/trade   : {np.mean(profits):.4f} (${np.mean(profits)*100:.2f} per $100)")
    print(f"  Total profit       : {sum(profits):.4f}")
    print(f"  At $100/trade      : ${sum(profits)*100:,.2f}")

    # Win rate by move size
    print(f"\n  Win rate by BTC move size:")
    print(f"  {'Move %':>8} {'Trades':>7} {'WR%':>6} {'Avg Profit':>11}")
    print(f"  {'-'*36}")
    for lo, hi, lbl in [(0.05, 0.10, '0.05-0.10'), (0.10, 0.20, '0.10-0.20'),
                         (0.20, 0.50, '0.20-0.50'), (0.50, 1.0, '0.50-1.0'),
                         (1.0, 99, '>1.0%')]:
        bucket = [t for t in trade_results if lo <= t['move_pct'] < hi]
        if bucket:
            w = sum(1 for t in bucket if t['profit'] > 0)
            ap = np.mean([t['profit'] for t in bucket])
            print(f"  {lbl:>8} {len(bucket):>7} {w/len(bucket)*100:>5.1f}% {ap:>+10.4f}")

    # Win rate by lag duration
    print(f"\n  Win rate by lag duration:")
    print(f"  {'Lag':>8} {'Trades':>7} {'WR%':>6} {'Avg Profit':>11}")
    print(f"  {'-'*36}")
    for lo, hi, lbl in [(0, 1, '<1s'), (1, 3, '1-3s'), (3, 5, '3-5s'),
                         (5, 10, '5-10s'), (10, 30, '10-30s'), (30, 120, '30s+')]:
        bucket = [t for t in trade_results if lo <= lag_results[trade_results.index(t)]['lag_seconds'] < hi]
        if bucket:
            w = sum(1 for t in bucket if t['profit'] > 0)
            ap = np.mean([t['profit'] for t in bucket])
            print(f"  {lbl:>8} {len(bucket):>7} {w/len(bucket)*100:>5.1f}% {ap:>+10.4f}")

print(f"\n{'='*70}")
