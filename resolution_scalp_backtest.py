"""
resolution_scalp_backtest.py v2 — Fast version, loads all data into memory
"""
import sqlite3
import numpy as np
from collections import defaultdict

DB = 'databases/market_btc_5m.db'
conn = sqlite3.connect(DB)

print("Loading all data into memory...", flush=True)
up_raw = conn.execute(
    "SELECT question, unix_time, mid, ask FROM polymarket_odds "
    "WHERE outcome='Up' ORDER BY question, unix_time").fetchall()
dn_raw = conn.execute(
    "SELECT question, unix_time, mid, ask FROM polymarket_odds "
    "WHERE outcome='Down' ORDER BY question, unix_time").fetchall()
conn.close()

print(f"  Up ticks: {len(up_raw):,} | Down ticks: {len(dn_raw):,}", flush=True)

# Group by candle
up_by_q = defaultdict(list)
dn_by_q = defaultdict(list)
for q, t, mid, ask in up_raw:
    up_by_q[q].append((t, mid, ask))
for q, t, mid, ask in dn_raw:
    dn_by_q[q].append((t, mid, ask))

candles = sorted(set(up_by_q.keys()) & set(dn_by_q.keys()))
print(f"  Candles: {len(candles)}", flush=True)

del up_raw, dn_raw  # free memory

def poly_fee(p):
    return p * 0.25 * (p * (1 - p)) ** 2

results = defaultdict(list)
hours = 184

print("Running backtest...", flush=True)
for i, question in enumerate(candles):
    up = up_by_q[question]
    dn = dn_by_q[question]

    if len(up) < 5 or len(dn) < 5:
        continue

    winner = 'Up' if up[-1][1] >= dn[-1][1] else 'Down'
    duration = max(up[-1][0] - up[0][0], 1)

    for threshold in [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        for ticks, side in [(up, 'Up'), (dn, 'Down')]:
            for t, mid, ask in ticks:
                if mid >= threshold and 0 < ask <= 0.99:
                    fee = poly_fee(ask)
                    payout = 1.0 if side == winner else 0.0
                    profit = (payout - ask - fee) * 100
                    pct = (t - ticks[0][0]) / duration * 100
                    results[threshold].append({
                        'profit': profit, 'entry': ask,
                        'won': side == winner, 'candle_pct': pct,
                    })
                    break

print(f"\n{'=' * 78}")
print(f"  RESOLUTION SCALPING BACKTEST - BTC 5m ({len(candles)} candles, {hours}h)")
print(f"{'=' * 78}")
print(f"  {'Thresh':>6} {'Trades':>7} {'WR%':>6} {'AvgWin':>8} {'AvgLoss':>9} "
      f"{'Avg/trd':>8} {'Total':>10} {'$/day':>9} {'MaxLoss':>8}")
print(f"  {'-' * 76}")

for threshold in [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
    trades = results[threshold]
    if not trades:
        continue
    wins = [t for t in trades if t['won']]
    losses = [t for t in trades if not t['won']]
    wr = len(wins) / len(trades) * 100
    wp = [t['profit'] for t in wins] if wins else [0]
    lp = [t['profit'] for t in losses] if losses else [0]
    all_p = [t['profit'] for t in trades]
    total = sum(all_p)
    per_day = total * 24 / hours

    print(f"  {threshold:>6.2f} {len(trades):>7} {wr:>5.1f}% "
          f"${np.mean(wp):>+6.2f} ${np.mean(lp):>+7.2f} "
          f"${np.mean(all_p):>+6.2f} "
          f"${total:>+9,.2f} ${per_day:>+8,.2f} ${min(all_p):>+7.2f}")

# Detail for 0.90
print(f"\n{'=' * 78}")
print(f"  DETAIL: Threshold = 0.90")
print(f"{'=' * 78}")
t90 = results[0.90]
if t90:
    wins = [t for t in t90 if t['won']]
    losses = [t for t in t90 if not t['won']]
    print(f"  Trades: {len(t90)} | Wins: {len(wins)} ({len(wins)/len(t90)*100:.1f}%)")
    print(f"  Entry price  - wins:   {np.mean([t['entry'] for t in wins]):.4f}")
    if losses:
        print(f"  Entry price  - losses: {np.mean([t['entry'] for t in losses]):.4f}")
        lp = [t['profit'] for t in losses]
        print(f"  Avg loss: ${np.mean(lp):+.2f} | Max loss: ${min(lp):+.2f} | Median: ${np.median(lp):+.2f}")
        lpct = [t['candle_pct'] for t in losses]
        print(f"  Loss timing: avg={np.mean(lpct):.0f}% through candle")

    pcts = [t['candle_pct'] for t in t90]
    print(f"  Entry timing: avg={np.mean(pcts):.0f}% med={np.median(pcts):.0f}%")

    candles_day = len(t90) * 24 / hours
    avg_profit = np.mean([t['profit'] for t in t90])
    print(f"\n  Candles/day with opportunity: {candles_day:.0f}")
    print(f"  {'Capital':>10} {'Profit/trade':>13} {'Daily PnL':>10}")
    print(f"  {'-' * 36}")
    for cap in [100, 500, 1000, 5000, 10000]:
        pnl_per = avg_profit * cap / 100
        daily = pnl_per * candles_day
        print(f"  ${cap:>9} ${pnl_per:>+11.2f} ${daily:>+9,.2f}")

print(f"\n{'=' * 78}")
