"""
resolution_scalp_v3.py — Galindrast-style DCA throughout candle
================================================================
Strategy: After initial BTC signal, DCA into the winning side throughout
the entire candle. Buy more as confidence increases (price rises).
Scale position size based on current mid price.
Hold ALL entries to candle resolution.
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

up_by_q = defaultdict(list)
dn_by_q = defaultdict(list)
for q, t, mid, ask in up_raw:
    up_by_q[q].append((t, mid, ask))
for q, t, mid, ask in dn_raw:
    dn_by_q[q].append((t, mid, ask))

candles = sorted(set(up_by_q.keys()) & set(dn_by_q.keys()))
del up_raw, dn_raw

sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
sample_p = np.interp(sample_t, btc_t, btc_p)

hours = (btc_t[-1] - btc_t[0]) / 3600
print(f"  BTC: {len(btc_t):,} | Candles: {len(candles)} | Hours: {hours:.0f}", flush=True)

def poly_fee(p):
    return p * 0.25 * (p * (1 - p)) ** 2


# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK = 15          # BTC move lookback (seconds)
MOVE_THRESH = 0.05     # min BTC move % to trigger initial signal
DCA_INTERVAL = 15      # buy every 15 seconds after signal
BASE_SHARES = 10       # base shares per DCA buy

# Size scaling based on confidence (poly mid)
def get_shares(mid):
    if mid >= 0.95:   return BASE_SHARES * 10   # 100 shares — near certain
    elif mid >= 0.90: return BASE_SHARES * 8    # 80 shares
    elif mid >= 0.80: return BASE_SHARES * 5    # 50 shares
    elif mid >= 0.70: return BASE_SHARES * 3    # 30 shares
    elif mid >= 0.55: return BASE_SHARES * 2    # 20 shares
    else:             return BASE_SHARES * 1    # 10 shares


print(f"\nRunning DCA backtest...", flush=True)

candle_results = []

for ci, question in enumerate(candles):
    if ci % 200 == 0:
        print(f"  [{ci}/{len(candles)}]...", flush=True)

    up = up_by_q[question]
    dn = dn_by_q[question]
    if len(up) < 5 or len(dn) < 5:
        continue

    winner = 'Up' if up[-1][1] >= dn[-1][1] else 'Down'
    candle_start = min(up[0][0], dn[0][0])
    candle_end = max(up[-1][0], dn[-1][0])

    # Find initial BTC signal
    signal_dir = None
    signal_time = None

    for check_t in np.arange(candle_start + LOOKBACK, candle_end, 5):
        idx_now = bisect_right(sample_t, check_t) - 1
        idx_prev = bisect_right(sample_t, check_t - LOOKBACK) - 1
        if idx_now < 0 or idx_prev < 0 or idx_now >= len(sample_p):
            continue

        btc_move = (sample_p[idx_now] - sample_p[idx_prev]) / sample_p[idx_prev] * 100
        if abs(btc_move) >= MOVE_THRESH:
            signal_dir = 'Up' if btc_move > 0 else 'Down'
            signal_time = check_t
            break

    if not signal_dir:
        continue

    # DCA into the signal direction from signal_time through candle end
    ticks = up if signal_dir == 'Up' else dn
    entries = []
    last_buy_t = 0

    for t, mid, ask in ticks:
        if t < signal_time:
            continue
        if t - last_buy_t < DCA_INTERVAL:
            continue
        if ask <= 0 or ask > 0.99:
            continue
        if mid < 0.10:  # don't buy if mid is too low (wrong side)
            continue

        shares = get_shares(mid)
        fee = poly_fee(ask)
        cost = ask * shares + fee * shares
        payout = 1.0 * shares if signal_dir == winner else 0.0

        entries.append({
            'time': t,
            'mid': mid,
            'ask': ask,
            'shares': shares,
            'cost': cost,
            'fee': fee * shares,
            'payout': payout,
        })
        last_buy_t = t

    if not entries:
        continue

    total_shares = sum(e['shares'] for e in entries)
    total_cost = sum(e['cost'] for e in entries)
    total_payout = sum(e['payout'] for e in entries)
    total_fee = sum(e['fee'] for e in entries)
    pnl = total_payout - total_cost
    avg_entry = total_cost / total_shares if total_shares > 0 else 0
    won = signal_dir == winner

    candle_results.append({
        'question': question,
        'signal_dir': signal_dir,
        'winner': winner,
        'won': won,
        'n_entries': len(entries),
        'total_shares': total_shares,
        'total_cost': total_cost,
        'total_payout': total_payout,
        'pnl': pnl,
        'avg_entry': avg_entry,
        'total_fee': total_fee,
    })

# ── Results ───────────────────────────────────────────────────────────────────
wins = [r for r in candle_results if r['won']]
losses = [r for r in candle_results if not r['won']]

print(f"\n{'=' * 80}")
print(f"  GALINDRAST DCA BACKTEST RESULTS")
print(f"  BTC 5m | {len(candles)} candles | {hours:.0f} hours")
print(f"  DCA every {DCA_INTERVAL}s | Scale: {BASE_SHARES}sh base, up to {BASE_SHARES*10}sh at 0.95+")
print(f"{'=' * 80}")

print(f"\n  Candles with signal: {len(candle_results)}")
print(f"  Wins:   {len(wins)} ({len(wins)/len(candle_results)*100:.1f}%)")
print(f"  Losses: {len(losses)} ({len(losses)/len(candle_results)*100:.1f}%)")

all_pnl = [r['pnl'] for r in candle_results]
win_pnl = [r['pnl'] for r in wins]
loss_pnl = [r['pnl'] for r in losses]

print(f"\n  Avg PnL per candle:   ${np.mean(all_pnl):+.2f}")
print(f"  Avg win:              ${np.mean(win_pnl):+.2f}")
print(f"  Avg loss:             ${np.mean(loss_pnl):+.2f}")
print(f"  Win/Loss ratio:       {abs(np.mean(win_pnl)/np.mean(loss_pnl)):.2f}x")
print(f"  Max win:              ${max(all_pnl):+.2f}")
print(f"  Max loss:             ${min(all_pnl):+.2f}")
print(f"  Total PnL:            ${sum(all_pnl):+,.2f}")
print(f"  Daily PnL:            ${sum(all_pnl)*24/hours:+,.2f}")

print(f"\n  Avg entries/candle:   {np.mean([r['n_entries'] for r in candle_results]):.1f}")
print(f"  Avg shares/candle:    {np.mean([r['total_shares'] for r in candle_results]):.0f}")
print(f"  Avg cost/candle:      ${np.mean([r['total_cost'] for r in candle_results]):,.2f}")
print(f"  Avg fees/candle:      ${np.mean([r['total_fee'] for r in candle_results]):.2f}")

# ── Scaling ───────────────────────────────────────────────────────────────────
print(f"\n  SCALING (multiply BASE_SHARES):")
candles_day = len(candle_results) * 24 / hours
print(f"  Candles/day: {candles_day:.0f}")
print(f"  {'Base':>6} {'Avg Cost':>10} {'Daily PnL':>10} {'Monthly':>12}")
print(f"  {'-' * 42}")
for mult in [1, 5, 10, 30, 50, 100]:
    daily = sum(all_pnl) * mult * 24 / hours
    avg_cost = np.mean([r['total_cost'] for r in candle_results]) * mult
    monthly = daily * 30
    print(f"  {mult:>5}x ${avg_cost:>9,.0f} ${daily:>+9,.2f} ${monthly:>+11,.2f}")

# ── Win rate by entry count ───────────────────────────────────────────────────
print(f"\n  WIN RATE BY ENTRIES PER CANDLE:")
print(f"  {'Entries':<12} {'Candles':>8} {'WR%':>6} {'Avg PnL':>9}")
print(f"  {'-' * 38}")
for lo, hi, lbl in [(1, 3, '1-2'), (3, 6, '3-5'), (6, 10, '6-9'),
                     (10, 15, '10-14'), (15, 25, '15-24'), (25, 999, '25+')]:
    bucket = [r for r in candle_results if lo <= r['n_entries'] < hi]
    if bucket:
        bwr = sum(1 for r in bucket if r['won']) / len(bucket) * 100
        bap = np.mean([r['pnl'] for r in bucket])
        print(f"  {lbl:<12} {len(bucket):>8} {bwr:>5.1f}% ${bap:>+7.2f}")

# ── Loss analysis ─────────────────────────────────────────────────────────────
print(f"\n  LOSS ANALYSIS:")
if losses:
    print(f"  {'Shares':<10} {'Cost':<10} {'Loss':<10} {'Avg Entry':<10} {'Entries':<8}")
    print(f"  {'-' * 50}")
    for r in sorted(losses, key=lambda x: x['pnl'])[:10]:
        print(f"  {r['total_shares']:<10.0f} ${r['total_cost']:<9,.0f} ${r['pnl']:<+9,.0f} {r['avg_entry']:<10.3f} {r['n_entries']:<8}")

print(f"\n{'=' * 80}")
