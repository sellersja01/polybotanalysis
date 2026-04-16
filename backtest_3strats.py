import sqlite3
from collections import defaultdict

DB = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

conn = sqlite3.connect(DB)
rows = conn.execute(
    'SELECT unix_time, market_id, outcome, ask, bid, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn.close()

candles = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, ask, bid, mid in rows:
    cs = (int(float(ts)) // INTERVAL) * INTERVAL
    candles[(cs, mid_id)][out].append((float(ts), float(ask), float(bid), float(mid)))

ALL = []
for (cs, mid_id), sides in candles.items():
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
# STRATEGY 1: Resolution Scalp (enter at t+240s, buy leading side >= 0.85)
# ============================================================
def strat1_resolution_scalp():
    results = []
    SHARES = 100
    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)
        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0
        entered = False

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            offset = t - cs
            if offset < 240 or entered:
                continue
            if last_up_mid == 0 or last_dn_mid == 0:
                continue

            if last_up_mid >= 0.85 and last_up_mid > last_dn_mid:
                entry_side = 'Up'; entry_ask = last_up_ask; entered = True
            elif last_dn_mid >= 0.85 and last_dn_mid > last_up_mid:
                entry_side = 'Down'; entry_ask = last_dn_ask; entered = True

        if not entered:
            results.append(0.0)
            continue

        cost = entry_ask * SHARES + fee(SHARES, entry_ask)
        if entry_side == winner:
            pnl = (1.0 * SHARES) - cost
        else:
            pnl = 0 - cost
        results.append(pnl)

    return results


# ============================================================
# STRATEGY 2: Both-sides DCA + Late Flood (Galindrast/W7 style)
# ============================================================
def strat2_dca_flood():
    results = []
    BASE = 10
    DCA_INTERVAL = 15

    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)

        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0
        up_cost = dn_cost = 0.0
        up_shares = dn_shares = 0
        last_buy_time = 0
        up_sold = dn_sold = False
        up_sell_rev = dn_sell_rev = 0.0

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            offset = t - cs
            if last_up_mid == 0 or last_dn_mid == 0:
                continue
            if offset < 5:
                continue

            if t - last_buy_time >= DCA_INTERVAL:
                last_buy_time = t
                leading = max(last_up_mid, last_dn_mid)
                if leading >= 0.90:
                    scale = 8
                elif leading >= 0.80:
                    scale = 5
                elif leading >= 0.70:
                    scale = 3
                else:
                    scale = 1
                shares = BASE * scale

                if leading >= 0.70:
                    if last_up_mid > last_dn_mid:
                        if not up_sold:
                            up_cost += last_up_ask * shares + fee(shares, last_up_ask)
                            up_shares += shares
                    else:
                        if not dn_sold:
                            dn_cost += last_dn_ask * shares + fee(shares, last_dn_ask)
                            dn_shares += shares
                else:
                    if not up_sold:
                        up_cost += last_up_ask * shares + fee(shares, last_up_ask)
                        up_shares += shares
                    if not dn_sold:
                        dn_cost += last_dn_ask * shares + fee(shares, last_dn_ask)
                        dn_shares += shares

                if last_up_mid <= 0.20 and up_shares > 0 and not up_sold:
                    up_sell_rev = max(0, 2 * last_up_mid - last_up_ask) * up_shares
                    up_sold = True
                if last_dn_mid <= 0.20 and dn_shares > 0 and not dn_sold:
                    dn_sell_rev = max(0, 2 * last_dn_mid - last_dn_ask) * dn_shares
                    dn_sold = True

        if up_shares == 0 and dn_shares == 0:
            results.append(0.0)
            continue

        if winner == 'Up':
            win_pnl = (1.0 * up_shares - up_cost) if not up_sold else (up_sell_rev - up_cost)
            lose_pnl = (dn_sell_rev - dn_cost) if dn_sold else (0 - dn_cost)
        else:
            win_pnl = (1.0 * dn_shares - dn_cost) if not dn_sold else (dn_sell_rev - dn_cost)
            lose_pnl = (up_sell_rev - up_cost) if up_sold else (0 - up_cost)

        results.append(win_pnl + lose_pnl)

    return results


# ============================================================
# STRATEGY 3: Pure Late-Candle Scalp (one trade at t+240s, buy leader)
# ============================================================
def strat3_pure_late():
    results = []
    SHARES = 100

    for cs, up_ticks, dn_ticks in ALL:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = build_timeline(up_ticks, dn_ticks)

        last_up_mid = last_up_ask = last_dn_mid = last_dn_ask = 0
        entered = False

        for t, side, ask, bid, mid in all_t:
            if side == 'Up':
                last_up_mid = mid; last_up_ask = ask
            else:
                last_dn_mid = mid; last_dn_ask = ask

            offset = t - cs
            if offset < 240 or entered:
                continue
            if last_up_mid == 0 or last_dn_mid == 0:
                continue

            if last_up_mid > last_dn_mid:
                entry_side = 'Up'; entry_ask = last_up_ask
            elif last_dn_mid > last_up_mid:
                entry_side = 'Down'; entry_ask = last_dn_ask
            else:
                continue
            entered = True

        if not entered:
            results.append(0.0)
            continue

        cost = entry_ask * SHARES + fee(SHARES, entry_ask)
        if entry_side == winner:
            pnl = (1.0 * SHARES) - cost
        else:
            pnl = 0 - cost
        results.append(pnl)

    return results


def report(name, results):
    n = len(results)
    active = [r for r in results if r != 0]
    n_active = len(active)
    wins = sum(1 for r in active if r > 0)
    losses = n_active - wins
    net = sum(results)
    avg_win = sum(r for r in active if r > 0) / max(wins, 1)
    avg_loss = sum(r for r in active if r <= 0) / max(losses, 1)
    worst = min(results) if results else 0

    print(f"=== {name} ===")
    print(f"  Candles: {n} total, {n_active} with trades ({100*n_active/n:.0f}%)")
    print(f"  WR: {100*wins/max(n_active,1):.1f}% ({wins}/{n_active})")
    print(f"  Net PnL: ${net:+.2f}")
    print(f"  $/candle (all): ${net/n:+.2f}")
    print(f"  $/candle (active): ${net/max(n_active,1):+.2f}")
    print(f"  Avg win: ${avg_win:+.2f} | Avg loss: ${avg_loss:+.2f}")
    print(f"  Worst: ${worst:+.2f}")
    print(f"  Daily est (288 candles): ${net/n*288:+.0f}")
    print()


r1 = strat1_resolution_scalp()
r2 = strat2_dca_flood()
r3 = strat3_pure_late()

report("Strat 1: Resolution Scalp (t+240s, leading >= 0.85, 100sh)", r1)
report("Strat 2: Both-Sides DCA + Flood (W7 style, 10sh base)", r2)
report("Strat 3: Pure Late Scalp (t+240s, buy leader ANY price, 100sh)", r3)
