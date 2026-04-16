"""
Layered Entry Backtest

Strategy:
  - Enter both sides whenever either side's mid crosses a threshold level (0.45, 0.40, 0.35, 0.30, 0.25)
  - Each level = 100 shares of EACH side bought at current ask
  - Exit the loser side when its mid drops to 0.20 (sell at bid = 2*mid - ask)
  - Hold winner to resolution at $1.00
  - Real Polymarket fees applied: fee = shares * price * 0.25 * (price*(1-price))^2

Winner = whichever side had higher mid at the LAST observed tick (100% of candles included).
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
    'SOL_5m':  r'C:\Users\James\polybotanalysis\market_sol_5m.db',
    'SOL_15m': r'C:\Users\James\polybotanalysis\market_sol_15m.db',
    'XRP_5m':  r'C:\Users\James\polybotanalysis\market_xrp_5m.db',
    'XRP_15m': r'C:\Users\James\polybotanalysis\market_xrp_15m.db',
}
INTERVALS = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900, 'ETH_15m': 900,
             'SOL_5m': 300, 'SOL_15m': 900, 'XRP_5m': 300, 'XRP_15m': 900}

SHARES_PER_LEVEL = 100
EXIT_MID = 0.20
FEE_RATE = 0.25
FEE_EXP  = 2

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price)) ** 1  # post-Mar30 formula

def run(label, db_path, entry_levels):
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
    total_levels_entered = 0

    for (cs, mid_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        # Winner = whoever has higher mid at last observed tick
        final_up_mid = up_ticks[-1][2]
        final_dn_mid = dn_ticks[-1][2]
        winner_mid = max(final_up_mid, final_dn_mid)
        winner = 'Up' if final_up_mid >= final_dn_mid else 'Down'


        all_ticks = sorted(
            [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
        )

        # Track entries per level and exit bid per side
        levels_triggered = set()
        up_entries  = []   # list of (ask_paid, fee_paid)
        dn_entries  = []
        up_exit_bid = None
        dn_exit_bid = None

        # Track most recent ask per side as we scan forward
        last_up_ask = None
        last_dn_ask = None

        for ts, side, ask, mid in all_ticks:
            if side == 'Up':   last_up_ask = ask
            else:              last_dn_ask = ask

            # Entry: when this side's mid hits an untriggered level, buy both sides
            for lvl in entry_levels:
                if lvl not in levels_triggered and mid <= lvl:
                    levels_triggered.add(lvl)
                    if last_up_ask and last_dn_ask:
                        up_entries.append((last_up_ask, fee(SHARES_PER_LEVEL, last_up_ask)))
                        dn_entries.append((last_dn_ask, fee(SHARES_PER_LEVEL, last_dn_ask)))

            # Exit trigger: sell loser when mid <= EXIT_MID
            if side == 'Up' and up_exit_bid is None and mid <= EXIT_MID:
                up_exit_bid = max(0.0, 2 * mid - ask)
            elif side == 'Down' and dn_exit_bid is None and mid <= EXIT_MID:
                dn_exit_bid = max(0.0, 2 * mid - ask)

        if not up_entries or not dn_entries:
            results.append({'pnl': 0.0, 'cost': 0.0, 'win': False, 'levels': 0})
            continue  # never triggered — counts as $0 candle

        total_levels_entered += len(levels_triggered)
        n_entries = len(up_entries)  # same as len(dn_entries)

        total_up_cost  = sum(a for a, f in up_entries) * SHARES_PER_LEVEL
        total_dn_cost  = sum(a for a, f in dn_entries) * SHARES_PER_LEVEL
        total_up_fees  = sum(f for a, f in up_entries)
        total_dn_fees  = sum(f for a, f in dn_entries)
        total_shares   = n_entries * SHARES_PER_LEVEL

        if winner == 'Up':
            up_pnl  = (1.0 * total_shares) - total_up_cost - total_up_fees
            if dn_exit_bid is not None:
                dn_pnl = (dn_exit_bid * total_shares) - total_dn_cost - total_dn_fees
            else:
                dn_pnl = (0.0 * total_shares) - total_dn_cost - total_dn_fees
            # If winner side accidentally hit exit mid
            if up_exit_bid is not None:
                up_pnl = (up_exit_bid * total_shares) - total_up_cost - total_up_fees
        else:
            dn_pnl  = (1.0 * total_shares) - total_dn_cost - total_dn_fees
            if up_exit_bid is not None:
                up_pnl = (up_exit_bid * total_shares) - total_up_cost - total_up_fees
            else:
                up_pnl = (0.0 * total_shares) - total_up_cost - total_up_fees
            if dn_exit_bid is not None:
                dn_pnl = (dn_exit_bid * total_shares) - total_dn_cost - total_dn_fees

        pnl  = up_pnl + dn_pnl
        cost = total_up_cost + total_dn_cost
        results.append({'pnl': pnl, 'cost': cost, 'win': pnl > 0, 'levels': len(levels_triggered)})

    if not results:
        return None

    n    = len(results)
    wins = sum(1 for r in results if r['win'])
    net  = sum(r['pnl'] for r in results)
    cost = sum(r['cost'] for r in results)
    avg_lvls = sum(r['levels'] for r in results) / n

    return {
        'n':              n,
        'wins':           wins,
        'wr':             100 * wins / n,
        'net':            net,
        'cost':           cost,
        'roi':            100 * net / cost if cost else 0,
        'pnl_per_candle': net / n,
        'avg_levels':     avg_lvls,
    }


CONFIGS = [
    ('Single 0.30',          [0.30]),
    ('Single 0.25',          [0.25]),
    ('0.35+0.25',            [0.35, 0.25]),
    ('0.40+0.30',            [0.40, 0.30]),
    ('0.45+0.35+0.25',       [0.45, 0.35, 0.25]),
    ('0.45+0.40+0.35+0.30',  [0.45, 0.40, 0.35, 0.30]),
    ('All 5 levels',         [0.45, 0.40, 0.35, 0.30, 0.25]),
]

MARKETS = ['BTC_5m', 'BTC_15m', 'ETH_5m', 'ETH_15m', 'SOL_5m', 'SOL_15m', 'XRP_5m', 'XRP_15m']

print(f"\n{'='*100}")
print(f"  LAYERED ENTRY BACKTEST  (100% of candles, winner = highest mid at last tick, real fees)")
print(f"  Entry: buy both sides at each level | Exit loser at mid={EXIT_MID} | {SHARES_PER_LEVEL} shares/level")
print(f"{'='*100}")

for cfg_name, levels in CONFIGS:
    print(f"\n  Config: {cfg_name}  (levels: {levels})")
    print(f"  {'Market':>10} {'n':>6} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7} {'$/candle':>9} {'AvgLvls':>8}")
    print(f"  {'-'*65}")

    total_n = total_wins = 0
    total_net = total_cost = 0.0
    total_lvls = 0.0

    for mkt in MARKETS:
        r = run(mkt, DBS[mkt], levels)
        if not r:
            continue
        print(f"  {mkt:>10} {r['n']:>6} {r['wr']:>6.1f} {r['net']:>+10.2f} "
              f"{r['roi']:>7.2f} {r['pnl_per_candle']:>+9.2f} {r['avg_levels']:>8.1f}")
        total_n    += r['n']
        total_wins += r['wins']
        total_net  += r['net']
        total_cost += r['cost']
        total_lvls += r['avg_levels'] * r['n']

    if total_n:
        wr  = 100 * total_wins / total_n
        roi = 100 * total_net / total_cost if total_cost else 0
        ppc = total_net / total_n
        alv = total_lvls / total_n
        print(f"  {'TOTAL':>10} {total_n:>6} {wr:>6.1f} {total_net:>+10.2f} "
              f"{roi:>7.2f} {ppc:>+9.2f} {alv:>8.1f}")
