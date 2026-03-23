"""
Exit Threshold Backtest

Tests different early exit thresholds (0.05 to 0.20) against the
wait-for-divergence single 0.25 entry and all-5-levels strategies.

Key question: does lowering the exit threshold from 0.20 reduce
accidental winner exits enough to improve overall ROI?

100% of candles, real fees, winner = highest mid at last tick.
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}
INTERVALS  = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900, 'ETH_15m': 900}
MARKETS    = ['BTC_5m', 'BTC_15m', 'ETH_5m']   # exclude ETH_15m
SHARES     = 100
FEE_RATE   = 0.25
FEE_EXP    = 2

def calc_fee(shares, price):
    return shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXP

def run(label, db_path, entry_levels, exit_mid):
    interval = INTERVALS[label]
    conn     = sqlite3.connect(db_path)
    rows     = conn.execute(
        'SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, mid_id, out, ask, mid in rows:
        cs = (int(float(ts)) // interval) * interval
        candles[(cs, mid_id)][out].append((float(ts), float(ask), float(mid)))

    results       = []
    winner_exits  = 0
    loser_exits   = 0

    for (cs, mid_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        final_mid = up_ticks[-1][2]
        winner    = 'Up' if final_mid >= 0.5 else 'Down'

        all_ticks = sorted(
            [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
        )

        levels_triggered = set()
        up_entries  = []
        dn_entries  = []
        up_exit_bid = None
        dn_exit_bid = None
        last_up_ask = None
        last_dn_ask = None

        for ts, side, ask, mid in all_ticks:
            if side == 'Up':   last_up_ask = ask
            else:              last_dn_ask = ask

            # Entry triggers
            if last_up_ask and last_dn_ask:
                for lvl in entry_levels:
                    if lvl not in levels_triggered and mid <= lvl:
                        levels_triggered.add(lvl)
                        up_entries.append((last_up_ask, calc_fee(SHARES, last_up_ask)))
                        dn_entries.append((last_dn_ask, calc_fee(SHARES, last_dn_ask)))

            # Exit trigger
            if levels_triggered and mid <= exit_mid:
                if side == 'Up' and up_exit_bid is None:
                    up_exit_bid = max(0.0, 2 * mid - ask)
                elif side == 'Down' and dn_exit_bid is None:
                    dn_exit_bid = max(0.0, 2 * mid - ask)

        if not up_entries or not dn_entries:
            continue

        n_entries     = len(up_entries)
        total_shares  = n_entries * SHARES
        total_up_cost = sum(a for a, f in up_entries) * SHARES
        total_dn_cost = sum(a for a, f in dn_entries) * SHARES
        total_up_fee  = sum(f for a, f in up_entries)
        total_dn_fee  = sum(f for a, f in dn_entries)

        if winner == 'Up':
            up_resolve = up_exit_bid if up_exit_bid is not None else 1.0
            dn_resolve = dn_exit_bid if dn_exit_bid is not None else 0.0
            if up_exit_bid is not None: winner_exits += 1
            if dn_exit_bid is not None: loser_exits  += 1
        else:
            dn_resolve = dn_exit_bid if dn_exit_bid is not None else 1.0
            up_resolve = up_exit_bid if up_exit_bid is not None else 0.0
            if dn_exit_bid is not None: winner_exits += 1
            if up_exit_bid is not None: loser_exits  += 1

        pnl  = (up_resolve * total_shares - total_up_cost - total_up_fee) + \
               (dn_resolve * total_shares - total_dn_cost - total_dn_fee)
        cost = total_up_cost + total_dn_cost
        results.append({'pnl': pnl, 'cost': cost, 'win': pnl > 0})

    if not results:
        return None
    n    = len(results)
    wins = sum(1 for r in results if r['win'])
    net  = sum(r['pnl'] for r in results)
    cost = sum(r['cost'] for r in results)
    return {
        'n': n, 'wins': wins,
        'wr':  100 * wins / n,
        'net': net,
        'cost': cost,
        'roi': 100 * net / cost if cost else 0,
        'ppc': net / n,
        'winner_exit_pct': 100 * winner_exits / n,
        'loser_exit_pct':  100 * loser_exits  / n,
    }


EXIT_THRESHOLDS = [0.20, 0.17, 0.15, 0.12, 0.10, 0.07, 0.05, None]
CONFIGS = [
    ('Single 0.25',  [0.25]),
    ('All 5 levels', [0.45, 0.40, 0.35, 0.30, 0.25]),
]

for cfg_name, levels in CONFIGS:
    print(f"\n{'='*90}")
    print(f"  Config: {cfg_name}")
    print(f"  {'ExitMid':>8} {'n':>6} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9} {'WinExit%':>9} {'LosExit%':>9}")
    print(f"  {'-'*80}")

    best_roi = -999
    for thresh in EXIT_THRESHOLDS:
        total_n = total_wins = 0
        total_net = total_cost = 0.0
        total_we = total_le = 0.0

        for mkt in MARKETS:
            r = run(mkt, DBS[mkt], levels, thresh if thresh is not None else -1)
            if not r: continue
            total_n    += r['n']
            total_wins += r['wins']
            total_net  += r['net']
            total_cost += r.get('cost', 0) or 0
            total_we   += r['winner_exit_pct'] * r['n'] / 100
            total_le   += r['loser_exit_pct']  * r['n'] / 100

        if not total_n: continue
        wr   = 100 * total_wins / total_n
        roi  = 100 * total_net  / total_cost if total_cost else 0
        ppc  = total_net / total_n
        we   = 100 * total_we / total_n
        le   = 100 * total_le / total_n
        flag = ' <--' if roi > best_roi else ''
        if roi > best_roi: best_roi = roi
        lbl  = f"{thresh:.2f}" if thresh is not None else "never"
        print(f"  {lbl:>8} {total_n:>6} {wr:>6.1f} {total_net:>+10.2f} {roi:>7.2f}{flag} "
              f"{ppc:>+9.2f} {we:>8.1f}% {le:>8.1f}%")
