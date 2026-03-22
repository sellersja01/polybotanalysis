"""
Limit Order Backtest

Instead of buying at the ASK (market order), we simulate posting LIMIT BIDS.
A limit bid at price X fills when the market ASK drops to X or below.

Strategy:
  - Post a standing bid on BOTH sides from candle open
  - Up bid at up_bid_price, Down bid at dn_bid_price
  - Each side refills with a cooldown (buy_interval seconds)
  - Only count as a "real trade" if BOTH sides got at least one fill
  - Combined bid = up_bid + dn_bid < 1.0 => guaranteed edge if both fill

Key question: at what bid levels do both sides fill often enough to be profitable?
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}
INTERVALS = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900, 'ETH_15m': 900}

SHARES = 100


def run(label, db_path, up_bid, dn_bid, buy_interval=10):
    interval = INTERVALS[label]

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, mid_id, out, ask, mid in rows:
        cs = (int(float(ts)) // interval) * interval
        candles[(cs, mid_id)][out].append((float(ts), float(ask), float(mid)))

    results = []

    for (cs, mid_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        final_mid = up_ticks[-1][2]
        if   final_mid >= 0.85: winner = 'Up'
        elif final_mid <= 0.15: winner = 'Down'
        else:                   continue

        # Simulate limit bids: fill when ask <= bid price
        up_fills, dn_fills = [], []
        last_up_buy = last_dn_buy = -999

        all_ticks = (
            [(ts, 'Up',   ask) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask) for ts, ask, mid in dn_ticks]
        )
        all_ticks.sort()

        for ts, side, ask in all_ticks:
            if side == 'Up' and ask <= up_bid and ts - last_up_buy >= buy_interval:
                up_fills.append(ask)
                last_up_buy = ts
            elif side == 'Down' and ask <= dn_bid and ts - last_dn_buy >= buy_interval:
                dn_fills.append(ask)
                last_dn_buy = ts

        # Single-sided: one bid never filled — hold to resolution (realistic worst case)
        if not up_fills or not dn_fills:
            if up_fills:
                sh = len(up_fills) * SHARES
                cost = sum(up_fills) * SHARES
                pnl = (1.0 * sh - cost) if winner == 'Up' else (0.0 * sh - cost)
            elif dn_fills:
                sh = len(dn_fills) * SHARES
                cost = sum(dn_fills) * SHARES
                pnl = (1.0 * sh - cost) if winner == 'Down' else (0.0 * sh - cost)
            else:
                pnl, cost = 0, 0
            results.append({
                'winner': winner, 'pnl': pnl, 'cost': cost,
                'avg_up': None, 'avg_dn': None, 'combined': None,
                'both': False, 'win': pnl > 0, 'filled': bool(up_fills or dn_fills),
            })
            continue

        up_sh  = len(up_fills) * SHARES
        dn_sh  = len(dn_fills) * SHARES
        up_c   = sum(up_fills) * SHARES
        dn_c   = sum(dn_fills) * SHARES

        if winner == 'Up':
            pnl = (1.0 * up_sh - up_c) + (0.0 * dn_sh - dn_c)
        else:
            pnl = (0.0 * up_sh - up_c) + (1.0 * dn_sh - dn_c)

        avg_up   = up_c / up_sh
        avg_dn   = dn_c / dn_sh
        combined = avg_up + avg_dn

        results.append({
            'winner': winner, 'pnl': pnl, 'cost': up_c + dn_c,
            'avg_up': avg_up, 'avg_dn': avg_dn, 'combined': combined,
            'both': True, 'win': pnl > 0, 'filled': True,
        })

    if not results:
        return None

    total_candles = len(results)
    both_sided = [r for r in results if r['both']]
    one_sided  = [r for r in results if r['filled'] and not r['both']]
    all_filled = [r for r in results if r['filled']]

    n_both  = len(both_sided)
    n_one   = len(one_sided)
    n_total = len(all_filled)

    net      = sum(r['pnl'] for r in all_filled)
    cost     = sum(r['cost'] for r in all_filled)
    wins     = sum(1 for r in all_filled if r['win'])
    combs    = [r['combined'] for r in both_sided]
    avg_comb = sum(combs) / len(combs) if combs else None

    return {
        'total':         total_candles,
        'n_both':        n_both,
        'n_one':         n_one,
        'both_pct':      100 * n_both / total_candles,
        'one_pct':       100 * n_one  / total_candles,
        'wins':          wins,
        'wr':            100 * wins / n_total if n_total else 0,
        'net':           net,
        'cost':          cost,
        'roi':           100 * net / cost if cost else 0,
        'avg_combined':  avg_comb,
        'pnl_per_total': net / total_candles,
    }


# ── Grid search over bid levels ───────────────────────────────────────────────
BID_PAIRS = []

# Symmetric bids
for b in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45]:
    BID_PAIRS.append((b, b, f'{b:.2f}/{b:.2f}'))

# Asymmetric: test (lo, hi) pairs where lo+hi < 1.0
for hi in [0.45, 0.50, 0.55, 0.60]:
    for lo in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        if lo < hi and lo + hi < 1.0:
            BID_PAIRS.append((lo, hi, f'{lo:.2f}/{hi:.2f}'))

INTERVALS_TEST = [5, 10, 20]

print(f"\n{'='*115}")
print(f"  LIMIT ORDER BACKTEST (HONEST) — single-sided candles held to resolution")
print(f"{'='*115}")
print(f"  {'Bids(U/D)':>10} {'Int':>4} {'TotalC':>7} {'Both%':>6} {'One%':>5} "
      f"{'WR%':>6} {'AvgComb':>8} {'NetPnL':>10} {'ROI%':>7} {'$/allC':>8}")
print(f"  {'-'*90}")

best_roi = -999
best_cfg = None

seen = set()
for up_bid, dn_bid, blabel in BID_PAIRS:
    key = (min(up_bid, dn_bid), max(up_bid, dn_bid))
    if key in seen:
        continue
    seen.add(key)

    for iv in INTERVALS_TEST:
        total_total = total_both = total_one = total_wins = 0
        total_net = total_cost = 0
        combs_all = []

        for label, db in DBS.items():
            r = run(label, db, up_bid, dn_bid, iv)
            if not r:
                continue
            total_total += r['total']
            total_both  += r['n_both']
            total_one   += r['n_one']
            total_wins  += r['wins']
            total_net   += r['net']
            total_cost  += r['cost']
            if r['avg_combined']:
                combs_all.append(r['avg_combined'])

        if not total_total:
            continue

        n_filled = total_both + total_one
        both_pct = 100 * total_both / total_total
        one_pct  = 100 * total_one  / total_total
        wr       = 100 * total_wins / n_filled if n_filled else 0
        roi      = 100 * total_net / total_cost if total_cost else 0
        avg_comb = sum(combs_all) / len(combs_all) if combs_all else 0
        ppc      = total_net / total_total

        flag = ' <--' if roi > best_roi and total_both >= 30 else ''
        if roi > best_roi and total_both >= 30:
            best_roi = roi
            best_cfg = (up_bid, dn_bid, iv, blabel)

        print(f"  {blabel:>10} {iv:>4}s {total_total:>7} {both_pct:>5.1f}% {one_pct:>4.1f}% "
              f"{wr:>6.1f} {avg_comb:>8.4f} {total_net:>+10.2f} {roi:>7.1f}{flag} {ppc:>+7.2f}")
    print()

# ── Per-market breakdown for best config ──────────────────────────────────────
if best_cfg:
    up_bid, dn_bid, iv, blabel = best_cfg
    print(f"\n  === Best config: bids={blabel}, interval={iv}s ===")
    print(f"  {'Market':>10} {'TotalC':>7} {'Both%':>6} {'One%':>5} {'WR%':>6} "
          f"{'AvgComb':>8} {'NetPnL':>10} {'ROI%':>7} {'$/allC':>8}")
    print(f"  {'-'*75}")
    for label, db in DBS.items():
        r = run(label, db, up_bid, dn_bid, iv)
        if r:
            print(f"  {label:>10} {r['total']:>7} {r['both_pct']:>5.1f}% {r['one_pct']:>4.1f}% "
                  f"{r['wr']:>6.1f} {r['avg_combined'] or 0:>8.4f} "
                  f"{r['net']:>+10.2f} {r['roi']:>7.1f} {r['pnl_per_total']:>+7.2f}")
