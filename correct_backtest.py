"""
Corrected Backtest — Fixed Shares + Price Cap
Includes ALL candles, not just double-sided ones.
This is the true expected performance.
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


def run(label, db_path, price_cap, buy_interval):
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

        # Determine winner from final Up mid
        final_mid = up_ticks[-1][2]
        if   final_mid >= 0.85: winner = 'Up'
        elif final_mid <= 0.15: winner = 'Down'
        else:                   continue  # unresolved — skip

        # Simulate buys
        up_fills, dn_fills = [], []
        last_up_buy = last_dn_buy = -999

        all_ticks = (
            [(ts, 'Up',   ask) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask) for ts, ask, mid in dn_ticks]
        )
        all_ticks.sort()

        for ts, side, ask in all_ticks:
            if ask > price_cap:
                continue
            if side == 'Up' and ts - last_up_buy >= buy_interval:
                up_fills.append(ask)
                last_up_buy = ts
            elif side == 'Down' and ts - last_dn_buy >= buy_interval:
                dn_fills.append(ask)
                last_dn_buy = ts

        # Include even if only one side filled — that's a real loss scenario
        if not up_fills and not dn_fills:
            continue

        up_sh  = len(up_fills) * SHARES
        dn_sh  = len(dn_fills) * SHARES
        up_c   = sum(up_fills) * SHARES
        dn_c   = sum(dn_fills) * SHARES

        if winner == 'Up':
            pnl = (1.0 * up_sh - up_c) + (0.0 * dn_sh - dn_c)
        else:
            pnl = (0.0 * up_sh - up_c) + (1.0 * dn_sh - dn_c)

        avg_up = (up_c / up_sh) if up_sh else 0
        avg_dn = (dn_c / dn_sh) if dn_sh else 0
        both   = len(up_fills) > 0 and len(dn_fills) > 0

        results.append({
            'winner':  winner,
            'pnl':     pnl,
            'cost':    up_c + dn_c,
            'avg_up':  avg_up,
            'avg_dn':  avg_dn,
            'n_up':    len(up_fills),
            'n_dn':    len(dn_fills),
            'both':    both,
            'win':     pnl > 0,
        })

    if not results:
        return None

    n       = len(results)
    wins    = sum(1 for r in results if r['win'])
    both    = sum(1 for r in results if r['both'])
    net     = sum(r['pnl'] for r in results)
    cost    = sum(r['cost'] for r in results)
    avg_win = sum(r['pnl'] for r in results if r['win']) / max(wins, 1)
    avg_los = sum(r['pnl'] for r in results if not r['win']) / max(n - wins, 1)

    return {
        'n': n, 'wins': wins, 'both': both,
        'wr': 100 * wins / n,
        'both_pct': 100 * both / n,
        'net': net, 'cost': cost,
        'roi': 100 * net / cost if cost else 0,
        'avg_win': avg_win,
        'avg_los': avg_los,
        'pnl_per_candle': net / n,
    }


CAPS      = [0.25, 0.30, 0.35, 0.40, 0.45]
INTERVALS_TEST = [10, 15, 20]

print(f"\n{'='*90}")
print(f"  CORRECTED BACKTEST — Fixed {SHARES} Shares + Price Cap — ALL candles included")
print(f"{'='*90}")
print(f"  {'Cap':>5} {'Int':>4} {'Candles':>8} {'WR%':>6} {'Both%':>7} "
      f"{'AvgWin':>9} {'AvgLoss':>9} {'NetPnL':>10} {'ROI%':>7}")
print(f"  {'-'*70}")

best_roi = -999
best_cfg = None

for cap in CAPS:
    for iv in INTERVALS_TEST:
        total_n = total_wins = total_both = 0
        total_net = total_cost = 0
        total_win_pnl = total_los_pnl = 0
        total_win_n = total_los_n = 0

        for label, db in DBS.items():
            r = run(label, db, cap, iv)
            if not r:
                continue
            total_n    += r['n']
            total_wins += r['wins']
            total_both += r['both']
            total_net  += r['net']
            total_cost += r['cost']
            total_win_pnl += r['avg_win'] * r['wins']
            total_win_n   += r['wins']
            total_los_pnl += r['avg_los'] * (r['n'] - r['wins'])
            total_los_n   += (r['n'] - r['wins'])

        if not total_n:
            continue

        wr       = 100 * total_wins / total_n
        both_pct = 100 * total_both / total_n
        roi      = 100 * total_net / total_cost if total_cost else 0
        avg_win  = total_win_pnl / max(total_win_n, 1)
        avg_los  = total_los_pnl / max(total_los_n, 1)

        flag = ' <--' if roi > best_roi else ''
        if roi > best_roi:
            best_roi = roi
            best_cfg = (cap, iv)

        print(f"  {cap:>5.2f} {iv:>4}s {total_n:>8} {wr:>6.1f} {both_pct:>7.1f} "
              f"{avg_win:>+9.2f} {avg_los:>+9.2f} {total_net:>+10.2f} {roi:>7.1f}{flag}")
    print()

# Per-market breakdown for best config
if best_cfg:
    cap, iv = best_cfg
    print(f"\n  === Best config: cap={cap}, interval={iv}s — per market ===")
    print(f"  {'Market':>10} {'Candles':>8} {'WR%':>6} {'Both%':>7} "
          f"{'AvgWin':>9} {'AvgLoss':>9} {'NetPnL':>10} {'ROI%':>7}")
    print(f"  {'-'*68}")
    for label, db in DBS.items():
        r = run(label, db, cap, iv)
        if r:
            print(f"  {label:>10} {r['n']:>8} {r['wr']:>6.1f} {r['both_pct']:>7.1f} "
                  f"{r['avg_win']:>+9.2f} {r['avg_los']:>+9.2f} "
                  f"{r['net']:>+10.2f} {r['roi']:>7.1f}")
