"""
Asymmetric Entry Backtest

Strategy:
  - When either side's mid drops to a threshold, buy:
      * CHEAP side: N shares (heavy)
      * EXPENSIVE side: M shares (light)
  - Multiple thresholds: 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10
  - NO early exit — hold both to resolution
  - Winner pays $1.00, loser pays $0.00
  - Test different cheap:expensive ratios

Goal: replicate wallet_7's approach of loading cheap side
while maintaining a smaller position on the expensive side.
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'databases/market_btc_5m.db',
    'BTC_15m': r'databases/market_btc_15m.db',
    'ETH_5m':  r'databases/market_eth_5m.db',
}
INTERVALS = {'BTC_5m': 300, 'BTC_15m': 900, 'ETH_5m': 300}

ENTRY_LEVELS = [0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10]
FEE_RATE     = 0.25
FEE_EXP      = 2

def fee(shares, price):
    return shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXP

def run(label, db_path, cheap_shares, expensive_shares):
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
        winner = 'Up' if final_mid >= 0.5 else 'Down'

        all_ticks = sorted(
            [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
        )

        levels_triggered = set()
        up_shares_total  = 0
        dn_shares_total  = 0
        up_cost_total    = 0.0
        dn_cost_total    = 0.0
        up_fees_total    = 0.0
        dn_fees_total    = 0.0

        last_up_ask = None
        last_dn_ask = None

        for ts, side, ask, mid in all_ticks:
            if side == 'Up':   last_up_ask = ask
            else:              last_dn_ask = ask

            for lvl in ENTRY_LEVELS:
                if lvl not in levels_triggered and mid <= lvl:
                    levels_triggered.add(lvl)
                    if last_up_ask is None or last_dn_ask is None:
                        continue

                    # Cheap side = whichever has lower mid
                    if side == 'Up':
                        # Up is the cheap side
                        c_sh, e_sh = cheap_shares, expensive_shares
                        c_ask, e_ask = last_up_ask, last_dn_ask
                        up_shares_total  += c_sh
                        dn_shares_total  += e_sh
                        up_cost_total    += c_ask * c_sh
                        dn_cost_total    += e_ask * e_sh
                        up_fees_total    += fee(c_sh, c_ask)
                        dn_fees_total    += fee(e_sh, e_ask)
                    else:
                        # Down is the cheap side
                        c_sh, e_sh = cheap_shares, expensive_shares
                        c_ask, e_ask = last_dn_ask, last_up_ask
                        dn_shares_total  += c_sh
                        up_shares_total  += e_sh
                        dn_cost_total    += c_ask * c_sh
                        up_cost_total    += e_ask * e_sh
                        dn_fees_total    += fee(c_sh, c_ask)
                        up_fees_total    += fee(e_sh, e_ask)

        if not levels_triggered:
            continue

        total_cost = up_cost_total + dn_cost_total
        total_fees = up_fees_total + dn_fees_total

        if winner == 'Up':
            revenue = up_shares_total * 1.0
        else:
            revenue = dn_shares_total * 1.0

        pnl = revenue - total_cost - total_fees

        # Combined avg (for equal min shares)
        avg_up = up_cost_total / up_shares_total if up_shares_total else 0
        avg_dn = dn_cost_total / dn_shares_total if dn_shares_total else 0

        results.append({
            'pnl':  pnl,
            'cost': total_cost,
            'win':  pnl > 0,
        })

    if not results:
        return None

    n    = len(results)
    wins = sum(1 for r in results if r['win'])
    net  = sum(r['pnl'] for r in results)
    cost = sum(r['cost'] for r in results)

    return {
        'n':   n,
        'wr':  100 * wins / n,
        'net': net,
        'roi': 100 * net / cost if cost else 0,
        'ppc': net / n,
    }


# Ratios to test: (cheap_shares, expensive_shares)
RATIOS = [
    ('1:1  (100/100)', 100, 100),
    ('2:1  (200/100)', 200, 100),
    ('3:1  (300/100)', 300, 100),
    ('5:1  (500/100)', 500, 100),
    ('10:1 (1000/100)', 1000, 100),
]

MARKETS = ['BTC_5m', 'BTC_15m', 'ETH_5m']

print(f"\n{'='*90}")
print(f"  ASYMMETRIC ENTRY BACKTEST")
print(f"  Levels: {ENTRY_LEVELS} | No early exit | Buy cheap side heavy, expensive side light")
print(f"{'='*90}")

for ratio_name, cheap_sh, exp_sh in RATIOS:
    print(f"\n  [Ratio {ratio_name}]")
    print(f"  {'Market':>10} {'n':>6} {'WR%':>6} {'NetPnL':>12} {'ROI%':>7} {'$/candle':>10}")
    print(f"  {'-'*58}")

    total_n = 0
    total_net = total_cost = 0.0

    for mkt in MARKETS:
        r = run(mkt, DBS[mkt], cheap_sh, exp_sh)
        if not r:
            continue
        print(f"  {mkt:>10} {r['n']:>6} {r['wr']:>6.1f} {r['net']:>+12.2f} {r['roi']:>7.2f} {r['ppc']:>+10.2f}")
        total_n   += r['n']
        total_net += r['net']
        total_cost += r['net'] / (r['roi']/100) if r['roi'] else 0

    if total_n:
        roi = 100 * total_net / total_cost if total_cost else 0
        ppc = total_net / total_n
        print(f"  {'TOTAL':>10} {total_n:>6} {'':>6} {total_net:>+12.2f} {roi:>7.2f} {ppc:>+10.2f}")
