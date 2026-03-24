"""
Ladder Scalp Backtest

Strategy 1 - MEAN REVERSION LADDER:
  Buy whichever side falls through each level (0.45, 0.40, 0.35, 0.30, 0.25).
  Each lot gets its own take-profit at entry_ask + N cents.
  If take-profit fills during candle -> realise profit.
  If candle ends unfilled -> resolve at 0 or 1.

Strategy 2 - OSCILLATION CAPTURE (both sides):
  Watch both sides. When either side drops Z cents from its recent peak, buy it.
  Sell when bid reaches entry + N cents. Repeat throughout candle.

BTC_5m, 100% candles, real fees.
"""

import sqlite3
from collections import defaultdict

DB       = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300
SHARES   = 100

def calc_fee(shares, price):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

print("Loading BTC_5m...")
conn = sqlite3.connect(DB)
rows = conn.execute(
    'SELECT unix_time, market_id, outcome, bid, ask, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn.close()

candles = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, bid, ask, mid in rows:
    cs = (int(float(ts)) // INTERVAL) * INTERVAL
    bid_val = float(bid) if bid else max(0.0, 2*float(mid) - float(ask))
    candles[(cs, mid_id)][out].append((float(ts), float(ask), bid_val, float(mid)))

print(f"Loaded {len(candles)} candles\n")

ENTRY_LEVELS = [0.45, 0.40, 0.35, 0.30, 0.25]
TAKE_PROFITS = [0.02, 0.03, 0.04, 0.05]

# ─────────────────────────────────────────────────────────────
# STRATEGY 1: Mean reversion ladder
# ─────────────────────────────────────────────────────────────
strat1 = {tp: [] for tp in TAKE_PROFITS}

for (cs, mid_id), sides in candles.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][3]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    all_ticks = sorted(
        [(ts, 'Up',   ask, bid, mid) for ts, ask, bid, mid in up_ticks] +
        [(ts, 'Down', ask, bid, mid) for ts, ask, bid, mid in dn_ticks]
    )

    for tp in TAKE_PROFITS:
        open_lots  = {'Up': [], 'Down': []}
        triggered  = {'Up': set(), 'Down': set()}
        realised   = 0.0
        total_cost = 0.0

        for ts, side, ask, bid, mid in all_ticks:
            # Check take-profits on this side
            still_open = []
            for entry_ask, tp_target in open_lots[side]:
                if bid >= tp_target:
                    realised += bid * SHARES - entry_ask * SHARES - calc_fee(SHARES, entry_ask)
                else:
                    still_open.append((entry_ask, tp_target))
            open_lots[side] = still_open

            # New entry level triggers
            for lvl in ENTRY_LEVELS:
                if lvl not in triggered[side] and mid <= lvl:
                    triggered[side].add(lvl)
                    open_lots[side].append((ask, ask + tp))
                    total_cost += ask * SHARES

        # Resolve remaining at candle end
        res_pnl = 0.0
        for side in ['Up', 'Down']:
            rp = 1.0 if side == winner else 0.0
            for entry_ask, _ in open_lots[side]:
                res_pnl += rp * SHARES - entry_ask * SHARES - calc_fee(SHARES, entry_ask)

        total_pnl = realised + res_pnl
        n_entries = sum(len(triggered[s]) for s in ['Up', 'Down'])
        if n_entries == 0:
            continue

        strat1[tp].append({
            'pnl': total_pnl, 'cost': total_cost,
            'win': total_pnl > 0, 'realised': realised,
        })

# ─────────────────────────────────────────────────────────────
# STRATEGY 2: Oscillation capture
# ─────────────────────────────────────────────────────────────
DROP_TRIGGERS = [0.05, 0.08, 0.10]
strat2 = {(d, tp): [] for d in DROP_TRIGGERS for tp in TAKE_PROFITS}

for (cs, mid_id), sides in candles.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][3]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    all_ticks = sorted(
        [(ts, 'Up',   ask, bid, mid) for ts, ask, bid, mid in up_ticks] +
        [(ts, 'Down', ask, bid, mid) for ts, ask, bid, mid in dn_ticks]
    )

    for drop in DROP_TRIGGERS:
        for tp in TAKE_PROFITS:
            peak_mid   = {'Up': 0.5, 'Down': 0.5}
            open_lots  = {'Up': [], 'Down': []}
            realised   = 0.0
            total_cost = 0.0
            MAX_LOTS   = 10

            for ts, side, ask, bid, mid in all_ticks:
                if mid > peak_mid[side]:
                    peak_mid[side] = mid

                # Check take-profits
                still_open = []
                for entry_ask, tp_target in open_lots[side]:
                    if bid >= tp_target:
                        realised += bid * SHARES - entry_ask * SHARES - calc_fee(SHARES, entry_ask)
                        peak_mid[side] = mid
                    else:
                        still_open.append((entry_ask, tp_target))
                open_lots[side] = still_open

                # Buy trigger: dropped enough from peak
                if (peak_mid[side] - mid >= drop and
                        len(open_lots[side]) < MAX_LOTS):
                    open_lots[side].append((ask, ask + tp))
                    total_cost += ask * SHARES
                    peak_mid[side] = mid  # reset so we don't spam buys

            # Resolve remaining
            res_pnl = 0.0
            for side in ['Up', 'Down']:
                rp = 1.0 if side == winner else 0.0
                for entry_ask, _ in open_lots[side]:
                    res_pnl += rp * SHARES - entry_ask * SHARES - calc_fee(SHARES, entry_ask)

            total_pnl = realised + res_pnl
            if total_cost == 0:
                continue

            strat2[(drop, tp)].append({
                'pnl': total_pnl, 'cost': total_cost,
                'win': total_pnl > 0, 'realised': realised,
            })

# ─────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────
def summarize(lst):
    if not lst: return None
    n    = len(lst)
    wins = sum(1 for r in lst if r['win'])
    net  = sum(r['pnl'] for r in lst)
    cost = sum(r['cost'] for r in lst)
    real = sum(r['realised'] for r in lst)
    return dict(n=n, wr=100*wins/n, net=net, roi=100*net/cost, ppc=net/n, realised=real)

def pr(label, s):
    if not s: return
    print(f"  {label:<40} n={s['n']:>4}  WR={s['wr']:>5.1f}%  "
          f"ROI={s['roi']:>+6.2f}%  $/c={s['ppc']:>+7.2f}  scalped=${s['realised']:>+9.2f}")

print("=" * 85)
print("  STRATEGY 1 - MEAN REVERSION LADDER")
print("  Buy each level (0.45->0.25) on whichever side falls, take-profit N cents above")
print("=" * 85)
for tp in TAKE_PROFITS:
    pr(f"+{int(tp*100)}c take-profit", summarize(strat1[tp]))

print()
print("=" * 85)
print("  STRATEGY 2 - OSCILLATION CAPTURE")
print("  Buy when mid drops X from recent peak, sell at +N cents. Repeat all candle.")
print("=" * 85)
for drop in DROP_TRIGGERS:
    print(f"\n  Drop trigger: -{int(drop*100)}c from peak")
    for tp in TAKE_PROFITS:
        pr(f"  +{int(tp*100)}c take-profit", summarize(strat2[(drop, tp)]))
