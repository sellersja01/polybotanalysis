"""
Delayed Early Exit Backtest

Tests what happens if we delay the early exit (mid=0.20) until
at least N seconds into the candle, to avoid getting whipsawed
in the opening volatility.

Compares:
  - Original: exit anytime mid hits 0.20
  - Delayed 60s: don't exit until 60s into candle
  - Delayed 90s: don't exit until 90s into candle
  - Delayed 120s: don't exit until 120s into candle
  - No early exit: hold loser to 0.00 always
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\selle\git_repository\polybotanalysis\databases\market_btc_5m.db',
    'BTC_15m': r'C:\Users\selle\git_repository\polybotanalysis\databases\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\selle\git_repository\polybotanalysis\databases\market_eth_5m.db',
}
INTERVALS = {'BTC_5m': 300, 'BTC_15m': 900, 'ETH_5m': 300}

ENTRY_LEVELS     = [0.45, 0.40, 0.35, 0.30, 0.25]
SHARES_PER_LEVEL = 100
EXIT_MID         = 0.20
FEE_RATE         = 0.25
FEE_EXP          = 2

def fee(shares, price):
    return shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXP

def run(label, db_path, exit_delay_secs):
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
        up_entries  = []
        dn_entries  = []
        up_exit_bid = None
        dn_exit_bid = None
        last_up_ask = None
        last_dn_ask = None

        for ts, side, ask, mid in all_ticks:
            if side == 'Up':   last_up_ask = ask
            else:              last_dn_ask = ask

            # Entry logic (unchanged)
            for lvl in ENTRY_LEVELS:
                if lvl not in levels_triggered and mid <= lvl:
                    levels_triggered.add(lvl)
                    if last_up_ask and last_dn_ask:
                        up_entries.append((last_up_ask, fee(SHARES_PER_LEVEL, last_up_ask)))
                        dn_entries.append((last_dn_ask, fee(SHARES_PER_LEVEL, last_dn_ask)))

            # Exit logic: only trigger after exit_delay_secs into candle
            secs_into_candle = ts - cs
            if secs_into_candle < exit_delay_secs:
                continue

            if side == 'Up' and up_exit_bid is None and mid <= EXIT_MID:
                up_exit_bid = max(0.0, 2 * mid - ask)
            elif side == 'Down' and dn_exit_bid is None and mid <= EXIT_MID:
                dn_exit_bid = max(0.0, 2 * mid - ask)

        if not up_entries or not dn_entries:
            continue

        n_entries      = len(up_entries)
        total_up_cost  = sum(a for a, f in up_entries) * SHARES_PER_LEVEL
        total_dn_cost  = sum(a for a, f in dn_entries) * SHARES_PER_LEVEL
        total_up_fees  = sum(f for a, f in up_entries)
        total_dn_fees  = sum(f for a, f in dn_entries)
        total_shares   = n_entries * SHARES_PER_LEVEL

        if winner == 'Up':
            up_pnl = (up_exit_bid * total_shares if up_exit_bid is not None else 1.0 * total_shares) - total_up_cost - total_up_fees
            dn_pnl = (dn_exit_bid * total_shares if dn_exit_bid is not None else 0.0 * total_shares) - total_dn_cost - total_dn_fees
        else:
            dn_pnl = (dn_exit_bid * total_shares if dn_exit_bid is not None else 1.0 * total_shares) - total_dn_cost - total_dn_fees
            up_pnl = (up_exit_bid * total_shares if up_exit_bid is not None else 0.0 * total_shares) - total_up_cost - total_up_fees

        pnl  = up_pnl + dn_pnl
        cost = total_up_cost + total_dn_cost
        results.append({'pnl': pnl, 'cost': cost, 'win': pnl > 0})

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


DELAYS = [
    ('Original (0s)',   0),
    ('Delay 30s',      30),
    ('Delay 60s',      60),
    ('Delay 90s',      90),
    ('Delay 120s',    120),
    ('Delay 180s',    180),
    ('No early exit', 99999),
]

MARKETS = ['BTC_5m', 'BTC_15m', 'ETH_5m']

print(f"\n{'='*90}")
print(f"  DELAYED EARLY EXIT BACKTEST  (All 5 levels, 100 shares/level, exit mid=0.20)")
print(f"{'='*90}")

for delay_name, delay_secs in DELAYS:
    print(f"\n  [{delay_name}]")
    print(f"  {'Market':>10} {'n':>6} {'WR%':>6} {'NetPnL':>12} {'ROI%':>7} {'$/candle':>10}")
    print(f"  {'-'*58}")

    total_n = total_wins_n = 0
    total_net = total_cost = 0.0

    for mkt in MARKETS:
        r = run(mkt, DBS[mkt], delay_secs)
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
