"""
arb_analysis.py — Analyze cross-platform arbitrage opportunities
================================================================
Reads arb_collector.db (Polymarket + Kalshi side-by-side snapshots)
and measures gap frequency, size, duration, and profitability.
"""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB = 'databases/arb_collector.db'

conn = sqlite3.connect(DB)

# ── Basic stats ───────────────────────────────────────────────────────────────
total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
r = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
hours = (r[1] - r[0]) / 3600
start = datetime.fromtimestamp(r[0], tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
end   = datetime.fromtimestamp(r[1], tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

print(f"{'='*65}")
print(f"  ARB COLLECTOR ANALYSIS")
print(f"{'='*65}")
print(f"  Rows    : {total:,}")
print(f"  Period  : {start} -> {end}")
print(f"  Duration: {hours:.1f} hours")
print(f"  Outcomes: {conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]}")

# ── Per-asset breakdown ───────────────────────────────────────────────────────
print(f"\n  Per-asset row counts:")
for row in conn.execute("SELECT asset, COUNT(*) FROM snapshots GROUP BY asset ORDER BY asset"):
    print(f"    {row[0]}: {row[1]:,}")

# ── Fee functions ─────────────────────────────────────────────────────────────
def poly_fee(price, shares=1):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

def kalshi_fee(price, contracts=1):
    return contracts * 0.07 * price * (1 - price)

# ── Arb gap analysis ─────────────────────────────────────────────────────────
# For each snapshot where BOTH platforms have valid prices (0.05-0.95),
# check if poly_up_ask + kalshi_dn_ask < 1.00 (or vice versa)
print(f"\n{'='*65}")
print(f"  ARB GAP ANALYSIS")
print(f"{'='*65}")

rows = conn.execute("""
    SELECT ts, asset, candle_id,
           p_up_bid, p_up_ask, p_dn_bid, p_dn_ask,
           k_up_bid, k_up_ask, k_dn_bid, k_dn_ask
    FROM snapshots
    WHERE p_up_bid > 0 AND p_up_ask > 0 AND p_dn_bid > 0 AND p_dn_ask > 0
      AND k_up_bid > 0 AND k_up_ask > 0 AND k_dn_bid > 0 AND k_dn_ask > 0
      AND p_up_ask < 0.95 AND p_dn_ask < 0.95
      AND k_up_ask < 0.95 AND k_dn_ask < 0.95
    ORDER BY ts
""").fetchall()

print(f"  Active rows (both platforms, 0.05-0.95): {len(rows):,}")

# For each row, compute raw gap and net gap (after fees) for both directions
# Direction A: buy Poly Up + Kalshi Down  → cost = p_up_ask + k_dn_ask, payout = $1
# Direction B: buy Poly Down + Kalshi Up  → cost = p_dn_ask + k_up_ask, payout = $1

results = []
for ts, asset, candle, pub, pua, pdb, pda, kub, kua, kdb, kda in rows:
    # Direction A: Poly Up + Kalshi Down
    cost_a = pua + kda
    raw_gap_a = 1.0 - cost_a
    fee_a = poly_fee(pua) + kalshi_fee(kda)
    net_a = raw_gap_a - fee_a

    # Direction B: Poly Down + Kalshi Up
    cost_b = pda + kua
    raw_gap_b = 1.0 - cost_b
    fee_b = poly_fee(pda) + kalshi_fee(kua)
    net_b = raw_gap_b - fee_b

    # Best direction
    if net_a >= net_b:
        best_raw, best_net, best_dir = raw_gap_a, net_a, 'A'
        best_fee = fee_a
    else:
        best_raw, best_net, best_dir = raw_gap_b, net_b, 'B'
        best_fee = fee_b

    results.append({
        'ts': ts, 'asset': asset, 'candle': candle,
        'raw_a': raw_gap_a, 'net_a': net_a,
        'raw_b': raw_gap_b, 'net_b': net_b,
        'best_raw': best_raw, 'best_net': best_net,
        'best_dir': best_dir, 'best_fee': best_fee,
    })

# ── Summary stats ─────────────────────────────────────────────────────────────
def avg(lst): return sum(lst)/len(lst) if lst else 0

profitable = [r for r in results if r['best_net'] > 0]
print(f"\n  Total ticks analyzed     : {len(results):,}")
print(f"  Ticks with net profit > 0: {len(profitable):,} ({len(profitable)/len(results)*100:.1f}%)")

if profitable:
    print(f"  Avg net gap (profitable) : {avg([r['best_net'] for r in profitable]):.4f} (${avg([r['best_net'] for r in profitable])*100:.1f} per $100)")
    print(f"  Max net gap              : {max(r['best_net'] for r in profitable):.4f}")
    print(f"  Min net gap (profitable) : {min(r['best_net'] for r in profitable):.4f}")

# Raw gap distribution (before fees)
print(f"\n  RAW GAP DISTRIBUTION (best direction, before fees):")
thresholds = [0.10, 0.08, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01, 0.00, -0.02, -0.05]
for t in thresholds:
    count = sum(1 for r in results if r['best_raw'] >= t)
    pct = count / len(results) * 100 if results else 0
    print(f"    >= {t:>+.2f} : {count:>7,}  ({pct:>5.1f}%)")

# Net gap distribution (after fees)
print(f"\n  NET GAP DISTRIBUTION (after both platforms' fees):")
thresholds2 = [0.08, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01, 0.005, 0.00, -0.01, -0.02]
for t in thresholds2:
    count = sum(1 for r in results if r['best_net'] >= t)
    pct = count / len(results) * 100 if results else 0
    label = f"${t*100:>+5.1f}/sh" if t != 0 else " break-even"
    print(f"    >= {t:>+.3f} ({label}): {count:>7,}  ({pct:>5.1f}%)")

# ── Per-asset breakdown ───────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  PER-ASSET BREAKDOWN")
print(f"{'='*65}")
print(f"  {'Asset':<6} {'Ticks':>7} {'Prof%':>6} {'Avg Net':>8} {'Max Net':>8} {'Avg Raw':>8}")
print(f"  {'-'*48}")
for asset in ['BTC', 'ETH', 'SOL', 'XRP']:
    ar = [r for r in results if r['asset'] == asset]
    if not ar:
        continue
    pr = [r for r in ar if r['best_net'] > 0]
    pct = len(pr) / len(ar) * 100 if ar else 0
    avg_net = avg([r['best_net'] for r in pr]) if pr else 0
    max_net = max([r['best_net'] for r in pr]) if pr else 0
    avg_raw = avg([r['best_raw'] for r in ar])
    print(f"  {asset:<6} {len(ar):>7,} {pct:>5.1f}% {avg_net:>+.4f} {max_net:>+.4f} {avg_raw:>+.4f}")

# ── Time-based analysis ──────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  PROFITABLE GAPS BY HOUR (UTC)")
print(f"{'='*65}")
by_hour = defaultdict(lambda: {'total': 0, 'profit': 0, 'nets': []})
for r in results:
    h = datetime.fromtimestamp(r['ts'], tz=timezone.utc).hour
    by_hour[h]['total'] += 1
    if r['best_net'] > 0:
        by_hour[h]['profit'] += 1
        by_hour[h]['nets'].append(r['best_net'])

print(f"  {'Hour':>4} {'Total':>7} {'Prof':>6} {'%':>6} {'Avg Net':>8}")
print(f"  {'-'*36}")
for h in sorted(by_hour):
    d = by_hour[h]
    pct = d['profit'] / d['total'] * 100 if d['total'] else 0
    an = avg(d['nets']) if d['nets'] else 0
    print(f"  {h:>4}  {d['total']:>7,} {d['profit']:>6,} {pct:>5.1f}% {an:>+.4f}")

# ── Gap duration analysis ─────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  GAP DURATION ANALYSIS (consecutive profitable ticks)")
print(f"{'='*65}")

# Group by asset+candle, find streaks of profitable ticks
streaks = []
for asset in ['BTC', 'ETH', 'SOL', 'XRP']:
    ar = sorted([r for r in results if r['asset'] == asset], key=lambda x: x['ts'])
    in_streak = False
    streak_start = 0
    streak_nets = []
    for r in ar:
        if r['best_net'] > 0:
            if not in_streak:
                streak_start = r['ts']
                streak_nets = []
                in_streak = True
            streak_nets.append(r['best_net'])
        else:
            if in_streak:
                duration = r['ts'] - streak_start
                streaks.append({
                    'asset': asset, 'duration': duration,
                    'avg_net': avg(streak_nets), 'max_net': max(streak_nets),
                    'n_ticks': len(streak_nets),
                    'start': streak_start,
                })
                in_streak = False
    if in_streak:
        duration = ar[-1]['ts'] - streak_start
        streaks.append({
            'asset': asset, 'duration': duration,
            'avg_net': avg(streak_nets), 'max_net': max(streak_nets),
            'n_ticks': len(streak_nets),
            'start': streak_start,
        })

if streaks:
    print(f"  Total profitable streaks: {len(streaks)}")
    print(f"  Avg duration            : {avg([s['duration'] for s in streaks]):.1f}s")
    print(f"  Max duration            : {max(s['duration'] for s in streaks):.1f}s")
    print(f"  Avg ticks per streak    : {avg([s['n_ticks'] for s in streaks]):.1f}")
    print(f"\n  Duration distribution:")
    for thresh in [1, 5, 10, 30, 60, 120, 300]:
        c = sum(1 for s in streaks if s['duration'] >= thresh)
        print(f"    >= {thresh:>3}s : {c:>5} streaks")

    # Top 10 longest streaks
    print(f"\n  Top 10 longest profitable streaks:")
    print(f"  {'Asset':<5} {'Dur(s)':>7} {'Ticks':>6} {'Avg Net':>8} {'Max Net':>8} {'Time (UTC)'}")
    print(f"  {'-'*60}")
    for s in sorted(streaks, key=lambda x: -x['duration'])[:10]:
        t = datetime.fromtimestamp(s['start'], tz=timezone.utc).strftime('%H:%M:%S')
        print(f"  {s['asset']:<5} {s['duration']:>7.1f} {s['n_ticks']:>6} {s['avg_net']:>+.4f} {s['max_net']:>+.4f}  {t}")
else:
    print("  No profitable streaks found.")

# ── Per-candle PnL simulation ─────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  PER-CANDLE PnL SIMULATION ($100/trade)")
print(f"{'='*65}")

# For each candle, take the BEST single net gap opportunity
candle_best = {}
for r in results:
    key = (r['asset'], r['candle'])
    if key not in candle_best or r['best_net'] > candle_best[key]['best_net']:
        candle_best[key] = r

profitable_candles = {k: v for k, v in candle_best.items() if v['best_net'] > 0}
print(f"  Total candles with data  : {len(candle_best)}")
print(f"  Candles with arb opp     : {len(profitable_candles)} ({len(profitable_candles)/len(candle_best)*100:.1f}%)")
if profitable_candles:
    nets = [v['best_net'] for v in profitable_candles.values()]
    print(f"  Avg best net gap/candle  : {avg(nets):.4f} (${avg(nets)*100:.2f} per $100)")
    print(f"  Max best net gap         : {max(nets):.4f} (${max(nets)*100:.2f} per $100)")
    total_pnl = sum(nets) * 100  # $100 per trade
    print(f"  Total PnL (1 trade/candle at $100): ${total_pnl:,.2f} over {hours:.1f}h")
    print(f"  Annualized (if 24/7)     : ${total_pnl * (24/hours) * 365:,.0f}/yr")

conn.close()
print(f"\n{'='*65}")
