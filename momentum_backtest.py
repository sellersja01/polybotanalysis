"""
Momentum Filter Backtest

Tests two improvements:
1. Drop ETH_5m entirely
2. Momentum filter: only enter if mid was above threshold X ticks ago
   (fast-falling = decisive move, slow grind = likely to bounce)

Uses Single 0.25 config, 100% candles, real fees.
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
}
INTERVALS = {'BTC_5m': 300, 'BTC_15m': 900, 'ETH_5m': 300}
SHARES       = 100
ENTRY_THRESH = 0.25
EXIT_THRESH  = 0.20

def calc_fee(shares, price):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

def run_market(label, db_path):
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

    candle_results = []

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

        triggered    = False
        trigger_ts   = None
        up_entry_ask = None
        dn_entry_ask = None
        last_up_ask  = None
        last_dn_ask  = None
        up_exit_bid  = None
        dn_exit_bid  = None

        # Track mid history for momentum calculation
        # mid_history[side] = list of (ts, mid) in order
        mid_history = {'Up': [], 'Down': []}

        for ts, side, ask, mid in all_ticks:
            if side == 'Up':  last_up_ask = ask
            else:             last_dn_ask = ask

            mid_history[side].append((ts, mid))

            if not triggered and last_up_ask and last_dn_ask and mid <= ENTRY_THRESH:
                triggered    = True
                trigger_ts   = ts
                up_entry_ask = last_up_ask
                dn_entry_ask = last_dn_ask

                # Momentum: what was this side's mid N ticks ago?
                hist = mid_history[side]
                mid_2_ticks_ago  = hist[-3][1] if len(hist) >= 3 else hist[0][1]
                mid_4_ticks_ago  = hist[-5][1] if len(hist) >= 5 else hist[0][1]
                mid_6_ticks_ago  = hist[-7][1] if len(hist) >= 7 else hist[0][1]
                mid_10_ticks_ago = hist[-11][1] if len(hist) >= 11 else hist[0][1]

                # Time-based: what was mid 30s / 60s / 90s ago?
                def mid_n_secs_ago(secs):
                    target = ts - secs
                    for t, m in reversed(hist[:-1]):
                        if t <= target:
                            return m
                    return hist[0][1]

                mid_30s  = mid_n_secs_ago(30)
                mid_60s  = mid_n_secs_ago(60)
                mid_90s  = mid_n_secs_ago(90)
                mid_120s = mid_n_secs_ago(120)

                candle_results.append({
                    'winner': winner,
                    'up_entry_ask': up_entry_ask,
                    'dn_entry_ask': dn_entry_ask,
                    'mid_2t':  mid_2_ticks_ago,
                    'mid_4t':  mid_4_ticks_ago,
                    'mid_6t':  mid_6_ticks_ago,
                    'mid_10t': mid_10_ticks_ago,
                    'mid_30s': mid_30s,
                    'mid_60s': mid_60s,
                    'mid_90s': mid_90s,
                    'mid_120s': mid_120s,
                })

            if triggered and mid <= EXIT_THRESH:
                if side == 'Up' and up_exit_bid is None:
                    up_exit_bid = max(0.0, 2 * mid - ask)
                elif side == 'Down' and dn_exit_bid is None:
                    dn_exit_bid = max(0.0, 2 * mid - ask)

        if not candle_results or candle_results[-1].get('resolved'):
            continue

        if not triggered:
            continue

        r = candle_results[-1]

        up_cost = r['up_entry_ask'] * SHARES
        dn_cost = r['dn_entry_ask'] * SHARES
        up_fee  = calc_fee(SHARES, r['up_entry_ask'])
        dn_fee  = calc_fee(SHARES, r['dn_entry_ask'])

        if winner == 'Up':
            up_resolve = up_exit_bid if up_exit_bid is not None else 1.0
            dn_resolve = dn_exit_bid if dn_exit_bid is not None else 0.0
        else:
            dn_resolve = dn_exit_bid if dn_exit_bid is not None else 1.0
            up_resolve = up_exit_bid if up_exit_bid is not None else 0.0

        pnl  = (up_resolve * SHARES - up_cost - up_fee) + \
               (dn_resolve * SHARES - dn_cost - dn_fee)
        cost = up_cost + dn_cost

        r['pnl']  = pnl
        r['cost'] = cost
        r['win']  = pnl > 0
        r['resolved'] = True

    return [r for r in candle_results if r.get('resolved')]


def summarize(lst):
    if not lst: return None
    n    = len(lst)
    wins = sum(1 for r in lst if r['win'])
    net  = sum(r['pnl'] for r in lst)
    cost = sum(r['cost'] for r in lst)
    return dict(n=n, wr=100*wins/n, net=net, roi=100*net/cost, ppc=net/n)

def pr(label, s, indent=2):
    if s:
        pad = ' ' * indent
        print(f"{pad}{label:<35} n={s['n']:>4}  WR={s['wr']:>5.1f}%  ROI={s['roi']:>+6.2f}%  $/candle={s['ppc']:>+7.2f}")


print("Loading data...")
btc5  = run_market('BTC_5m',  DBS['BTC_5m'])
btc15 = run_market('BTC_15m', DBS['BTC_15m'])
eth5  = run_market('ETH_5m',  DBS['ETH_5m'])

all_3  = btc5 + btc15 + eth5
btc_only = btc5 + btc15

print(f"\n{'='*75}")
print(f"  BASELINE")
print(f"{'='*75}")
pr("BTC_5m",          summarize(btc5))
pr("BTC_15m",         summarize(btc15))
pr("ETH_5m",          summarize(eth5))
pr("ALL 3 markets",   summarize(all_3))
pr("BTC only (no ETH_5m)", summarize(btc_only))

print(f"\n{'='*75}")
print(f"  MOMENTUM FILTER  (how fast was mid falling at trigger?)")
print(f"{'='*75}")

# Test time-based momentum thresholds
for field, label in [('mid_30s','30s ago'), ('mid_60s','60s ago'),
                     ('mid_90s','90s ago'), ('mid_120s','120s ago')]:
    print(f"\n  -- Mid {label} at trigger moment --")
    for thresh in [0.28, 0.30, 0.32, 0.35, 0.40]:
        # "Fast" = mid was above thresh N seconds ago (fell quickly)
        fast_all  = [r for r in all_3   if r[field] >= thresh]
        slow_all  = [r for r in all_3   if r[field] <  thresh]
        fast_btc  = [r for r in btc_only if r[field] >= thresh]
        slow_btc  = [r for r in btc_only if r[field] <  thresh]

        sf = summarize(fast_all)
        ss = summarize(slow_all)
        if sf and ss:
            print(f"    {label} >= {thresh}  TRADE: ", end='')
            pr("", sf, indent=0)
            print(f"    {label} <  {thresh}  SKIP:  ", end='')
            pr("", ss, indent=0)
            print()

print(f"\n{'='*75}")
print(f"  COMBINED: BTC only + Momentum filter")
print(f"{'='*75}")

for field, label in [('mid_60s','60s ago'), ('mid_90s','90s ago')]:
    for thresh in [0.28, 0.30, 0.32, 0.35]:
        fast = [r for r in btc_only if r[field] >= thresh]
        slow = [r for r in btc_only if r[field] <  thresh]
        sf   = summarize(fast)
        ss   = summarize(slow)
        if sf:
            print(f"  BTC only + mid_{label} >= {thresh}:")
            pr("  TRADE", sf)
            pr("  SKIP ", ss)
            print()
