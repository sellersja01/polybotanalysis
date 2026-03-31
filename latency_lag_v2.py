"""
latency_lag_v2.py — Fast version: load everything into memory, then process
"""
import sqlite3
import numpy as np
from bisect import bisect_left
from datetime import datetime, timezone

DB = 'databases/market_btc_5m.db'
conn = sqlite3.connect(DB)

print("Loading BTC prices...", flush=True)
btc_raw = conn.execute("SELECT unix_time, price FROM asset_price ORDER BY unix_time").fetchall()
btc_t = np.array([r[0] for r in btc_raw])
btc_p = np.array([r[1] for r in btc_raw])
print(f"  {len(btc_t):,} ticks loaded", flush=True)

print("Loading Poly Up mids...", flush=True)
poly_raw = conn.execute("""
    SELECT unix_time, mid FROM polymarket_odds
    WHERE outcome = 'Up'
    ORDER BY unix_time
""").fetchall()
poly_t = np.array([r[0] for r in poly_raw])
poly_m = np.array([r[1] for r in poly_raw])
print(f"  {len(poly_t):,} ticks loaded", flush=True)
conn.close()

hours = (btc_t[-1] - btc_t[0]) / 3600
print(f"  Duration: {hours:.1f}h | {datetime.fromtimestamp(btc_t[0], tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(btc_t[-1], tz=timezone.utc).strftime('%Y-%m-%d')}")

# ── Detect BTC moves and measure Poly lag ─────────────────────────────────────
print("\nDetecting BTC moves and measuring lag...", flush=True)

# Sample BTC at regular intervals (every 1 second) for speed
sample_interval = 1.0
sample_times = np.arange(btc_t[0], btc_t[-1], sample_interval)

# Interpolate BTC price at sample points
btc_sampled = np.interp(sample_times, btc_t, btc_p)

# For each sample, compare to price N seconds ago
LOOKBACKS = [5, 10, 15, 30]  # seconds
MOVE_THRESHOLDS = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]  # percent

# Use 15-second lookback as primary
LOOKBACK = 15
lookback_samples = int(LOOKBACK / sample_interval)

lag_events = []

# Find indices where BTC moved significantly
for i in range(lookback_samples, len(sample_times)):
    move_pct = (btc_sampled[i] - btc_sampled[i - lookback_samples]) / btc_sampled[i - lookback_samples] * 100

    if abs(move_pct) < 0.05:
        continue

    btc_move_time = sample_times[i]
    direction = "up" if move_pct > 0 else "down"

    # Find Poly mid at the time of BTC move
    poly_idx = bisect_left(poly_t, btc_move_time)
    if poly_idx >= len(poly_t) or poly_idx == 0:
        continue
    # Use the tick just before
    poly_idx = min(poly_idx, len(poly_t) - 1)
    if poly_t[poly_idx] > btc_move_time:
        poly_idx -= 1
    if poly_idx < 0:
        continue

    poly_mid_at_move = poly_m[poly_idx]

    # Find when Poly shifts by >= 2 cents in expected direction
    expected_sign = 1 if direction == "up" else -1
    response_time = None
    poly_mid_after = None

    # Search forward up to 120 seconds
    search_end = btc_move_time + 120
    for k in range(poly_idx + 1, len(poly_t)):
        if poly_t[k] > search_end:
            break
        delta = poly_m[k] - poly_mid_at_move
        if delta * expected_sign >= 0.02:
            response_time = poly_t[k] - btc_move_time
            poly_mid_after = poly_m[k]
            break

    if response_time is not None:
        lag_events.append({
            'time': btc_move_time,
            'move_pct': move_pct,
            'direction': direction,
            'poly_before': poly_mid_at_move,
            'poly_after': poly_mid_after,
            'lag': response_time,
        })

# Deduplicate: keep only 1 event per 30-second window
deduped = []
last_time = 0
for ev in sorted(lag_events, key=lambda x: x['time']):
    if ev['time'] - last_time >= 30:
        deduped.append(ev)
        last_time = ev['time']
lag_events = deduped

print(f"  Detected {len(lag_events):,} lag events (deduped to 1 per 30s)")

# ── Results ───────────────────────────────────────────────────────────────────
lags = [e['lag'] for e in lag_events]
lags_sorted = sorted(lags)

print(f"\n{'='*70}")
print(f"  LAG MEASUREMENT RESULTS")
print(f"{'='*70}")
print(f"  Events             : {len(lag_events):,}")
print(f"  Avg lag            : {np.mean(lags):.2f}s")
print(f"  Median lag         : {np.median(lags):.2f}s")
print(f"  P10                : {lags_sorted[int(len(lags)*0.10)]:.2f}s")
print(f"  P25                : {lags_sorted[int(len(lags)*0.25)]:.2f}s")
print(f"  P75                : {lags_sorted[int(len(lags)*0.75)]:.2f}s")
print(f"  P90                : {lags_sorted[int(len(lags)*0.90)]:.2f}s")

print(f"\n  Lag distribution:")
for thresh in [0.5, 1, 2, 3, 5, 10, 15, 30, 60]:
    count = sum(1 for l in lags if l <= thresh)
    print(f"    <= {thresh:>4.0f}s : {count:>6} ({count/len(lags)*100:>5.1f}%)")

print(f"\n  Lag by BTC move size:")
print(f"  {'Move %':>10} {'Count':>6} {'Avg Lag':>8} {'Med Lag':>8} {'P25':>6} {'P75':>6}")
print(f"  {'-'*48}")
for lo, hi, lbl in [(0.05, 0.10, '0.05-0.10%'), (0.10, 0.20, '0.10-0.20%'),
                     (0.20, 0.50, '0.20-0.50%'), (0.50, 1.0, '0.50-1.0%'),
                     (1.0, 99, '>1.0%')]:
    bucket = [e for e in lag_events if lo <= abs(e['move_pct']) < hi]
    if bucket:
        bl = sorted([e['lag'] for e in bucket])
        print(f"  {lbl:>10} {len(bucket):>6} {np.mean(bl):>7.2f}s {np.median(bl):>7.2f}s {bl[int(len(bl)*0.25)]:>5.1f}s {bl[int(len(bl)*0.75)]:>5.1f}s")

# ── Trade simulation ─────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  LATENCY ARB SIMULATION")
print(f"  Buy correct side at stale Poly odds, exit after repricing")
print(f"{'='*70}")

wins = 0
losses = 0
total_profit = 0
profits = []

for ev in lag_events:
    # Entry: buy at poly_before + half spread (assume 1c spread)
    if ev['direction'] == 'up':
        entry = ev['poly_before'] + 0.005  # buy Up at ask
        exit_p = ev['poly_after']           # sell Up after repricing
    else:
        entry = (1 - ev['poly_before']) + 0.005  # buy Down at ask
        exit_p = 1 - ev['poly_after']             # sell Down after repricing

    # Apply Poly fee on entry
    fee = entry * 0.25 * (entry * (1 - entry)) ** 2
    profit = exit_p - entry - fee

    profits.append(profit)
    total_profit += profit
    if profit > 0:
        wins += 1
    else:
        losses += 1

wr = wins / len(profits) * 100 if profits else 0
avg_p = np.mean(profits) if profits else 0
print(f"  Total trades       : {len(profits):,}")
print(f"  Wins               : {wins:,} ({wr:.1f}%)")
print(f"  Losses             : {losses:,}")
print(f"  Avg profit/trade   : {avg_p:.4f} (${avg_p*100:.2f} per $100)")
print(f"  Total profit       : {total_profit:.4f}")
print(f"  At $100/trade      : ${total_profit * 100:,.2f} over {hours:.0f}h")
print(f"  Per day (extrap)   : ${total_profit * 100 * 24 / hours:,.2f}")

# By move size
print(f"\n  Profitability by BTC move size:")
print(f"  {'Move %':>10} {'Trades':>7} {'WR%':>6} {'Avg Prof':>9} {'$/trade':>8}")
print(f"  {'-'*44}")
for lo, hi, lbl in [(0.05, 0.10, '0.05-0.10%'), (0.10, 0.20, '0.10-0.20%'),
                     (0.20, 0.50, '0.20-0.50%'), (0.50, 1.0, '0.50-1.0%'),
                     (1.0, 99, '>1.0%')]:
    idxs = [i for i, e in enumerate(lag_events) if lo <= abs(e['move_pct']) < hi]
    if idxs:
        bp = [profits[i] for i in idxs]
        w = sum(1 for p in bp if p > 0)
        print(f"  {lbl:>10} {len(idxs):>7} {w/len(idxs)*100:>5.1f}% {np.mean(bp):>+8.4f} ${np.mean(bp)*100:>+7.2f}")

# Only trades where lag > 1s (we'd realistically catch these)
catchable = [(lag_events[i], profits[i]) for i in range(len(lag_events)) if lag_events[i]['lag'] >= 1.0]
if catchable:
    c_profits = [c[1] for c in catchable]
    c_wins = sum(1 for p in c_profits if p > 0)
    print(f"\n  CATCHABLE trades (lag >= 1s):")
    print(f"    Trades: {len(catchable)} | WR: {c_wins/len(catchable)*100:.1f}% | Avg: ${np.mean(c_profits)*100:+.2f}/trade | Total: ${sum(c_profits)*100:,.2f}")

catchable2 = [(lag_events[i], profits[i]) for i in range(len(lag_events)) if lag_events[i]['lag'] >= 3.0]
if catchable2:
    c_profits2 = [c[1] for c in catchable2]
    c_wins2 = sum(1 for p in c_profits2 if p > 0)
    print(f"  EASY trades (lag >= 3s):")
    print(f"    Trades: {len(catchable2)} | WR: {c_wins2/len(catchable2)*100:.1f}% | Avg: ${np.mean(c_profits2)*100:+.2f}/trade | Total: ${sum(c_profits2)*100:,.2f}")

print(f"\n{'='*70}")
