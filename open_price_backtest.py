"""
Open Price + Cheap Accumulation Backtest

Mimics wallet_2's actual observed pattern:
  1. At candle open, buy BOTH sides immediately at whatever price they are
  2. Continue buying whichever side gets cheap (below accum_cap) with cooldown
  3. Both sides always fill from step 1 -> no single-sided exposure

The open buy ensures we're never stuck one-sided.
The cheap accumulation pulls the combined average down for extra edge.

Grid search over:
  - open_shares: how many shares to buy at open on each side
  - accum_cap:   price threshold to trigger additional buys
  - accum_shares: shares per additional buy
  - buy_interval: cooldown between additional buys (seconds)
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


def run(label, db_path, open_shares, accum_cap, accum_shares, buy_interval):
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

        # Step 1: open buy on both sides at first available tick
        open_up = up_ticks[0][1]   # first Up ask
        open_dn = dn_ticks[0][1]   # first Down ask

        up_fills  = [(open_up, open_shares)]
        dn_fills  = [(open_dn, open_shares)]

        # Step 2: accumulate more on whichever side gets cheap
        last_up_buy = up_ticks[0][0]
        last_dn_buy = dn_ticks[0][0]

        all_ticks = (
            [(ts, 'Up',   ask) for ts, ask, mid in up_ticks[1:]] +
            [(ts, 'Down', ask) for ts, ask, mid in dn_ticks[1:]]
        )
        all_ticks.sort()

        for ts, side, ask in all_ticks:
            if ask > accum_cap:
                continue
            if side == 'Up' and ts - last_up_buy >= buy_interval:
                up_fills.append((ask, accum_shares))
                last_up_buy = ts
            elif side == 'Down' and ts - last_dn_buy >= buy_interval:
                dn_fills.append((ask, accum_shares))
                last_dn_buy = ts

        up_sh  = sum(s for _, s in up_fills)
        dn_sh  = sum(s for _, s in dn_fills)
        up_c   = sum(p * s for p, s in up_fills)
        dn_c   = sum(p * s for p, s in dn_fills)

        if winner == 'Up':
            pnl = (1.0 * up_sh - up_c) + (0.0 * dn_sh - dn_c)
        else:
            pnl = (0.0 * up_sh - up_c) + (1.0 * dn_sh - dn_c)

        avg_up   = up_c / up_sh
        avg_dn   = dn_c / dn_sh
        combined = avg_up + avg_dn

        results.append({
            'pnl':      pnl,
            'cost':     up_c + dn_c,
            'avg_up':   avg_up,
            'avg_dn':   avg_dn,
            'combined': combined,
            'win':      pnl > 0,
            'n_up':     len(up_fills),
            'n_dn':     len(dn_fills),
        })

    if not results:
        return None

    n    = len(results)
    wins = sum(1 for r in results if r['win'])
    net  = sum(r['pnl'] for r in results)
    cost = sum(r['cost'] for r in results)
    combs = [r['combined'] for r in results]

    return {
        'n':            n,
        'wins':         wins,
        'wr':           100 * wins / n,
        'net':          net,
        'cost':         cost,
        'roi':          100 * net / cost if cost else 0,
        'avg_combined': sum(combs) / len(combs),
        'pnl_per_candle': net / n,
    }


# ── Grid search ───────────────────────────────────────────────────────────────
OPEN_SHARES   = [50, 100, 200]
ACCUM_CAPS    = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
ACCUM_SHARES  = [50, 100, 200]
BUY_INTERVALS = [5, 10, 20]

print(f"\n{'='*100}")
print(f"  OPEN-PRICE + CHEAP ACCUMULATION BACKTEST")
print(f"  Buy both sides at open, then accumulate more below accum_cap")
print(f"{'='*100}")
print(f"  {'OpenSh':>7} {'Cap':>5} {'AccSh':>6} {'Int':>4} {'Candles':>8} "
      f"{'WR%':>6} {'AvgComb':>8} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
print(f"  {'-'*80}")

best_roi = -999
best_cfg = None

for open_sh in OPEN_SHARES:
    for accum_cap in ACCUM_CAPS:
        for accum_sh in ACCUM_SHARES:
            for iv in BUY_INTERVALS:
                total_n = total_wins = 0
                total_net = total_cost = 0
                combs_all = []

                for label, db in DBS.items():
                    r = run(label, db, open_sh, accum_cap, accum_sh, iv)
                    if not r:
                        continue
                    total_n    += r['n']
                    total_wins += r['wins']
                    total_net  += r['net']
                    total_cost += r['cost']
                    combs_all.append(r['avg_combined'])

                if not total_n:
                    continue

                wr       = 100 * total_wins / total_n
                roi      = 100 * total_net / total_cost if total_cost else 0
                avg_comb = sum(combs_all) / len(combs_all)
                ppc      = total_net / total_n

                flag = ' <--' if roi > best_roi else ''
                if roi > best_roi:
                    best_roi = roi
                    best_cfg = (open_sh, accum_cap, accum_sh, iv)

                print(f"  {open_sh:>7} {accum_cap:>5.2f} {accum_sh:>6} {iv:>4}s {total_n:>8} "
                      f"{wr:>6.1f} {avg_comb:>8.4f} {total_net:>+10.2f} {roi:>7.1f}{flag} "
                      f"{ppc:>+8.2f}")
        print()

# ── Best config per market ────────────────────────────────────────────────────
if best_cfg:
    open_sh, accum_cap, accum_sh, iv = best_cfg
    print(f"\n  === Best config: open={open_sh}sh, accum_cap={accum_cap}, "
          f"accum={accum_sh}sh, interval={iv}s ===")
    print(f"  {'Market':>10} {'Candles':>8} {'WR%':>6} {'AvgComb':>8} "
          f"{'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
    print(f"  {'-'*62}")
    for label, db in DBS.items():
        r = run(label, db, open_sh, accum_cap, accum_sh, iv)
        if r:
            print(f"  {label:>10} {r['n']:>8} {r['wr']:>6.1f} {r['avg_combined']:>8.4f} "
                  f"{r['net']:>+10.2f} {r['roi']:>7.1f} {r['pnl_per_candle']:>+8.2f}")

# ── Combined avg distribution for best config (BTC_5m) ───────────────────────
if best_cfg:
    open_sh, accum_cap, accum_sh, iv = best_cfg
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

        up_f = [(up_ticks[0][1], open_sh)]
        dn_f = [(dn_ticks[0][1], open_sh)]
        last_up = up_ticks[0][0]
        last_dn = dn_ticks[0][0]
        for ts, side, ask in sorted(
            [(ts, 'Up', ask) for ts, ask, m in up_ticks[1:]] +
            [(ts, 'Down', ask) for ts, ask, m in dn_ticks[1:]]
        ):
            if ask > accum_cap: continue
            if side == 'Up' and ts - last_up >= iv:
                up_f.append((ask, accum_sh)); last_up = ts
            elif side == 'Down' and ts - last_dn >= iv:
                dn_f.append((ask, accum_sh)); last_dn = ts

        avg_up = sum(p*s for p,s in up_f) / sum(s for _,s in up_f)
        avg_dn = sum(p*s for p,s in dn_f) / sum(s for _,s in dn_f)
        combs.append(avg_up + avg_dn)

    print(f"\n  === Combined avg distribution (BTC_5m, best config) ===")
    buckets = [(0,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.9),(0.9,1.0),(1.0,1.1),(1.1,1.3),(1.3,2.0)]
    print(f"  {'Combined':>10} {'N':>5} {'%':>6}")
    for lo, hi in buckets:
        n = sum(1 for c in combs if lo <= c < hi)
        print(f"  {lo:.1f}-{hi:.1f}    {n:>5} {100*n/len(combs):>5.1f}%")
    print(f"  Avg combined: {sum(combs)/len(combs):.4f}")
    print(f"  % profitable (comb < 1.0): {100*sum(1 for c in combs if c<1.0)/len(combs):.1f}%")
