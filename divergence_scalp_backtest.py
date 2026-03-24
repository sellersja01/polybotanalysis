"""
Divergence Scalp Backtest

Two strategies when mid hits 0.25:
A) ONE-SIDED: Buy only diverged side at ask. Set limit sell N cents above.
   If fills -> take profit and exit. If not -> hold to resolution.

B) TWO-SIDED + SCALP: Buy both sides (current strategy). Set limit sell on
   diverged side N cents above buy. If fills -> lock quick profit on that
   side, hold other to resolution. If not -> same as current strategy.

BTC_5m, 100% candles, real fees.
"""

import sqlite3
from collections import defaultdict

DB       = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300
SHARES   = 100
ENTRY_THRESH = 0.25
EXIT_THRESH  = 0.20

def calc_fee(shares, price):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

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

BOUNCE_TARGETS = [0.02, 0.03, 0.04, 0.05]
results_a = {t: [] for t in BOUNCE_TARGETS}
results_b = {t: [] for t in BOUNCE_TARGETS}
baseline  = []

for (cs, mid_id), sides in candles.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][3]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    all_ticks = sorted(
        [(ts,'Up',  ask,bid,mid) for ts,ask,bid,mid in up_ticks] +
        [(ts,'Down',ask,bid,mid) for ts,ask,bid,mid in dn_ticks]
    )

    triggered = False; trigger_idx = None
    div_side = None; oth_side = None
    div_ask = None; oth_ask = None
    last_ua = None; last_da = None

    for i, (ts, side, ask, bid, mid) in enumerate(all_ticks):
        if side == 'Up':  last_ua = ask
        else:             last_da = ask
        if not triggered and last_ua and last_da and mid <= ENTRY_THRESH:
            triggered   = True; trigger_idx = i
            div_side    = side
            oth_side    = 'Down' if side == 'Up' else 'Up'
            div_ask     = ask
            oth_ask     = last_da if side == 'Up' else last_ua
            break

    if not triggered:
        continue

    div_cost = div_ask * SHARES; div_fee = calc_fee(SHARES, div_ask)
    oth_cost = oth_ask * SHARES; oth_fee = calc_fee(SHARES, oth_ask)

    # Scan post-trigger for early exits
    base_div_exit = None; base_oth_exit = None
    for ts, side, ask, bid, mid in all_ticks[trigger_idx+1:]:
        if mid <= EXIT_THRESH:
            if side == div_side and base_div_exit is None: base_div_exit = max(0.0, bid)
            if side == oth_side and base_oth_exit is None: base_oth_exit = max(0.0, bid)

    def res(which, early):
        if which == winner: return 1.0
        return early if early is not None else 0.0

    base_pnl = (res(div_side,base_div_exit)*SHARES - div_cost - div_fee) + \
               (res(oth_side,base_oth_exit)*SHARES - oth_cost - oth_fee)
    baseline.append({'pnl':base_pnl,'cost':div_cost+oth_cost,'win':base_pnl>0,'limit_filled':False})

    for bounce in BOUNCE_TARGETS:
        limit_price = div_ask + bounce
        filled = False; fill_bid = None
        div_exit = None; oth_exit = None

        for ts, side, ask, bid, mid in all_ticks[trigger_idx+1:]:
            if side == div_side and not filled and bid >= limit_price:
                filled = True; fill_bid = bid
            if mid <= EXIT_THRESH:
                if side == div_side and div_exit is None and not filled: div_exit = max(0.0, bid)
                if side == oth_side and oth_exit is None:                oth_exit = max(0.0, bid)

        # Strategy A: one-sided
        if filled:
            pnl_a = fill_bid * SHARES - div_cost - div_fee
        else:
            pnl_a = res(div_side, div_exit) * SHARES - div_cost - div_fee
        results_a[bounce].append({'pnl':pnl_a,'cost':div_cost,'win':pnl_a>0,'limit_filled':filled})

        # Strategy B: two-sided + scalp
        if filled:
            div_pnl = fill_bid * SHARES - div_cost - div_fee
            oth_pnl = res(oth_side, oth_exit) * SHARES - oth_cost - oth_fee
            pnl_b   = div_pnl + oth_pnl
        else:
            pnl_b = base_pnl
        results_b[bounce].append({'pnl':pnl_b,'cost':div_cost+oth_cost,'win':pnl_b>0,'limit_filled':filled})

def summarize(lst):
    if not lst: return None
    n=len(lst); wins=sum(1 for r in lst if r['win'])
    net=sum(r['pnl'] for r in lst); cost=sum(r['cost'] for r in lst)
    fills=sum(1 for r in lst if r.get('limit_filled'))
    return dict(n=n,wr=100*wins/n,net=net,roi=100*net/cost,ppc=net/n,fill_pct=100*fills/n)

def pr(label, s, ind=2):
    if not s: return
    print(f"{'  '*ind}{label:<35} n={s['n']:>4}  WR={s['wr']:>5.1f}%  ROI={s['roi']:>+6.2f}%  $/c={s['ppc']:>+7.2f}  fills={s['fill_pct']:>4.0f}%")

print(f"\n{'='*85}")
print(f"  BASELINE — two-sided, no scalp")
print(f"{'='*85}")
pr("BTC_5m", summarize(baseline), 1)

print(f"\n{'='*85}")
print(f"  STRATEGY A — ONE-SIDED SCALP")
print(f"{'='*85}")
for t in BOUNCE_TARGETS:
    r=results_a[t]; sa=summarize(r)
    sf=summarize([x for x in r if x['limit_filled']])
    su=summarize([x for x in r if not x['limit_filled']])
    print(f"\n  +{int(t*100)}c target")
    pr("ALL", sa, 1)
    if sf: pr(f"  limit filled ({sf['n']} candles)", sf, 1)
    if su: pr(f"  held to res  ({su['n']} candles)", su, 1)

print(f"\n{'='*85}")
print(f"  STRATEGY B — TWO-SIDED + SCALP DIVERGED SIDE")
print(f"{'='*85}")
for t in BOUNCE_TARGETS:
    r=results_b[t]; sb=summarize(r)
    sf=summarize([x for x in r if x['limit_filled']])
    su=summarize([x for x in r if not x['limit_filled']])
    print(f"\n  +{int(t*100)}c target")
    pr("ALL", sb, 1)
    if sf: pr(f"  limit filled ({sf['n']} candles)", sf, 1)
    if su: pr(f"  held to res  ({su['n']} candles)", su, 1)
