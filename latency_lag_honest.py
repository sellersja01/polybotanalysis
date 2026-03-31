"""
latency_lag_honest.py — HONEST backtest: enter on EVERY BTC move, track real outcomes
======================================================================================
No cherry-picking. When BTC moves >= threshold, we enter.
Then check what Poly did over the next 30/60/120 seconds.
"""
import sqlite3
import numpy as np
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

DATABASES = [
    ("databases/market_btc_5m.db",  "BTC 5m"),
    ("databases/market_btc_15m.db", "BTC 15m"),
    ("databases/market_eth_5m.db",  "ETH 5m"),
]

LOOKBACK = 15
MOVE_THRESH = 0.05
MAX_ENTRY = 0.90
COOLDOWN = 30  # seconds between entries


def poly_fee(price):
    return price * 0.25 * (price * (1 - price)) ** 2


def run_honest(db_path, label):
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'asset_price' not in tables:
        conn.close()
        return None

    print(f"\n  Loading {label}...", flush=True)
    btc_raw = conn.execute("SELECT unix_time, price FROM asset_price ORDER BY unix_time").fetchall()
    btc_t = np.array([r[0] for r in btc_raw])
    btc_p = np.array([r[1] for r in btc_raw])

    # Load BOTH Up and Down mids
    up_raw = conn.execute("SELECT unix_time, mid, ask FROM polymarket_odds WHERE outcome='Up' ORDER BY unix_time").fetchall()
    dn_raw = conn.execute("SELECT unix_time, mid, ask FROM polymarket_odds WHERE outcome='Down' ORDER BY unix_time").fetchall()
    up_t = np.array([r[0] for r in up_raw])
    up_m = np.array([r[1] for r in up_raw])
    up_a = np.array([r[2] for r in up_raw])
    dn_t = np.array([r[0] for r in dn_raw])
    dn_m = np.array([r[1] for r in dn_raw])
    dn_a = np.array([r[2] for r in dn_raw])
    conn.close()

    print(f"    Price: {len(btc_t):,} | Up: {len(up_t):,} | Down: {len(dn_t):,}", flush=True)

    hours = (btc_t[-1] - btc_t[0]) / 3600

    # Sample BTC every 1s
    sample_times = np.arange(btc_t[0], btc_t[-1], 1.0)
    btc_sampled = np.interp(sample_times, btc_t, btc_p)

    trades = []
    last_entry = 0

    for i in range(LOOKBACK, len(sample_times)):
        move_pct = (btc_sampled[i] - btc_sampled[i - LOOKBACK]) / btc_sampled[i - LOOKBACK] * 100
        if abs(move_pct) < MOVE_THRESH:
            continue

        t_now = sample_times[i]
        if t_now - last_entry < COOLDOWN:
            continue

        direction = "up" if move_pct > 0 else "down"

        # Get current Poly ask for the side we'd buy
        if direction == "up":
            idx = bisect_right(up_t, t_now) - 1
            if idx < 0 or idx >= len(up_t):
                continue
            entry_ask = up_a[idx]
            entry_mid = up_m[idx]
        else:
            idx = bisect_right(dn_t, t_now) - 1
            if idx < 0 or idx >= len(dn_t):
                continue
            entry_ask = dn_a[idx]
            entry_mid = dn_m[idx]

        # Skip if entry too high or too low
        if entry_ask <= 0.01 or entry_ask > MAX_ENTRY:
            continue
        # Skip if Poly already repriced (mid > 0.55 means market already moved)
        if entry_mid > 0.55:
            continue

        last_entry = t_now
        fee = poly_fee(entry_ask)

        # Track what happens over next 10s, 30s, 60s, and at candle end
        outcomes = {}
        for horizon_label, horizon_s in [("10s", 10), ("30s", 30), ("60s", 60), ("120s", 120)]:
            t_exit = t_now + horizon_s
            if direction == "up":
                exit_idx = bisect_right(up_t, t_exit) - 1
                if exit_idx >= 0 and exit_idx < len(up_t):
                    exit_mid = up_m[exit_idx]
                    profit = exit_mid - entry_ask - fee
                    outcomes[horizon_label] = profit
            else:
                exit_idx = bisect_right(dn_t, t_exit) - 1
                if exit_idx >= 0 and exit_idx < len(dn_t):
                    exit_mid = dn_m[exit_idx]
                    profit = exit_mid - entry_ask - fee
                    outcomes[horizon_label] = profit

        trades.append({
            'time': t_now,
            'direction': direction,
            'move_pct': move_pct,
            'entry_ask': entry_ask,
            'entry_mid': entry_mid,
            'fee': fee,
            'outcomes': outcomes,
        })

    if not trades:
        print(f"    No trades")
        return None

    # Report
    print(f"    Trades: {len(trades):,} over {hours:.0f}h ({len(trades)/hours*24:.0f}/day)")

    result = {'label': label, 'hours': hours, 'n': len(trades)}

    for horizon in ["10s", "30s", "60s", "120s"]:
        profits = [t['outcomes'].get(horizon, 0) for t in trades if horizon in t['outcomes']]
        if not profits:
            continue
        wins = sum(1 for p in profits if p > 0)
        n = len(profits)
        wr = wins / n * 100
        avg_p = np.mean(profits)
        total = sum(profits)
        per_day = total * 100 * 24 / hours

        result[horizon] = {
            'n': n, 'wins': wins, 'wr': wr,
            'avg': avg_p, 'total': total, 'per_day': per_day,
        }

    # By move size for 30s horizon
    print(f"\n    Win rate by move size (exit after 30s):")
    print(f"    {'Move%':>8} {'Trades':>7} {'WR%':>6} {'Avg$':>8} {'$/day':>9}")
    print(f"    {'-'*42}")
    for lo, hi, lbl in [(0.05, 0.08, '0.05-0.08'), (0.08, 0.12, '0.08-0.12'),
                         (0.12, 0.20, '0.12-0.20'), (0.20, 0.50, '0.20-0.50'),
                         (0.50, 99, '>0.50')]:
        bucket = [t for t in trades if lo <= abs(t['move_pct']) < hi and '30s' in t['outcomes']]
        if bucket:
            p = [t['outcomes']['30s'] for t in bucket]
            w = sum(1 for x in p if x > 0)
            print(f"    {lbl:>8} {len(bucket):>7} {w/len(bucket)*100:>5.1f}% ${np.mean(p)*100:>+6.2f} ${sum(p)*100*24/hours:>8,.2f}")

    return result


print("=" * 70)
print("  HONEST LATENCY ARB BACKTEST")
print(f"  Enter on EVERY BTC move >= {MOVE_THRESH}% in {LOOKBACK}s")
print(f"  Track outcome at 10s, 30s, 60s, 120s after entry")
print(f"  Max entry price: {MAX_ENTRY} | Cooldown: {COOLDOWN}s")
print(f"  Only enter when poly mid < 0.55 (stale)")
print("=" * 70)

all_results = []
for db_path, label in DATABASES:
    r = run_honest(db_path, label)
    if r:
        all_results.append(r)

print(f"\n{'='*70}")
print(f"  SUMMARY — Win Rate & PnL by Exit Horizon")
print(f"{'='*70}")
print(f"  {'Market':<10} {'Trades':>7} | {'10s WR':>7} {'10s $/d':>9} | {'30s WR':>7} {'30s $/d':>9} | {'60s WR':>7} {'60s $/d':>9}")
print(f"  {'-'*80}")

for r in all_results:
    parts = [f"  {r['label']:<10} {r['n']:>7} |"]
    for h in ["10s", "30s", "60s"]:
        if h in r:
            d = r[h]
            parts.append(f" {d['wr']:>5.1f}% ${d['per_day']:>8,.2f} |")
        else:
            parts.append(f" {'N/A':>6} {'N/A':>9} |")
    print("".join(parts))

print(f"\n{'='*70}")
