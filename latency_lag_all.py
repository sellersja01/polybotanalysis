"""
latency_lag_all.py — Backtest latency arb across ALL databases
===============================================================
BTC 5m, BTC 15m, ETH 5m, ETH 15m, SOL 15m, XRP 15m
"""
import sqlite3
import numpy as np
from bisect import bisect_left
from datetime import datetime, timezone
import os

DATABASES = [
    ("databases/market_btc_5m.db",  "BTC 5m"),
    ("databases/market_btc_15m.db", "BTC 15m"),
    ("databases/market_eth_5m.db",  "ETH 5m"),
    ("databases/market_eth_15m.db", "ETH 15m"),
    ("databases/market_sol_15m.db", "SOL 15m"),
    ("databases/market_xrp_15m.db", "XRP 15m"),
]

LOOKBACK = 15
MOVE_THRESH = 0.05
MAX_ENTRY = 0.90

def poly_fee(price):
    return price * 0.25 * (price * (1 - price)) ** 2

def run_backtest(db_path, label):
    if not os.path.exists(db_path):
        print(f"  {label}: DB not found, skipping")
        return None

    conn = sqlite3.connect(db_path)

    # Check tables
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'asset_price' not in tables:
        print(f"  {label}: no asset_price table, skipping")
        conn.close()
        return None

    print(f"\n  Loading {label}...", flush=True)
    btc_raw = conn.execute("SELECT unix_time, price FROM asset_price ORDER BY unix_time").fetchall()
    btc_t = np.array([r[0] for r in btc_raw])
    btc_p = np.array([r[1] for r in btc_raw])
    print(f"    Price ticks: {len(btc_t):,}", flush=True)

    poly_raw = conn.execute("""
        SELECT unix_time, mid FROM polymarket_odds
        WHERE outcome = 'Up'
        ORDER BY unix_time
    """).fetchall()
    poly_t = np.array([r[0] for r in poly_raw])
    poly_m = np.array([r[1] for r in poly_raw])
    print(f"    Poly ticks:  {len(poly_t):,}", flush=True)
    conn.close()

    if len(btc_t) < 100 or len(poly_t) < 100:
        print(f"    Not enough data, skipping")
        return None

    hours = (btc_t[-1] - btc_t[0]) / 3600

    # Sample BTC at 1s intervals
    sample_times = np.arange(btc_t[0], btc_t[-1], 1.0)
    btc_sampled = np.interp(sample_times, btc_t, btc_p)

    lookback_samples = LOOKBACK
    lag_events = []

    for i in range(lookback_samples, len(sample_times)):
        move_pct = (btc_sampled[i] - btc_sampled[i - lookback_samples]) / btc_sampled[i - lookback_samples] * 100
        if abs(move_pct) < MOVE_THRESH:
            continue

        btc_move_time = sample_times[i]
        direction = "up" if move_pct > 0 else "down"

        poly_idx = bisect_left(poly_t, btc_move_time)
        if poly_idx >= len(poly_t) or poly_idx == 0:
            continue
        if poly_t[poly_idx] > btc_move_time:
            poly_idx -= 1
        if poly_idx < 0:
            continue

        poly_mid_at_move = poly_m[poly_idx]

        expected_sign = 1 if direction == "up" else -1
        response_time = None
        poly_mid_after = None

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

    # Dedup: 1 per 30s
    deduped = []
    last_time = 0
    for ev in sorted(lag_events, key=lambda x: x['time']):
        if ev['time'] - last_time >= 30:
            deduped.append(ev)
            last_time = ev['time']
    lag_events = deduped

    # Simulate trades
    wins = 0
    losses = 0
    profits = []
    for ev in lag_events:
        if ev['direction'] == 'up':
            entry = ev['poly_before'] + 0.005
            exit_p = ev['poly_after']
        else:
            entry = (1 - ev['poly_before']) + 0.005
            exit_p = 1 - ev['poly_after']

        if entry > MAX_ENTRY or entry <= 0:
            continue

        fee = poly_fee(entry)
        profit = exit_p - entry - fee
        profits.append(profit)
        if profit > 0:
            wins += 1
        else:
            losses += 1

    n = len(profits)
    if n == 0:
        print(f"    No trades")
        return None

    lags = [e['lag'] for e in lag_events]

    result = {
        'label': label,
        'hours': hours,
        'events': len(lag_events),
        'trades': n,
        'wins': wins,
        'losses': losses,
        'wr': wins / n * 100,
        'avg_profit': np.mean(profits),
        'total_profit': sum(profits),
        'avg_lag': np.mean(lags),
        'median_lag': np.median(lags),
        'per_day': sum(profits) * 100 * 24 / hours,
    }
    return result


print("=" * 70)
print("  LATENCY ARB BACKTEST — ALL MARKETS")
print(f"  Lookback: {LOOKBACK}s | Move thresh: {MOVE_THRESH}% | Max entry: {MAX_ENTRY}")
print("=" * 70)

results = []
for db_path, label in DATABASES:
    r = run_backtest(db_path, label)
    if r:
        results.append(r)

print(f"\n{'='*70}")
print(f"  RESULTS SUMMARY")
print(f"{'='*70}")
print(f"  {'Market':<10} {'Hours':>6} {'Events':>7} {'Trades':>7} {'WR%':>6} {'Avg$':>7} {'Total$':>9} {'$/day':>9} {'MedLag':>7}")
print(f"  {'-'*72}")

total_profit = 0
total_trades = 0
total_wins = 0

for r in results:
    print(
        f"  {r['label']:<10} {r['hours']:>5.0f}h {r['events']:>7,} {r['trades']:>7,} "
        f"{r['wr']:>5.1f}% ${r['avg_profit']*100:>5.2f} ${r['total_profit']*100:>8,.2f} "
        f"${r['per_day']:>8,.2f} {r['median_lag']:>6.1f}s"
    )
    total_profit += r['total_profit'] * 100
    total_trades += r['trades']
    total_wins += r['wins']

if total_trades:
    print(f"  {'-'*72}")
    print(f"  {'TOTAL':<10} {'':>6} {'':>7} {total_trades:>7,} {total_wins/total_trades*100:>5.1f}% {'':>7} ${total_profit:>8,.2f}")
    avg_hours = np.mean([r['hours'] for r in results])
    print(f"\n  Combined daily PnL at $100/trade: ${total_profit * 24 / avg_hours:,.2f}")

print(f"\n{'='*70}")
