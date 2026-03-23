"""
Weekday vs Weekend Backtest

Tests whether the wait-for-divergence strategy performs differently
on weekdays (Mon-Fri) vs weekends (Sat-Sun).

Uses Single 0.25 config, 100% of candles, real fees.
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
}
INTERVALS = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900}
SHARES    = 100
FEE_RATE  = 0.25
FEE_EXP   = 2

def calc_fee(shares, price):
    return shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXP

def run(label, db_path, entry_levels=[0.25], exit_mid=0.20):
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

    # Group results by day type
    results = {'weekday': [], 'weekend': []}

    for (cs, mid_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        dt = datetime.fromtimestamp(cs, tz=timezone.utc)
        day_type = 'weekend' if dt.weekday() >= 5 else 'weekday'
        dow_name = dt.strftime('%A')

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
            if side == 'Up':  last_up_ask = ask
            else:             last_dn_ask = ask

            if last_up_ask and last_dn_ask:
                for lvl in entry_levels:
                    if lvl not in levels_triggered and mid <= lvl:
                        levels_triggered.add(lvl)
                        up_entries.append((last_up_ask, calc_fee(SHARES, last_up_ask)))
                        dn_entries.append((last_dn_ask, calc_fee(SHARES, last_dn_ask)))

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
        else:
            dn_resolve = dn_exit_bid if dn_exit_bid is not None else 1.0
            up_resolve = up_exit_bid if up_exit_bid is not None else 0.0

        pnl  = (up_resolve * total_shares - total_up_cost - total_up_fee) + \
               (dn_resolve * total_shares - total_dn_cost - total_dn_fee)
        cost = total_up_cost + total_dn_cost
        results[day_type].append({'pnl': pnl, 'cost': cost, 'win': pnl > 0, 'dow': dow_name})

    return results


def summarize(results_list):
    if not results_list:
        return None
    n    = len(results_list)
    wins = sum(1 for r in results_list if r['win'])
    net  = sum(r['pnl'] for r in results_list)
    cost = sum(r['cost'] for r in results_list)
    return {
        'n': n,
        'wr': 100 * wins / n,
        'net': net,
        'roi': 100 * net / cost if cost else 0,
        'ppc': net / n,
    }


print(f"\n{'='*80}")
print(f"  WEEKDAY vs WEEKEND BACKTEST  (Single 0.25, 100% candles, real fees)")
print(f"{'='*80}")

# Per-market breakdown
all_weekday = []
all_weekend = []

for mkt, db in DBS.items():
    res = run(mkt, db)
    all_weekday.extend(res['weekday'])
    all_weekend.extend(res['weekend'])

    wd = summarize(res['weekday'])
    we = summarize(res['weekend'])

    print(f"\n  {mkt}")
    print(f"  {'Type':>10} {'n':>5} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
    print(f"  {'-'*55}")
    if wd:
        print(f"  {'Weekday':>10} {wd['n']:>5} {wd['wr']:>6.1f} {wd['net']:>+10.2f} {wd['roi']:>7.2f} {wd['ppc']:>+9.2f}")
    if we:
        print(f"  {'Weekend':>10} {we['n']:>5} {we['wr']:>6.1f} {we['net']:>+10.2f} {we['roi']:>7.2f} {we['ppc']:>+9.2f}")

# Totals
wd_tot = summarize(all_weekday)
we_tot = summarize(all_weekend)

print(f"\n  {'='*55}")
print(f"  TOTAL ACROSS ALL MARKETS")
print(f"  {'Type':>10} {'n':>5} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
print(f"  {'-'*55}")
if wd_tot:
    print(f"  {'Weekday':>10} {wd_tot['n']:>5} {wd_tot['wr']:>6.1f} {wd_tot['net']:>+10.2f} {wd_tot['roi']:>7.2f} {wd_tot['ppc']:>+9.2f}")
if we_tot:
    print(f"  {'Weekend':>10} {we_tot['n']:>5} {we_tot['wr']:>6.1f} {we_tot['net']:>+10.2f} {we_tot['roi']:>7.2f} {we_tot['ppc']:>+9.2f}")

# Day-by-day breakdown
print(f"\n\n  DAY-BY-DAY BREAKDOWN (all markets combined)")
print(f"  {'Day':>12} {'n':>5} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9}")
print(f"  {'-'*55}")

all_results = all_weekday + all_weekend
day_order = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
for day in day_order:
    day_res = [r for r in all_results if r['dow'] == day]
    s = summarize(day_res)
    if s:
        marker = '  <<' if day in ('Saturday','Sunday') else ''
        print(f"  {day:>12} {s['n']:>5} {s['wr']:>6.1f} {s['net']:>+10.2f} {s['roi']:>7.2f} {s['ppc']:>+9.2f}{marker}")
