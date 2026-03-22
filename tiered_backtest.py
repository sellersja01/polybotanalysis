"""
Tiered Buy Backtest — mimics wallet_2's pattern.

Rules:
  - Buy whichever side is below cap (0.50) using a cooldown interval
  - Buy MORE shares at cheaper prices:
      price < 0.15 -> 4x shares
      price < 0.30 -> 2x shares
      price < cap  -> 1x shares
  - Both sides can accumulate during a candle as the market oscillates
  - Combined avg < 1.0 => profit regardless of winner

Key insight from wallet_2 analysis:
  They achieve combined avg 0.83-0.97 by loading up on the cheap side
  (< 0.15) while also having early fills on the eventual winner.
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

BASE_SHARES = 50


def tier_shares(price, base, t1=0.15, t2=0.30, mult1=4, mult2=2):
    if price < t1:
        return base * mult1
    if price < t2:
        return base * mult2
    return base


def run(label, db_path, cap, buy_interval, t1=0.15, t2=0.30, mult1=4, mult2=2):
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

        up_fills, dn_fills = [], []
        last_up_buy = last_dn_buy = -999

        all_ticks = (
            [(ts, 'Up',   ask) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask) for ts, ask, mid in dn_ticks]
        )
        all_ticks.sort()

        for ts, side, ask in all_ticks:
            if ask > cap:
                continue
            sh = tier_shares(ask, BASE_SHARES, t1, t2, mult1, mult2)
            if side == 'Up' and ts - last_up_buy >= buy_interval:
                up_fills.append((ask, sh))
                last_up_buy = ts
            elif side == 'Down' and ts - last_dn_buy >= buy_interval:
                dn_fills.append((ask, sh))
                last_dn_buy = ts

        if not up_fills and not dn_fills:
            continue

        up_sh  = sum(s for _, s in up_fills)
        dn_sh  = sum(s for _, s in dn_fills)
        up_c   = sum(p * s for p, s in up_fills)
        dn_c   = sum(p * s for p, s in dn_fills)

        if winner == 'Up':
            pnl = (1.0 * up_sh - up_c) + (0.0 * dn_sh - dn_c)
        else:
            pnl = (0.0 * up_sh - up_c) + (1.0 * dn_sh - dn_c)

        avg_up  = (up_c / up_sh)  if up_sh else 0
        avg_dn  = (dn_c / dn_sh)  if dn_sh else 0
        both    = up_sh > 0 and dn_sh > 0
        combined = avg_up + avg_dn if both else None

        results.append({
            'winner':   winner,
            'pnl':      pnl,
            'cost':     up_c + dn_c,
            'avg_up':   avg_up,
            'avg_dn':   avg_dn,
            'combined': combined,
            'n_up':     len(up_fills),
            'n_dn':     len(dn_fills),
            'both':     both,
            'win':      pnl > 0,
        })

    if not results:
        return None

    n       = len(results)
    wins    = sum(1 for r in results if r['win'])
    both_n  = sum(1 for r in results if r['both'])
    net     = sum(r['pnl'] for r in results)
    cost    = sum(r['cost'] for r in results)
    combs   = [r['combined'] for r in results if r['combined'] is not None]
    avg_combined = sum(combs) / len(combs) if combs else None

    return {
        'n': n, 'wins': wins, 'both': both_n,
        'wr':       100 * wins / n,
        'both_pct': 100 * both_n / n,
        'net': net, 'cost': cost,
        'roi':      100 * net / cost if cost else 0,
        'avg_combined': avg_combined,
        'pnl_per_candle': net / n,
    }


# ── Grid search ───────────────────────────────────────────────────────────────
CAPS      = [0.40, 0.45, 0.50, 0.55]
INTERVALS_TEST = [5, 10, 20]
TIER_CONFIGS = [
    # (t1,   t2,   mult1, mult2, label)
    (0.15,  0.30,  1,     1,    'flat'),     # no tiering, just price cap
    (0.15,  0.30,  2,     1,    '2x@0.15'),  # 2x only at very cheap
    (0.15,  0.30,  4,     2,    '4x@0.15'),  # 4x at very cheap, 2x at cheap
    (0.10,  0.25,  4,     2,    '4x@0.10'),  # same but tighter thresholds
]

print(f"\n{'='*100}")
print(f"  TIERED BUY BACKTEST — Base {BASE_SHARES} shares, ALL candles")
print(f"{'='*100}")
print(f"  {'Tier':>10} {'Cap':>5} {'Int':>4} {'Candles':>8} {'WR%':>6} "
      f"{'Both%':>7} {'AvgComb':>8} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
print(f"  {'-'*80}")

best_roi = -999
best_cfg = None

for t1, t2, mult1, mult2, tlabel in TIER_CONFIGS:
    for cap in CAPS:
        for iv in INTERVALS_TEST:
            total_n = total_wins = total_both = 0
            total_net = total_cost = 0
            combs_all = []

            for label, db in DBS.items():
                r = run(label, db, cap, iv, t1, t2, mult1, mult2)
                if not r:
                    continue
                total_n    += r['n']
                total_wins += r['wins']
                total_both += r['both']
                total_net  += r['net']
                total_cost += r['cost']
                if r['avg_combined']:
                    combs_all.append(r['avg_combined'])

            if not total_n:
                continue

            wr       = 100 * total_wins / total_n
            both_pct = 100 * total_both / total_n
            roi      = 100 * total_net / total_cost if total_cost else 0
            avg_comb = sum(combs_all) / len(combs_all) if combs_all else 0
            ppc      = total_net / total_n

            flag = ' <--' if roi > best_roi else ''
            if roi > best_roi:
                best_roi = roi
                best_cfg = (t1, t2, mult1, mult2, tlabel, cap, iv)

            print(f"  {tlabel:>10} {cap:>5.2f} {iv:>4}s {total_n:>8} {wr:>6.1f} "
                  f"{both_pct:>7.1f} {avg_comb:>8.4f} {total_net:>+10.2f} {roi:>7.1f}{flag} "
                  f"{ppc:>+8.2f}")
    print()

# ── Best config detailed breakdown ────────────────────────────────────────────
if best_cfg:
    t1, t2, mult1, mult2, tlabel, cap, iv = best_cfg
    print(f"\n  === Best config: tier={tlabel}, cap={cap}, interval={iv}s — per market ===")
    print(f"  {'Market':>10} {'Candles':>8} {'WR%':>6} {'Both%':>7} "
          f"{'AvgComb':>8} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
    print(f"  {'-'*70}")
    for label, db in DBS.items():
        r = run(label, db, cap, iv, t1, t2, mult1, mult2)
        if r:
            print(f"  {label:>10} {r['n']:>8} {r['wr']:>6.1f} {r['both_pct']:>7.1f} "
                  f"{r['avg_combined'] or 0:>8.4f} {r['net']:>+10.2f} {r['roi']:>7.1f} "
                  f"{r['pnl_per_candle']:>+8.2f}")

# ── Combined avg distribution for best config ────────────────────────────────
if best_cfg:
    t1, t2, mult1, mult2, tlabel, cap, iv = best_cfg
    print(f"\n  === Combined avg distribution (BTC_5m, best config) ===")
    interval = 300
    conn = sqlite3.connect(DBS['BTC_5m'])
    rows = conn.execute(
        'SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, mid_id, out, ask, mid in rows:
        cs = (int(float(ts)) // interval) * interval
        candles[(cs, mid_id)][out].append((float(ts), float(ask), float(mid)))

    combs = []
    for (cs, mid_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks: continue
        final_mid = up_ticks[-1][2]
        if final_mid < 0.85 and final_mid > 0.15: continue

        up_fills, dn_fills = [], []
        last_up = last_dn = -999
        for ts, side, ask in sorted(
            [(ts, 'Up', ask) for ts, ask, m in up_ticks] +
            [(ts, 'Down', ask) for ts, ask, m in dn_ticks]
        ):
            if ask > cap: continue
            sh = tier_shares(ask, BASE_SHARES, t1, t2, mult1, mult2)
            if side == 'Up' and ts - last_up >= iv:
                up_fills.append((ask, sh)); last_up = ts
            elif side == 'Down' and ts - last_dn >= iv:
                dn_fills.append((ask, sh)); last_dn = ts

        if not up_fills or not dn_fills: continue
        avg_up = sum(p*s for p,s in up_fills) / sum(s for _,s in up_fills)
        avg_dn = sum(p*s for p,s in dn_fills) / sum(s for _,s in dn_fills)
        combs.append(avg_up + avg_dn)

    buckets = [(0.5,0.7),(0.7,0.8),(0.8,0.85),(0.85,0.9),(0.9,0.95),(0.95,1.0),(1.0,1.1),(1.1,1.5)]
    print(f"  {'Combined':>12} {'N':>5} {'%':>6}")
    print(f"  {'-'*26}")
    for lo, hi in buckets:
        n = sum(1 for c in combs if lo <= c < hi)
        pct = 100 * n / len(combs) if combs else 0
        print(f"  {lo:.2f}-{hi:.2f}      {n:>5} {pct:>5.1f}%")
    print(f"  Total both-sided: {len(combs)}")
    print(f"  Avg combined: {sum(combs)/len(combs):.4f}" if combs else "  No data")
    print(f"  % profitable (comb < 1.0): {100*sum(1 for c in combs if c < 1.0)/len(combs):.1f}%" if combs else "")
