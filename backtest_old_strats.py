"""
Backtest the 4 unlisted strategies on BTC 5m:
  Strat 8  (Wait-for-Divergence)    — Buy both sides when either mid drops to 0.25, exit loser at 0.20
  Strat 9  (Contrarian Cheap DCA)   — DCA the falling side at levels 0.40->0.10, hedge expensive at 0.25
  Strat 10 (Expensive-Side Momentum)— Buy when either side ask >= 0.80, hold to resolution
  Strat 11 (IPWDCA)                 — Two-sided symmetric DCA on both sides simultaneously
"""
import sqlite3
import numpy as np
from collections import defaultdict

DB = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300
SHARES = 100

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

conn = sqlite3.connect(DB)
rows = conn.execute(
    'SELECT unix_time, market_id, outcome, ask, bid, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn.close()

candles_raw = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, ask, bid, mid in rows:
    cs = (int(float(ts)) // INTERVAL) * INTERVAL
    candles_raw[(cs, mid_id)][out].append((float(ts), float(ask), float(bid), float(mid)))

ALL = []
for (cs, mid_id), sides in candles_raw.items():
    if sides['Up'] and sides['Down']:
        ALL.append((cs, sides['Up'], sides['Down']))

print(f"Total candles: {len(ALL)}\n")


def get_winner(up_ticks, dn_ticks):
    return 'Up' if up_ticks[-1][3] >= dn_ticks[-1][3] else 'Down'


def build_timeline(up_ticks, dn_ticks):
    return sorted(
        [(t, 'Up', a, b, m) for t, a, b, m in up_ticks] +
        [(t, 'Down', a, b, m) for t, a, b, m in dn_ticks]
    )


# ============================================================
# STRAT 8: Wait-for-Divergence (V8)
# Buy BOTH sides when either mid drops to 0.25
# Hold winner to $1.00, sell loser at mid <= 0.20
# ============================================================
def strat8_divergence():
    results = []
    SH = 100
    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)
        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0
        entered = False
        up_entry_ask = dn_entry_ask = 0
        up_exit_bid = dn_exit_bid = None

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            if last_up_mid == 0 or last_dn_mid == 0:
                continue

            # Entry: either side drops to 0.25
            if not entered and (last_up_mid <= 0.25 or last_dn_mid <= 0.25):
                up_entry_ask = last_up_ask
                dn_entry_ask = last_dn_ask
                entered = True

            if entered:
                if side == 'Up' and up_exit_bid is None and mid <= 0.20:
                    up_exit_bid = max(0, 2 * mid - ask)
                if side == 'Down' and dn_exit_bid is None and mid <= 0.20:
                    dn_exit_bid = max(0, 2 * mid - ask)

        if not entered:
            results.append(0.0)
            continue

        up_cost = up_entry_ask * SH + fee(SH, up_entry_ask)
        dn_cost = dn_entry_ask * SH + fee(SH, dn_entry_ask)

        if winner == 'Up':
            win_pnl = (1.0 * SH) - up_cost if up_exit_bid is None else (up_exit_bid * SH) - up_cost
            lose_pnl = (dn_exit_bid * SH - dn_cost) if dn_exit_bid is not None else (0 - dn_cost)
        else:
            win_pnl = (1.0 * SH) - dn_cost if dn_exit_bid is None else (dn_exit_bid * SH) - dn_cost
            lose_pnl = (up_exit_bid * SH - up_cost) if up_exit_bid is not None else (0 - up_cost)

        results.append(win_pnl + lose_pnl)
    return results


# ============================================================
# STRAT 9: Contrarian Cheap-Side DCA
# DCA the falling side at 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10
# Buy expensive side once at 0.25 trigger as hedge
# Hold everything to resolution
# ============================================================
def strat9_contrarian_dca():
    results = []
    SH = 20  # shares per DCA level
    LEVELS = [0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10]

    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)
        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0

        # Track which side is falling
        cheap_side = None
        cheap_entries = []  # (ask, shares)
        hedge_entry = None  # (ask, shares)
        levels_hit = set()
        prev_up_mid = prev_dn_mid = None
        prices_fresh = False

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            if last_up_mid == 0 or last_dn_mid == 0:
                continue

            if not prices_fresh:
                if side == 'Up': prev_up_mid = mid
                else: prev_dn_mid = mid
                if prev_up_mid and prev_dn_mid and 0.10 <= prev_up_mid <= 0.90 and 0.10 <= prev_dn_mid <= 0.90:
                    prices_fresh = True
                continue

            cur_mid = mid
            prev_mid = prev_up_mid if side == 'Up' else prev_dn_mid

            for lvl in LEVELS:
                if lvl in levels_hit:
                    continue
                if prev_mid is not None and prev_mid > lvl >= cur_mid:
                    levels_hit.add(lvl)
                    # Determine which side is cheap
                    if side == 'Up':
                        # Up is falling — buy Up (cheap), hedge with Down (expensive)
                        cheap_entries.append(('Up', last_up_ask))
                        if hedge_entry is None and lvl <= 0.25:
                            hedge_entry = ('Down', last_dn_ask)
                    else:
                        cheap_entries.append(('Down', last_dn_ask))
                        if hedge_entry is None and lvl <= 0.25:
                            hedge_entry = ('Up', last_up_ask)

            if side == 'Up': prev_up_mid = mid
            else: prev_dn_mid = mid

        if not cheap_entries:
            results.append(0.0)
            continue

        # Calculate PnL
        total_pnl = 0
        for entry_side, entry_ask in cheap_entries:
            cost = entry_ask * SH + fee(SH, entry_ask)
            if (entry_side == 'Up' and winner == 'Up') or (entry_side == 'Down' and winner == 'Down'):
                total_pnl += (1.0 * SH) - cost
            else:
                total_pnl += 0 - cost

        if hedge_entry:
            h_side, h_ask = hedge_entry
            h_cost = h_ask * SH + fee(SH, h_ask)
            if (h_side == 'Up' and winner == 'Up') or (h_side == 'Down' and winner == 'Down'):
                total_pnl += (1.0 * SH) - h_cost
            else:
                total_pnl += 0 - h_cost

        results.append(total_pnl)
    return results


# ============================================================
# STRAT 10: Expensive-Side Momentum
# When either side ask >= 0.80, buy it. Hold to resolution.
# One entry per candle.
# ============================================================
def strat10_expensive_momentum():
    results = []
    SH = 100

    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)
        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0
        entered = False
        entry_side = None
        entry_ask = 0

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            if entered:
                continue
            if last_up_mid == 0 or last_dn_mid == 0:
                continue

            if last_up_ask >= 0.80 and last_up_mid > last_dn_mid:
                entry_side = 'Up'; entry_ask = last_up_ask; entered = True
            elif last_dn_ask >= 0.80 and last_dn_mid > last_up_mid:
                entry_side = 'Down'; entry_ask = last_dn_ask; entered = True

        if not entered:
            results.append(0.0)
            continue

        cost = entry_ask * SH + fee(SH, entry_ask)
        if entry_side == winner:
            pnl = (1.0 * SH) - cost
        else:
            pnl = 0 - cost
        results.append(pnl)
    return results


# ============================================================
# STRAT 11: IPWDCA (Two-sided symmetric DCA)
# DCA both sides equally at levels 0.40, 0.35, 0.30, 0.25, 0.20
# Hold everything to resolution. No hedge, no exit.
# ============================================================
def strat11_ipwdca():
    results = []
    SH = 20
    LEVELS = [0.40, 0.35, 0.30, 0.25, 0.20]

    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)
        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0

        up_levels_hit = set()
        dn_levels_hit = set()
        up_entries = []  # list of ask prices
        dn_entries = []
        prev_up_mid = prev_dn_mid = None
        prices_fresh = False

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            if last_up_mid == 0 or last_dn_mid == 0:
                continue

            if not prices_fresh:
                if side == 'Up': prev_up_mid = mid
                else: prev_dn_mid = mid
                if prev_up_mid and prev_dn_mid and 0.10 <= prev_up_mid <= 0.90 and 0.10 <= prev_dn_mid <= 0.90:
                    prices_fresh = True
                continue

            # Check crossovers for Up side
            if side == 'Up' and prev_up_mid is not None:
                for lvl in LEVELS:
                    if lvl not in up_levels_hit and prev_up_mid > lvl >= mid:
                        up_levels_hit.add(lvl)
                        up_entries.append(last_up_ask)

            # Check crossovers for Down side
            if side == 'Down' and prev_dn_mid is not None:
                for lvl in LEVELS:
                    if lvl not in dn_levels_hit and prev_dn_mid > lvl >= mid:
                        dn_levels_hit.add(lvl)
                        dn_entries.append(last_dn_ask)

            if side == 'Up': prev_up_mid = mid
            else: prev_dn_mid = mid

        if not up_entries and not dn_entries:
            results.append(0.0)
            continue

        total_pnl = 0
        for ask_price in up_entries:
            cost = ask_price * SH + fee(SH, ask_price)
            if winner == 'Up':
                total_pnl += (1.0 * SH) - cost
            else:
                total_pnl += 0 - cost

        for ask_price in dn_entries:
            cost = ask_price * SH + fee(SH, ask_price)
            if winner == 'Down':
                total_pnl += (1.0 * SH) - cost
            else:
                total_pnl += 0 - cost

        results.append(total_pnl)
    return results


def report(name, results):
    n = len(results)
    active = [r for r in results if r != 0]
    n_active = len(active)
    if n_active == 0:
        print(f"=== {name} ===  NO TRADES\n")
        return
    wins = sum(1 for r in active if r > 0)
    losses = n_active - wins
    net = sum(results)
    avg_win = sum(r for r in active if r > 0) / max(wins, 1)
    avg_loss = sum(r for r in active if r <= 0) / max(losses, 1)
    worst = min(active)
    best = max(active)
    avg_cost_approx = sum(abs(r) for r in active) / n_active

    print(f"=== {name} ===")
    print(f"  Candles: {n} total, {n_active} active ({100*n_active/n:.0f}%)")
    print(f"  WR: {100*wins/n_active:.1f}% ({wins}/{n_active})")
    print(f"  Net PnL: ${net:+,.2f} | $/candle (all): ${net/n:+.2f} | $/candle (active): ${net/n_active:+.2f}")
    print(f"  Avg win: ${avg_win:+.2f} | Avg loss: ${avg_loss:+.2f}")
    print(f"  Best: ${best:+.2f} | Worst: ${worst:+.2f}")
    print(f"  Daily est (288 candles): ${net/n*288:+,.0f}")
    print()


r8 = strat8_divergence()
r9 = strat9_contrarian_dca()
r10 = strat10_expensive_momentum()
r11 = strat11_ipwdca()

report("Strat 8 (Wait-for-Divergence)", r8)
report("Strat 9 (Contrarian Cheap DCA)", r9)
report("Strat 10 (Expensive-Side Momentum)", r10)
report("Strat 11 (IPWDCA — Symmetric DCA)", r11)
