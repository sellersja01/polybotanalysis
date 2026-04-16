"""
Loss Limiter Backtest — BTC 5m
Tests 5 loss-limiting filters against the base strategy.

Base: All 5 levels [0.45,0.40,0.35,0.30,0.25], 100 shares/level, exit loser at 0.20

Filters tested:
  A. Confirmation  — only enter after mid stays below level for N consecutive ticks
  B. Time cutoff   — no new entries after T seconds into candle
  C. Spread check  — skip entry if the OTHER side is already below MIN_OTHER_MID
  D. Min loser ask — skip entry if either side's ask is below MIN_ASK (near-dead token)
  E. Max levels    — cap entries at N levels max per candle
"""

import sqlite3
from collections import defaultdict

DB       = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300
SHARES   = 100
LEVELS   = [0.45, 0.40, 0.35, 0.30, 0.25]
EXIT_MID = 0.20

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

# -- Load data ----------------------------------------------------------------
print("Loading BTC 5m...")
conn = sqlite3.connect(DB)
rows = conn.execute(
    'SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn.close()

candles = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, ask, mid in rows:
    cs = (int(float(ts)) // INTERVAL) * INTERVAL
    candles[(cs, mid_id)][out].append((float(ts), float(ask), float(mid)))

ALL_CANDLES = []
for (cs, mid_id), sides in candles.items():
    if sides['Up'] and sides['Down']:
        ALL_CANDLES.append((cs, sides['Up'], sides['Down']))

print(f"Total candles: {len(ALL_CANDLES)}\n")

# -- Core simulator ------------------------------------------------------------
def simulate(candle_list, levels=None, confirm_ticks=1, time_cutoff=300,
             min_other_mid=0.0, min_ask=0.0, max_levels=99):
    """
    confirm_ticks : N consecutive ticks below level before entry fires
    time_cutoff   : no entries after this many seconds into candle
    min_other_mid : skip entry if the other side's current mid < this
    min_ask       : skip entry if either side's ask < this
    max_levels    : max number of levels to enter per candle
    """
    if levels is None:
        levels = LEVELS

    results = []

    for cs, up_ticks, dn_ticks in candle_list:
        all_ticks = sorted(
            [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
        )

        # Winner
        final_up_mid = up_ticks[-1][2]
        final_dn_mid = dn_ticks[-1][2]
        winner = 'Up' if final_up_mid >= final_dn_mid else 'Down'

        levels_triggered  = set()
        up_entries        = []
        dn_entries        = []
        up_exit_bid       = None
        dn_exit_bid       = None

        last_up_ask = last_dn_ask = None
        last_up_mid = last_dn_mid = None

        # For confirmation: track consecutive ticks below each level per side
        consec_below = {lvl: 0 for lvl in levels}

        for ts, side, ask, mid in all_ticks:
            elapsed = ts - cs

            if side == 'Up':
                last_up_ask = ask
                last_up_mid = mid
            else:
                last_dn_ask = ask
                last_dn_mid = mid

            if last_up_ask is None or last_dn_ask is None:
                continue

            # Entry checks
            if len(levels_triggered) < max_levels and elapsed <= time_cutoff:
                for lvl in levels:
                    if lvl in levels_triggered:
                        consec_below[lvl] = 0
                        continue

                    cur_mid = last_up_mid if side == 'Up' else last_dn_mid
                    other_mid = last_dn_mid if side == 'Up' else last_up_mid

                    if cur_mid <= lvl:
                        consec_below[lvl] += 1
                    else:
                        consec_below[lvl] = 0

                    if consec_below[lvl] >= confirm_ticks:
                        # Filter C: other side too cheap already
                        if other_mid is not None and other_mid < min_other_mid:
                            continue
                        # Filter D: either ask too low
                        if last_up_ask < min_ask or last_dn_ask < min_ask:
                            continue

                        levels_triggered.add(lvl)
                        up_entries.append((last_up_ask, fee(SHARES, last_up_ask)))
                        dn_entries.append((last_dn_ask, fee(SHARES, last_dn_ask)))

            # Exit: sell loser when mid <= EXIT_MID
            if side == 'Up' and up_exit_bid is None and mid <= EXIT_MID:
                up_exit_bid = max(0.0, 2 * mid - ask)
            elif side == 'Down' and dn_exit_bid is None and mid <= EXIT_MID:
                dn_exit_bid = max(0.0, 2 * mid - ask)

        # No entry — count as $0
        if not up_entries:
            results.append({'pnl': 0.0, 'cost': 0.0, 'win': False, 'triggered': 0})
            continue

        n            = len(up_entries)
        total_shares = n * SHARES
        up_cost      = sum(a for a, f in up_entries) * SHARES
        dn_cost      = sum(a for a, f in dn_entries) * SHARES
        up_fees      = sum(f for a, f in up_entries)
        dn_fees      = sum(f for a, f in dn_entries)
        total_cost   = up_cost + dn_cost + up_fees + dn_fees

        if winner == 'Up':
            win_pnl  = (1.0 * total_shares) - up_cost - up_fees
            lose_pnl = (dn_exit_bid * total_shares if dn_exit_bid else 0.0) - dn_cost - dn_fees
        else:
            win_pnl  = (1.0 * total_shares) - dn_cost - dn_fees
            lose_pnl = (up_exit_bid * total_shares if up_exit_bid else 0.0) - up_cost - up_fees

        pnl = win_pnl + lose_pnl
        results.append({'pnl': pnl, 'cost': total_cost, 'win': pnl > 0, 'triggered': n})

    n_total   = len(results)
    n_entered = sum(1 for r in results if r['triggered'] > 0)
    wins      = sum(1 for r in results if r['win'])
    net       = sum(r['pnl'] for r in results)
    cost      = sum(r['cost'] for r in results)
    avg_lvls  = sum(r['triggered'] for r in results) / n_total if n_total else 0

    return {
        'n':         n_total,
        'entered':   n_entered,
        'enter_pct': 100 * n_entered / n_total if n_total else 0,
        'wins':      wins,
        'wr':        100 * wins / n_total,
        'net':       net,
        'cost':      cost,
        'roi':       100 * net / cost if cost else 0,
        'ppc':       net / n_total,
        'avg_lvls':  avg_lvls,
    }

def row(label, r):
    return (f"  {label:<40} {r['n']:>5} {r['entered']:>5} ({r['enter_pct']:>4.0f}%) "
            f"{r['wr']:>6.1f}%  {r['net']:>+9.2f}  {r['roi']:>6.2f}%  "
            f"{r['ppc']:>+8.2f}  {r['avg_lvls']:>6.1f}")

HDR = (f"  {'Config':<40} {'n':>5} {'entr':>5} {'(%)':>6} "
       f"{'WR%':>7}  {'NetPnL':>9}  {'ROI%':>6}  "
       f"{'$/candle':>8}  {'avgLvl':>6}")

# -- Run all configs -----------------------------------------------------------
print("=" * 100)
print("  LOSS LIMITER BACKTEST — BTC 5m | 100 shares/level | post-Mar30 fees")
print("=" * 100)

# BASE
print("\n-- BASE (no filters) ----------------------------------------------------------------------------")
print(HDR)
base = simulate(ALL_CANDLES)
print(row("Base: all 5 levels, no filters", base))

# -- FILTER A: Confirmation ticks ---------------------------------------------
print("\n-- FILTER A: Confirmation (wait N ticks below level before entering) ----------------------------")
print(HDR)
for n in [2, 3, 5, 10]:
    r = simulate(ALL_CANDLES, confirm_ticks=n)
    print(row(f"A{n}: confirm={n} ticks", r))

# -- FILTER B: Time cutoff -----------------------------------------------------
print("\n-- FILTER B: Time cutoff (no new entries after T seconds) ---------------------------------------")
print(HDR)
for t in [60, 90, 120, 150, 180, 240]:
    r = simulate(ALL_CANDLES, time_cutoff=t)
    print(row(f"B{t}: no entries after {t}s", r))

# -- FILTER C: Min other side mid ---------------------------------------------
print("\n-- FILTER C: Min other side mid (skip if other side mid < threshold) ---------------------------")
print(HDR)
for m in [0.10, 0.15, 0.20, 0.25, 0.30]:
    r = simulate(ALL_CANDLES, min_other_mid=m)
    print(row(f"C{m}: skip if other_mid < {m}", r))

# -- FILTER D: Min ask (don't buy near-dead tokens) ----------------------------
print("\n-- FILTER D: Min ask (skip entry if either side ask < threshold) -------------------------------")
print(HDR)
for a in [0.03, 0.05, 0.08, 0.10, 0.15]:
    r = simulate(ALL_CANDLES, min_ask=a)
    print(row(f"D{a}: skip if ask < {a}", r))

# -- FILTER E: Max levels ------------------------------------------------------
print("\n-- FILTER E: Max levels per candle --------------------------------------------------------------")
print(HDR)
for ml in [1, 2, 3, 4]:
    r = simulate(ALL_CANDLES, max_levels=ml)
    print(row(f"E{ml}: max {ml} level(s) per candle", r))

# -- COMBINATIONS: Best filters stacked ---------------------------------------
print("\n-- COMBINATIONS: Stacking the best individual filters -------------------------------------------")
print(HDR)
combos = [
    ("B120 + D0.05",            dict(time_cutoff=120, min_ask=0.05)),
    ("B150 + D0.05",            dict(time_cutoff=150, min_ask=0.05)),
    ("B120 + C0.15",            dict(time_cutoff=120, min_other_mid=0.15)),
    ("B150 + C0.15",            dict(time_cutoff=150, min_other_mid=0.15)),
    ("D0.05 + C0.15",           dict(min_ask=0.05, min_other_mid=0.15)),
    ("B120 + D0.05 + C0.15",    dict(time_cutoff=120, min_ask=0.05, min_other_mid=0.15)),
    ("B150 + D0.05 + C0.15",    dict(time_cutoff=150, min_ask=0.05, min_other_mid=0.15)),
    ("E3 + D0.05",              dict(max_levels=3, min_ask=0.05)),
    ("E3 + B150 + D0.05",       dict(max_levels=3, time_cutoff=150, min_ask=0.05)),
    ("A3 + B150 + D0.05",       dict(confirm_ticks=3, time_cutoff=150, min_ask=0.05)),
    ("A3 + D0.05 + C0.15",      dict(confirm_ticks=3, min_ask=0.05, min_other_mid=0.15)),
    ("Best combo all",          dict(confirm_ticks=3, time_cutoff=150, min_ask=0.05, min_other_mid=0.15)),
]
for label, kwargs in combos:
    r = simulate(ALL_CANDLES, **kwargs)
    print(row(label, r))

print()
