"""
resolution_scalp_v2.py — Galindrast-style resolution scalping backtest
=======================================================================
Strategy: Watch Binance for BTC moves. When BTC moves significantly,
buy the correct side on Polymarket. Scale position size based on
how far into the candle and how certain the outcome is.

- Early entry (mid 0.40-0.60): smaller size, higher ROI if right
- Late entry (mid 0.80-0.97): bigger size, smaller ROI but near-certain
- Exit: hold to resolution ($1.00 or $0.00)

Test on 100% of candles using local data.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

DB = 'databases/market_btc_5m.db'

print("Loading all data into memory...", flush=True)
conn = sqlite3.connect(DB)

btc_raw = conn.execute("SELECT unix_time, price FROM asset_price ORDER BY unix_time").fetchall()
btc_t = np.array([r[0] for r in btc_raw])
btc_p = np.array([r[1] for r in btc_raw])

up_raw = conn.execute(
    "SELECT question, unix_time, mid, ask FROM polymarket_odds "
    "WHERE outcome='Up' ORDER BY question, unix_time").fetchall()
dn_raw = conn.execute(
    "SELECT question, unix_time, mid, ask FROM polymarket_odds "
    "WHERE outcome='Down' ORDER BY question, unix_time").fetchall()
conn.close()

# Group by candle
up_by_q = defaultdict(list)
dn_by_q = defaultdict(list)
for q, t, mid, ask in up_raw:
    up_by_q[q].append((t, mid, ask))
for q, t, mid, ask in dn_raw:
    dn_by_q[q].append((t, mid, ask))

candles = sorted(set(up_by_q.keys()) & set(dn_by_q.keys()))
del up_raw, dn_raw

# BTC sampled at 1s intervals for fast lookup
sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
sample_p = np.interp(sample_t, btc_t, btc_p)

hours = (btc_t[-1] - btc_t[0]) / 3600
print(f"  BTC ticks: {len(btc_t):,} | Candles: {len(candles)} | Hours: {hours:.0f}", flush=True)

def poly_fee(p):
    return p * 0.25 * (p * (1 - p)) ** 2


# ── Strategy configs to test ─────────────────────────────────────────────────
configs = [
    # (name, min_btc_move%, lookback_s, min_poly_mid, max_poly_mid, shares_base)
    ("Early aggressive (mid 0.40-0.60)", 0.05, 15, 0.40, 0.60, 100),
    ("Mid confidence (mid 0.55-0.75)",   0.05, 15, 0.55, 0.75, 100),
    ("Late scalp (mid 0.75-0.90)",       0.05, 15, 0.75, 0.90, 100),
    ("Resolution scalp (mid 0.90-0.97)", 0.05, 15, 0.90, 0.97, 100),
    ("Ultra late (mid 0.95-0.99)",       0.05, 15, 0.95, 0.99, 100),
    ("Galindrast combo (mid 0.40-0.97)", 0.05, 15, 0.40, 0.97, 100),
    ("Galindrast combo (mid 0.50-0.97)", 0.08, 15, 0.50, 0.97, 100),
    ("Galindrast combo (mid 0.60-0.97)", 0.10, 15, 0.60, 0.97, 100),
]

print(f"\n{'=' * 85}")
print(f"  GALINDRAST-STYLE RESOLUTION SCALPING BACKTEST")
print(f"  BTC 5m | {len(candles)} candles | {hours:.0f} hours")
print(f"  Enter when: BTC moves on Binance + Poly mid in target range")
print(f"  Exit: hold to candle resolution (highest mid at last tick)")
print(f"{'=' * 85}")


def run_config(name, move_thresh, lookback, min_mid, max_mid, shares):
    trades = []

    for question in candles:
        up = up_by_q[question]
        dn = dn_by_q[question]
        if len(up) < 5 or len(dn) < 5:
            continue

        # Winner = highest mid at last tick
        winner = 'Up' if up[-1][1] >= dn[-1][1] else 'Down'

        candle_start = min(up[0][0], dn[0][0])
        candle_end = max(up[-1][0], dn[-1][0])

        # Check BTC for moves throughout this candle
        entered = False

        # Sample BTC every 5 seconds during candle
        for check_t in np.arange(candle_start + lookback, candle_end, 5):
            if entered:
                break

            # BTC move in last lookback seconds
            idx_now = bisect_right(sample_t, check_t) - 1
            idx_prev = bisect_right(sample_t, check_t - lookback) - 1
            if idx_now < 0 or idx_prev < 0 or idx_now >= len(sample_p):
                continue

            btc_move = (sample_p[idx_now] - sample_p[idx_prev]) / sample_p[idx_prev] * 100
            if abs(btc_move) < move_thresh:
                continue

            direction = 'Up' if btc_move > 0 else 'Down'

            # Get current Poly odds for the direction we'd buy
            if direction == 'Up':
                ticks = up
            else:
                ticks = dn

            # Find latest poly tick at or before check_t
            poly_idx = None
            for j in range(len(ticks) - 1, -1, -1):
                if ticks[j][0] <= check_t:
                    poly_idx = j
                    break
            if poly_idx is None:
                continue

            current_mid = ticks[poly_idx][1]
            current_ask = ticks[poly_idx][2]

            # Check if mid is in our target range
            if current_mid < min_mid or current_mid > max_mid:
                continue
            if current_ask <= 0 or current_ask > 0.99:
                continue

            # ENTER!
            entry_price = current_ask
            fee = poly_fee(entry_price)
            payout = 1.0 if direction == winner else 0.0
            profit_per_share = payout - entry_price - fee
            profit_dollars = profit_per_share * shares

            # Position size scaling: bigger when more certain
            # Galindrast buys 10,000 shares at 0.96 but 3,000 at 0.40
            if current_mid >= 0.90:
                scale = 3.0  # 3x size for near-certain
            elif current_mid >= 0.75:
                scale = 2.0
            elif current_mid >= 0.60:
                scale = 1.5
            else:
                scale = 1.0

            scaled_profit = profit_dollars * scale
            scaled_cost = entry_price * shares * scale

            trades.append({
                'profit': profit_dollars,
                'scaled_profit': scaled_profit,
                'entry': entry_price,
                'mid': current_mid,
                'won': direction == winner,
                'direction': direction,
                'cost': entry_price * shares,
                'scaled_cost': scaled_cost,
                'btc_move': btc_move,
            })
            entered = True

    return trades


print(f"\n  {'Config':<40} {'Trades':>6} {'WR%':>6} {'AvgWin':>8} {'AvgLoss':>9} "
      f"{'Avg/trd':>8} {'$/day':>9} {'MaxLoss':>8}")
print(f"  {'-' * 98}")

all_results = {}
for name, move, look, mn, mx, shares in configs:
    trades = run_config(name, move, look, mn, mx, shares)
    if not trades:
        print(f"  {name:<40} {'0':>6}")
        continue

    wins = [t for t in trades if t['won']]
    losses = [t for t in trades if not t['won']]
    wr = len(wins) / len(trades) * 100

    # Use scaled profits
    wp = [t['scaled_profit'] for t in wins] if wins else [0]
    lp = [t['scaled_profit'] for t in losses] if losses else [0]
    all_p = [t['scaled_profit'] for t in trades]
    total = sum(all_p)
    per_day = total * 24 / hours

    print(f"  {name:<40} {len(trades):>6} {wr:>5.1f}% "
          f"${np.mean(wp):>+6.2f} ${np.mean(lp):>+7.2f} "
          f"${np.mean(all_p):>+6.2f} ${per_day:>+8,.2f} ${min(all_p):>+7.2f}")

    all_results[name] = trades

# ── Detailed analysis of best config ──────────────────────────────────────────
best_name = "Galindrast combo (mid 0.40-0.97)"
if best_name in all_results:
    trades = all_results[best_name]
    print(f"\n{'=' * 85}")
    print(f"  DETAILED: {best_name}")
    print(f"{'=' * 85}")

    wins = [t for t in trades if t['won']]
    losses = [t for t in trades if not t['won']]

    print(f"  Trades: {len(trades)} | Wins: {len(wins)} ({len(wins)/len(trades)*100:.1f}%) | Losses: {len(losses)}")

    # By entry mid bucket
    print(f"\n  BY ENTRY MID (where they entered):")
    print(f"  {'Mid Range':<15} {'Trades':>7} {'WR%':>6} {'AvgWin':>9} {'AvgLoss':>10} {'Avg/trd':>9} {'$/day':>9}")
    print(f"  {'-' * 70}")
    for lo, hi, lbl in [(0.40, 0.55, '0.40-0.55'), (0.55, 0.70, '0.55-0.70'),
                         (0.70, 0.80, '0.70-0.80'), (0.80, 0.90, '0.80-0.90'),
                         (0.90, 0.97, '0.90-0.97')]:
        bucket = [t for t in trades if lo <= t['mid'] < hi]
        if not bucket:
            continue
        bw = [t for t in bucket if t['won']]
        bl = [t for t in bucket if not t['won']]
        bwr = len(bw) / len(bucket) * 100
        bwp = [t['scaled_profit'] for t in bw] if bw else [0]
        blp = [t['scaled_profit'] for t in bl] if bl else [0]
        bap = [t['scaled_profit'] for t in bucket]
        print(f"  {lbl:<15} {len(bucket):>7} {bwr:>5.1f}% "
              f"${np.mean(bwp):>+7.2f} ${np.mean(blp):>+8.2f} "
              f"${np.mean(bap):>+7.2f} ${sum(bap)*24/hours:>+8,.2f}")

    # By BTC move size
    print(f"\n  BY BTC MOVE SIZE:")
    print(f"  {'Move%':<12} {'Trades':>7} {'WR%':>6} {'Avg/trd':>9}")
    print(f"  {'-' * 38}")
    for lo, hi, lbl in [(0.05, 0.10, '0.05-0.10'), (0.10, 0.20, '0.10-0.20'),
                         (0.20, 0.50, '0.20-0.50'), (0.50, 99, '>0.50')]:
        bucket = [t for t in trades if lo <= abs(t['btc_move']) < hi]
        if not bucket:
            continue
        bwr = sum(1 for t in bucket if t['won']) / len(bucket) * 100
        bap = [t['scaled_profit'] for t in bucket]
        print(f"  {lbl:<12} {len(bucket):>7} {bwr:>5.1f}% ${np.mean(bap):>+7.2f}")

    # Capital deployment simulation
    print(f"\n  CAPITAL DEPLOYMENT (with Galindrast-style scaling):")
    candles_per_day = len(trades) * 24 / hours
    print(f"  Trades/day: {candles_per_day:.0f}")
    print(f"  {'Base $':>10} {'Scale factor':>13} {'Daily PnL':>10}")
    print(f"  {'-' * 36}")
    for base in [100, 500, 1000, 3000, 5000]:
        daily = sum(t['scaled_profit'] for t in trades) * base / 100 * 24 / hours
        avg_cost = np.mean([t['scaled_cost'] for t in trades]) * base / 100
        print(f"  ${base:>9} avg deploy=${avg_cost:>7,.0f} ${daily:>+9,.2f}/day")

print(f"\n{'=' * 85}")
