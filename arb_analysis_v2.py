"""
arb_analysis_v2.py — Arb analysis with exact fees + execution simulation
=========================================================================
Three scenarios:
  A) Taker on both platforms (current fees)
  B) Taker Poly + Maker Kalshi (0% Kalshi fee)
  C) Taker on both with NEW Poly fees (March 30 change)
  D) Taker new Poly + Maker Kalshi

Also simulates realistic execution: 1 trade per candle at the best opportunity,
with different capital levels.
"""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB = 'databases/arb_collector.db'
conn = sqlite3.connect(DB)

# ── Fee functions ─────────────────────────────────────────────────────────────
def poly_fee_current(price):
    """Current Poly taker fee (per $1 contract)"""
    return price * 0.25 * (price * (1 - price)) ** 2

def poly_fee_new(price):
    """New Poly taker fee from March 30 (per $1 contract)"""
    return price * 0.072 * (price * (1 - price)) ** 1

def poly_fee_maker_current(price):
    """Current Poly maker fee (80% of taker = 20% rebate)"""
    return poly_fee_current(price) * 0.80

def poly_fee_maker_new(price):
    """New Poly maker fee (80% of taker)"""
    return poly_fee_new(price) * 0.80

def kalshi_fee_taker(price):
    """Kalshi taker fee (per $1 contract)"""
    return 0.07 * price * (1 - price)

def kalshi_fee_maker(price):
    """Kalshi maker fee = $0"""
    return 0.0

# ── Print fee comparison table ────────────────────────────────────────────────
print(f"{'='*75}")
print(f"  FEE COMPARISON TABLE (per $1 contract)")
print(f"{'='*75}")
print(f"  {'Price':>5}  {'Poly Now':>9}  {'Poly Mar30':>11}  {'Kalshi Tkr':>11}  {'Kalshi Mkr':>11}")
print(f"  {'':>5}  {'(taker)':>9}  {'(taker)':>11}  {'':>11}  {'':>11}")
print(f"  {'-'*60}")
for p in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
          0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
    pf = poly_fee_current(p)
    pfn = poly_fee_new(p)
    kf = kalshi_fee_taker(p)
    print(f"  {p:>5.2f}  ${pf:>7.4f}    ${pfn:>7.4f}      ${kf:>7.4f}      $0.0000")

# Combined fees at p=0.50 for each scenario
print(f"\n  Combined fees at p=0.50 (both legs):")
print(f"    A) Taker+Taker (now)        : ${poly_fee_current(0.5) + kalshi_fee_taker(0.5):.4f}  ({(poly_fee_current(0.5) + kalshi_fee_taker(0.5))*100:.2f}c)")
print(f"    B) Poly Taker + Kalshi Maker: ${poly_fee_current(0.5) + 0:.4f}  ({poly_fee_current(0.5)*100:.2f}c)")
print(f"    C) Taker+Taker (Mar 30)     : ${poly_fee_new(0.5) + kalshi_fee_taker(0.5):.4f}  ({(poly_fee_new(0.5) + kalshi_fee_taker(0.5))*100:.2f}c)")
print(f"    D) New Poly Tkr + Kalshi Mkr: ${poly_fee_new(0.5) + 0:.4f}  ({poly_fee_new(0.5)*100:.2f}c)")

# ── Load data ─────────────────────────────────────────────────────────────────
rows = conn.execute("""
    SELECT ts, asset, candle_id,
           p_up_bid, p_up_ask, p_dn_bid, p_dn_ask,
           k_up_bid, k_up_ask, k_dn_bid, k_dn_ask
    FROM snapshots
    WHERE p_up_bid > 0 AND p_up_ask > 0 AND p_dn_bid > 0 AND p_dn_ask > 0
      AND k_up_bid > 0 AND k_up_ask > 0 AND k_dn_bid > 0 AND k_dn_ask > 0
      AND p_up_ask < 0.95 AND p_dn_ask < 0.95
      AND k_up_ask < 0.95 AND k_dn_ask < 0.95
    ORDER BY asset, candle_id, ts
""").fetchall()

total_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
r = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
hours = (r[1] - r[0]) / 3600
outcomes = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
conn.close()

print(f"\n{'='*75}")
print(f"  DATA SUMMARY")
print(f"{'='*75}")
print(f"  Total rows: {total_rows:,} | Active: {len(rows):,} | Hours: {hours:.1f} | Outcomes: {outcomes}")

# ── Compute net gaps for all 4 scenarios ──────────────────────────────────────
def compute_gaps(rows, poly_fee_fn, kalshi_fee_fn):
    """For each row, compute best net gap across both directions."""
    results = []
    for ts, asset, candle, pub, pua, pdb, pda, kub, kua, kdb, kda in rows:
        # Dir A: buy Poly Up (taker) + buy Kalshi Down
        cost_a = pua + kda
        fee_a = poly_fee_fn(pua) + kalshi_fee_fn(kda)
        net_a = 1.0 - cost_a - fee_a

        # Dir B: buy Poly Down (taker) + buy Kalshi Up
        cost_b = pda + kua
        fee_b = poly_fee_fn(pda) + kalshi_fee_fn(kua)
        net_b = 1.0 - cost_b - fee_b

        best = max(net_a, net_b)
        best_dir = 'A' if net_a >= net_b else 'B'
        best_cost = cost_a if best_dir == 'A' else cost_b
        best_fee = fee_a if best_dir == 'A' else fee_b

        results.append({
            'ts': ts, 'asset': asset, 'candle': candle,
            'best_net': best, 'best_dir': best_dir,
            'best_cost': best_cost, 'best_fee': best_fee,
        })
    return results

scenarios = {
    'A) Taker+Taker (now)':         (poly_fee_current, kalshi_fee_taker),
    'B) Poly Tkr + Kalshi Mkr':     (poly_fee_current, kalshi_fee_maker),
    'C) Taker+Taker (Mar 30)':      (poly_fee_new,     kalshi_fee_taker),
    'D) New Poly Tkr + Kalshi Mkr': (poly_fee_new,     kalshi_fee_maker),
}

all_results = {}
for name, (pf, kf) in scenarios.items():
    all_results[name] = compute_gaps(rows, pf, kf)

# ── Summary for each scenario ────────────────────────────────────────────────
def avg(lst): return sum(lst)/len(lst) if lst else 0

print(f"\n{'='*75}")
print(f"  SCENARIO COMPARISON — TICK-LEVEL")
print(f"{'='*75}")
print(f"  {'Scenario':<35} {'Prof%':>6} {'AvgNet':>8} {'MaxNet':>8} {'AvgFee':>8}")
print(f"  {'-'*68}")
for name, res in all_results.items():
    prof = [r for r in res if r['best_net'] > 0]
    pct = len(prof)/len(res)*100 if res else 0
    an = avg([r['best_net'] for r in prof]) if prof else 0
    mn = max([r['best_net'] for r in prof]) if prof else 0
    af = avg([r['best_fee'] for r in res])
    print(f"  {name:<35} {pct:>5.1f}% {an:>+.4f} {mn:>+.4f} {af:>.4f}")

# ── Per-asset per-scenario ────────────────────────────────────────────────────
print(f"\n{'='*75}")
print(f"  PER-ASSET BREAKDOWN")
print(f"{'='*75}")
for name, res in all_results.items():
    print(f"\n  --- {name} ---")
    print(f"  {'Asset':<6} {'Ticks':>7} {'Prof%':>6} {'AvgNet':>8} {'MaxNet':>8}")
    print(f"  {'-'*40}")
    for asset in sorted(set(r['asset'] for r in res)):
        ar = [r for r in res if r['asset'] == asset]
        pr = [r for r in ar if r['best_net'] > 0]
        pct = len(pr)/len(ar)*100 if ar else 0
        an = avg([r['best_net'] for r in pr]) if pr else 0
        mn = max([r['best_net'] for r in pr]) if pr else 0
        print(f"  {asset:<6} {len(ar):>7,} {pct:>5.1f}% {an:>+.4f} {mn:>+.4f}")

# ── Execution simulation ─────────────────────────────────────────────────────
print(f"\n{'='*75}")
print(f"  EXECUTION SIMULATION")
print(f"{'='*75}")
print(f"  Rules:")
print(f"    - 1 trade per candle per asset (take the BEST net gap in the candle)")
print(f"    - Only execute if net gap > min threshold")
print(f"    - Capital levels: $100, $500, $1000 per trade")
print(f"    - Assume fills at displayed ask prices")

for name, res in all_results.items():
    print(f"\n  === {name} ===")

    # Group by (asset, candle) and find best opportunity
    candle_best = defaultdict(lambda: {'best_net': -999, 'best_cost': 0})
    for r in res:
        key = (r['asset'], r['candle'])
        if r['best_net'] > candle_best[key]['best_net']:
            candle_best[key] = r

    for min_gap in [0.00, 0.01, 0.02, 0.03, 0.05]:
        tradeable = {k: v for k, v in candle_best.items() if v['best_net'] >= min_gap}
        n_candles = len(candle_best)
        n_traded = len(tradeable)

        if not tradeable:
            print(f"    min_gap={min_gap:.2f}: 0 trades")
            continue

        nets = [v['best_net'] for v in tradeable.values()]
        costs = [v['best_cost'] for v in tradeable.values()]
        avg_net = avg(nets)
        total_net = sum(nets)

        # PnL at different capital levels
        # profit per trade = contracts * net_gap
        # contracts = capital / cost_per_contract (cost = poly_ask + kalshi_ask)
        print(f"    min_gap>={min_gap:.2f}c | {n_traded}/{n_candles} candles ({n_traded/n_candles*100:.0f}%) | avg_net={avg_net:.4f}")
        for capital in [100, 500, 1000]:
            total_pnl = 0
            for v in tradeable.values():
                contracts = capital / v['best_cost'] if v['best_cost'] > 0 else 0
                pnl = contracts * v['best_net']
                total_pnl += pnl
            per_hour = total_pnl / hours
            per_day = per_hour * 24
            print(f"      ${capital:>5}/trade: ${total_pnl:>8,.2f} over {hours:.1f}h = ${per_hour:>7,.2f}/hr = ${per_day:>9,.2f}/day")

# ── Gap persistence for maker scenario ────────────────────────────────────────
print(f"\n{'='*75}")
print(f"  GAP PERSISTENCE — How long do gaps stay open?")
print(f"  (Scenario B: Poly Taker + Kalshi Maker)")
print(f"{'='*75}")

res_b = all_results['B) Poly Tkr + Kalshi Mkr']

for min_gap in [0.01, 0.02, 0.03, 0.05]:
    streaks = []
    for asset in sorted(set(r['asset'] for r in res_b)):
        ar = sorted([r for r in res_b if r['asset'] == asset], key=lambda x: x['ts'])
        in_streak = False
        s_start = 0
        s_nets = []
        prev_ts = 0
        for r in ar:
            if r['best_net'] >= min_gap:
                if not in_streak:
                    s_start = r['ts']
                    s_nets = []
                    in_streak = True
                s_nets.append(r['best_net'])
                prev_ts = r['ts']
            else:
                if in_streak:
                    dur = prev_ts - s_start
                    streaks.append({'asset': asset, 'dur': dur, 'avg': avg(s_nets),
                                    'max': max(s_nets), 'n': len(s_nets)})
                    in_streak = False
        if in_streak:
            streaks.append({'asset': asset, 'dur': prev_ts - s_start, 'avg': avg(s_nets),
                            'max': max(s_nets), 'n': len(s_nets)})

    if not streaks:
        print(f"\n  gap >= {min_gap:.2f}: No streaks")
        continue

    durs = sorted([s['dur'] for s in streaks])
    print(f"\n  gap >= {min_gap:.2f}c: {len(streaks)} windows")
    print(f"    Avg duration : {avg(durs):>7.1f}s")
    print(f"    Median       : {durs[len(durs)//2]:>7.1f}s")
    print(f"    P75          : {durs[int(len(durs)*0.75)]:>7.1f}s")
    print(f"    P90          : {durs[int(len(durs)*0.90)]:>7.1f}s")
    print(f"    Max          : {durs[-1]:>7.1f}s")
    print(f"    >= 5s        : {sum(1 for d in durs if d >= 5):>5} ({sum(1 for d in durs if d >= 5)/len(durs)*100:.0f}%)")
    print(f"    >= 30s       : {sum(1 for d in durs if d >= 30):>5} ({sum(1 for d in durs if d >= 30)/len(durs)*100:.0f}%)")
    print(f"    >= 60s       : {sum(1 for d in durs if d >= 60):>5} ({sum(1 for d in durs if d >= 60)/len(durs)*100:.0f}%)")

print(f"\n{'='*75}")
