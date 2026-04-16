"""
Granular Layered Backtest — BTC 5m
Buy 10 shares at every 5-cent price level as odds fall.
Sweep exit threshold from 0.10 to 0.20.

Buy levels: every 5c from 0.45 down to 0.05
  [0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05]
Exit: sell loser when mid <= EXIT_THRESH (sweep 0.10 to 0.20)
"""

import sqlite3
from collections import defaultdict

DB       = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300
SHARES   = 10
LEVELS   = [0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05]

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

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

def simulate(exit_thresh, levels=None, min_ask=0.0):
    if levels is None:
        levels = LEVELS

    results = []

    for cs, up_ticks, dn_ticks in ALL_CANDLES:
        all_ticks = sorted(
            [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
            [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
        )

        final_up_mid = up_ticks[-1][2]
        final_dn_mid = dn_ticks[-1][2]
        winner = 'Up' if final_up_mid >= final_dn_mid else 'Down'

        levels_triggered = set()
        up_entries = []
        dn_entries = []
        up_exit_bid = None
        dn_exit_bid = None

        last_up_ask = last_dn_ask = None
        prev_up_mid = prev_dn_mid = None

        for ts, side, ask, mid in all_ticks:
            if side == 'Up':
                last_up_ask = ask
            else:
                last_dn_ask = ask

            if last_up_ask is None or last_dn_ask is None:
                continue

            # Only fire on actual crossover (not retroactive)
            if prev_up_mid is not None and prev_dn_mid is not None:
                for lvl in levels:
                    if lvl in levels_triggered: continue
                    cur_mid  = mid
                    prev_mid = prev_up_mid if side == 'Up' else prev_dn_mid

                    if prev_mid > lvl >= cur_mid:
                        if min_ask > 0 and (last_up_ask < min_ask or last_dn_ask < min_ask):
                            continue
                        levels_triggered.add(lvl)
                        up_entries.append((last_up_ask, fee(SHARES, last_up_ask)))
                        dn_entries.append((last_dn_ask, fee(SHARES, last_dn_ask)))

            if side == 'Up':
                prev_up_mid = mid
            else:
                prev_dn_mid = mid

            # Exit loser
            if side == 'Up' and up_exit_bid is None and mid <= exit_thresh:
                up_exit_bid = max(0.0, 2 * mid - ask)
            elif side == 'Down' and dn_exit_bid is None and mid <= exit_thresh:
                dn_exit_bid = max(0.0, 2 * mid - ask)

        if not up_entries:
            results.append({'pnl': 0.0, 'cost': 0.0, 'win': False, 'lvls': 0})
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
        results.append({'pnl': pnl, 'cost': total_cost, 'win': pnl > 0, 'lvls': n})

    n_total   = len(results)
    n_entered = sum(1 for r in results if r['lvls'] > 0)
    wins      = sum(1 for r in results if r['win'])
    net       = sum(r['pnl'] for r in results)
    cost      = sum(r['cost'] for r in results)
    avg_lvls  = sum(r['lvls'] for r in results) / n_total if n_total else 0
    avg_win   = sum(r['pnl'] for r in results if r['win']) / max(wins, 1)
    losses    = n_total - wins
    avg_loss  = sum(r['pnl'] for r in results if not r['win']) / max(losses, 1)

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
        'avg_win':   avg_win,
        'avg_loss':  avg_loss,
    }

HDR = (f"  {'Exit thresh':>12} {'n':>5} {'entered':>8} {'WR%':>7} "
       f"{'NetPnL':>10} {'ROI%':>7} {'$/candle':>9} "
       f"{'avg win':>8} {'avg loss':>9} {'avgLvl':>7}")

SEP = "  " + "-" * 90

# ── Sweep exit threshold, original 5 levels ────────────────────────────────
print("=" * 95)
print("  GRANULAR BACKTEST — BTC 5m | 10 shares/level | crossover entries only")
print("=" * 95)

print(f"\n-- ORIGINAL 5 LEVELS {LEVELS[:5]} --")
print(HDR); print(SEP)
for thresh in [0.10, 0.12, 0.15, 0.17, 0.20]:
    r = simulate(thresh, levels=LEVELS[:5])
    print(f"  exit @ {thresh:<6}     {r['n']:>5} {r['entered']:>8} {r['wr']:>7.1f}% "
          f"{r['net']:>+10.2f} {r['roi']:>7.2f}% {r['ppc']:>+9.2f} "
          f"{r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} {r['avg_lvls']:>7.1f}")

# ── All 9 levels ────────────────────────────────────────────────────────────
print(f"\n-- ALL 9 LEVELS {LEVELS} --")
print(HDR); print(SEP)
for thresh in [0.10, 0.12, 0.15, 0.17, 0.20]:
    r = simulate(thresh, levels=LEVELS)
    print(f"  exit @ {thresh:<6}     {r['n']:>5} {r['entered']:>8} {r['wr']:>7.1f}% "
          f"{r['net']:>+10.2f} {r['roi']:>7.2f}% {r['ppc']:>+9.2f} "
          f"{r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} {r['avg_lvls']:>7.1f}")

# ── Lower levels only (0.25 and below) ────────────────────────────────────
lower = [0.25, 0.20, 0.15, 0.10, 0.05]
print(f"\n-- LOWER LEVELS ONLY {lower} --")
print(HDR); print(SEP)
for thresh in [0.10, 0.12, 0.15, 0.17, 0.20]:
    r = simulate(thresh, levels=lower)
    print(f"  exit @ {thresh:<6}     {r['n']:>5} {r['entered']:>8} {r['wr']:>7.1f}% "
          f"{r['net']:>+10.2f} {r['roi']:>7.2f}% {r['ppc']:>+9.2f} "
          f"{r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} {r['avg_lvls']:>7.1f}")

# ── Upper levels only (0.45 to 0.25) ─────────────────────────────────────
upper = [0.45, 0.40, 0.35, 0.30, 0.25]
print(f"\n-- UPPER LEVELS ONLY {upper} --")
print(HDR); print(SEP)
for thresh in [0.10, 0.12, 0.15, 0.17, 0.20]:
    r = simulate(thresh, levels=upper)
    print(f"  exit @ {thresh:<6}     {r['n']:>5} {r['entered']:>8} {r['wr']:>7.1f}% "
          f"{r['net']:>+10.2f} {r['roi']:>7.2f}% {r['ppc']:>+9.2f} "
          f"{r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} {r['avg_lvls']:>7.1f}")

# ── All 9 levels + D0.08 filter ───────────────────────────────────────────
print(f"\n-- ALL 9 LEVELS + min ask filter (0.08) --")
print(HDR); print(SEP)
for thresh in [0.10, 0.12, 0.15, 0.17, 0.20]:
    r = simulate(thresh, levels=LEVELS, min_ask=0.08)
    print(f"  exit @ {thresh:<6}     {r['n']:>5} {r['entered']:>8} {r['wr']:>7.1f}% "
          f"{r['net']:>+10.2f} {r['roi']:>7.2f}% {r['ppc']:>+9.2f} "
          f"{r['avg_win']:>+8.2f} {r['avg_loss']:>+9.2f} {r['avg_lvls']:>7.1f}")

print()
