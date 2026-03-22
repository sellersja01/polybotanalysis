"""
Early Exit Backtest

Strategy:
  1. Buy BOTH sides at candle open (at ask prices) - guarantees both-sided
  2. Monitor mids throughout the candle
  3. When either side's mid drops below `exit_mid`, SELL that side at
     the current bid (= 2*mid - ask, the realistic sell price)
  4. Hold the other (winning) side to resolution at $1.00

Key question: does recovering bid value on the loser instead of holding
to $0 flip the math from negative to positive?

Grid over:
  - exit_mid: 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40
  - never_exit: baseline (no exit, hold both to resolution)
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


def run(label, db_path, exit_mid):
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
    n_loser_exited = 0
    n_winner_exited = 0   # accidental early exit of winning side
    exit_prices = []      # what price we actually exited the loser at

    for (cs, mid_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        final_mid = up_ticks[-1][2]
        winner = 'Up' if final_mid >= 0.5 else 'Down'

        # Step 1: buy both at open (at ask price)
        open_up_ask = up_ticks[0][1]
        open_dn_ask = dn_ticks[0][1]

        # Step 2: scan for exit trigger on each side
        # Exit when mid <= exit_mid; sell at bid = 2*mid - ask
        up_exit_bid = None
        dn_exit_bid = None

        all_ticks = sorted(
            [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
        )

        for ts, side, ask, mid in all_ticks:
            if exit_mid is None:
                break  # no-exit baseline
            bid = max(0.0, 2 * mid - ask)  # realistic sell price
            if side == 'Up' and up_exit_bid is None and mid <= exit_mid:
                up_exit_bid = bid
            elif side == 'Down' and dn_exit_bid is None and mid <= exit_mid:
                dn_exit_bid = bid

        # Step 3: calculate PnL
        if winner == 'Up':
            # Up wins → hold Up to 1.0
            # Down is loser → try to exit
            up_pnl = (1.0 - open_up_ask) * SHARES
            if dn_exit_bid is not None:
                dn_pnl = (dn_exit_bid - open_dn_ask) * SHARES
                n_loser_exited += 1
                exit_prices.append(dn_exit_bid)
            else:
                dn_pnl = (0.0 - open_dn_ask) * SHARES  # held to 0

            # Did we accidentally exit the winner?
            if up_exit_bid is not None:
                n_winner_exited += 1
                # Replace hold-to-1.0 with early exit
                up_pnl = (up_exit_bid - open_up_ask) * SHARES

        else:  # winner == 'Down'
            dn_pnl = (1.0 - open_dn_ask) * SHARES
            if up_exit_bid is not None:
                up_pnl = (up_exit_bid - open_up_ask) * SHARES
                n_loser_exited += 1
                exit_prices.append(up_exit_bid)
            else:
                up_pnl = (0.0 - open_up_ask) * SHARES

            if dn_exit_bid is not None:
                n_winner_exited += 1
                dn_pnl = (dn_exit_bid - open_dn_ask) * SHARES

        pnl  = up_pnl + dn_pnl
        cost = (open_up_ask + open_dn_ask) * SHARES
        results.append({'pnl': pnl, 'cost': cost, 'win': pnl > 0})

    if not results:
        return None

    n    = len(results)
    wins = sum(1 for r in results if r['win'])
    net  = sum(r['pnl'] for r in results)
    cost = sum(r['cost'] for r in results)
    avg_exit = sum(exit_prices) / len(exit_prices) if exit_prices else 0.0

    return {
        'n':               n,
        'wins':            wins,
        'wr':              100 * wins / n,
        'net':             net,
        'cost':            cost,
        'roi':             100 * net / cost if cost else 0,
        'pnl_per_candle':  net / n,
        'loser_exit_pct':  100 * n_loser_exited / n,
        'winner_exit_pct': 100 * n_winner_exited / n,
        'avg_exit_bid':    avg_exit,
    }


# ── Grid search ────────────────────────────────────────────────────────────────
EXIT_THRESHOLDS = [None, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05]

print(f"\n{'='*115}")
print(f"  EARLY EXIT BACKTEST — buy both at open, sell loser when mid <= threshold")
print(f"  Sell price = bid = 2*mid - ask  (realistic taker exit)")
print(f"{'='*115}")
print(f"  {'ExitMid':>8} {'Candles':>8} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} "
      f"{'$/candle':>9} {'Loser%':>7} {'Winner%':>8} {'AvgExitBid':>11}")
print(f"  {'-'*95}")

best_roi = -999
best_cfg = None

for thresh in EXIT_THRESHOLDS:
    total_n = total_wins = 0
    total_net = total_cost = 0
    total_loser = total_winner = total_exit_bids = 0
    exit_bid_sum = 0.0

    for label, db in DBS.items():
        r = run(label, db, thresh)
        if not r:
            continue
        total_n      += r['n']
        total_wins   += r['wins']
        total_net    += r['net']
        total_cost   += r['cost']
        total_loser  += r['loser_exit_pct'] * r['n'] / 100
        total_winner += r['winner_exit_pct'] * r['n'] / 100
        if r['avg_exit_bid'] > 0:
            exit_bid_sum += r['avg_exit_bid'] * r['n']
            total_exit_bids += r['n']

    if not total_n:
        continue

    wr    = 100 * total_wins / total_n
    roi   = 100 * total_net / total_cost if total_cost else 0
    ppc   = total_net / total_n
    l_pct = 100 * total_loser  / total_n
    w_pct = 100 * total_winner / total_n
    avg_eb = exit_bid_sum / total_exit_bids if total_exit_bids else 0

    label_str = f"{thresh:.2f}" if thresh is not None else "never"
    flag = ' <--' if roi > best_roi else ''
    if roi > best_roi:
        best_roi = roi
        best_cfg = thresh

    print(f"  {label_str:>8} {total_n:>8} {wr:>6.1f} {total_net:>+10.2f} {roi:>7.1f}{flag} "
          f"{ppc:>+8.2f} {l_pct:>6.1f}% {w_pct:>7.1f}% {avg_eb:>10.4f}")

# ── Per-market breakdown for best config ──────────────────────────────────────
if best_cfg is not None:
    print(f"\n  === Best exit threshold: mid={best_cfg} ===")
    print(f"  {'Market':>10} {'Candles':>8} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} "
          f"{'$/candle':>9} {'Loser%':>7} {'Winner%':>8} {'AvgExitBid':>11}")
    print(f"  {'-'*85}")
    for label, db in DBS.items():
        r = run(label, db, best_cfg)
        if r:
            print(f"  {label:>10} {r['n']:>8} {r['wr']:>6.1f} {r['net']:>+10.2f} "
                  f"{r['roi']:>7.1f} {r['pnl_per_candle']:>+8.2f} "
                  f"{r['loser_exit_pct']:>6.1f}% {r['winner_exit_pct']:>7.1f}% "
                  f"{r['avg_exit_bid']:>10.4f}")

# ── Open price distribution check ─────────────────────────────────────────────
print(f"\n  === Open price stats (what we pay at candle start) ===")
conn = sqlite3.connect(DBS['BTC_5m'])
rows = conn.execute(
    'SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn.close()
candles = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, ask, mid in rows:
    cs = (int(float(ts)) // 300) * 300
    candles[(cs, mid_id)][out].append((float(ts), float(ask), float(mid)))

open_up_asks, open_dn_asks, open_combineds = [], [], []
for (cs, mid_id), sides in candles.items():
    up = sides['Up']
    dn = sides['Down']
    if not up or not dn: continue
    final_mid = up[-1][2]
    if final_mid < 0.85 and final_mid > 0.15: continue
    open_up_asks.append(up[0][1])
    open_dn_asks.append(dn[0][1])
    open_combineds.append(up[0][1] + dn[0][1])

n = len(open_combineds)
print(f"  BTC_5m decisive candles: {n}")
print(f"  Avg open Up ask:   {sum(open_up_asks)/n:.4f}")
print(f"  Avg open Dn ask:   {sum(open_dn_asks)/n:.4f}")
print(f"  Avg combined open: {sum(open_combineds)/n:.4f}  (must beat this to profit)")
print(f"  % where combined < 1.0: {100*sum(1 for c in open_combineds if c<1.0)/n:.1f}%")
