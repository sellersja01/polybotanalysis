"""
Run the top strategies across ALL 8 markets:
  BTC 5m/15m, ETH 5m/15m, SOL 5m/15m, XRP 5m/15m

Strategies tested:
  Strat 1  (Resolution Scalp)     — t+240s, buy leader >= 0.85
  Strat 5  (Latency Arb)          — Coinbase move 0.05% in 15s, Poly stale
  Strat 7  (Candle Momentum)      — Coinbase moved vs candle open >= 0.05%
  Strat 8  (Wait-for-Divergence)  — Buy both when either mid <= 0.25
  Strat 10 (Expensive Momentum)   — Buy when ask >= 0.80
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

DBS = {
    'BTC_5m':  ('market_btc_5m.db',  300),
    'BTC_15m': ('market_btc_15m.db', 900),
    'ETH_5m':  ('market_eth_5m.db',  300),
    'ETH_15m': ('market_eth_15m.db', 900),
    'SOL_5m':  ('market_sol_5m.db',  300),
    'SOL_15m': ('market_sol_15m.db', 900),
    'XRP_5m':  ('market_xrp_5m.db',  300),
    'XRP_15m': ('market_xrp_15m.db', 900),
}

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

def poly_fee_new(price):
    return price * 0.072 * (price * (1 - price))


def load_market(db_file, interval):
    try:
        conn = sqlite3.connect(db_file)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        odds_rows = conn.execute(
            'SELECT unix_time, market_id, outcome, ask, bid, mid FROM polymarket_odds '
            'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
        ).fetchall()

        has_prices = 'asset_price' in tables
        btc_rows = []
        if has_prices:
            btc_rows = conn.execute(
                'SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time'
            ).fetchall()
        conn.close()

        candles_raw = defaultdict(lambda: {'Up': [], 'Down': []})
        for ts, mid_id, out, ask, bid, mid in odds_rows:
            cs = (int(float(ts)) // interval) * interval
            candles_raw[(cs, mid_id)][out].append((float(ts), float(ask), float(bid), float(mid)))

        candles = []
        for (cs, mid_id), sides in candles_raw.items():
            if sides['Up'] and sides['Down']:
                candles.append((cs, sides['Up'], sides['Down']))

        prices = None
        if btc_rows:
            prices = {
                't': np.array([float(r[0]) for r in btc_rows]),
                'p': np.array([float(r[1]) for r in btc_rows]),
            }

        return candles, prices
    except Exception as e:
        return [], None


def get_winner(up_ticks, dn_ticks):
    return 'Up' if up_ticks[-1][3] >= dn_ticks[-1][3] else 'Down'


# ── STRAT 1: Resolution Scalp ────────────────────────────────
def strat1(candles, interval):
    results = []
    SH = 100
    t_entry = int(interval * 0.8)  # 80% into candle (240s for 5m, 720s for 15m)
    for cs, up_ticks, dn_ticks in candles:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = sorted(
            [(t, 'Up', a, b, m) for t, a, b, m in up_ticks] +
            [(t, 'Down', a, b, m) for t, a, b, m in dn_ticks]
        )
        lu_m = lu_a = ld_m = ld_a = 0
        entered = False
        for t, side, ask, bid, mid in all_t:
            if side == 'Up': lu_m = mid; lu_a = ask
            else: ld_m = mid; ld_a = ask
            if t - cs < t_entry or entered or lu_m == 0 or ld_m == 0:
                continue
            if lu_m >= 0.85 and lu_m > ld_m:
                es = 'Up'; ea = lu_a; entered = True
            elif ld_m >= 0.85 and ld_m > lu_m:
                es = 'Down'; ea = ld_a; entered = True
        if not entered:
            results.append(0.0)
            continue
        cost = ea * SH + fee(SH, ea)
        results.append((1.0 * SH - cost) if es == winner else (0 - cost))
    return results


# ── STRAT 5: Latency Arb ─────────────────────────────────────
def strat5(candles, prices, interval):
    if prices is None:
        return []
    results = []
    SH = 100

    btc_t = prices['t']; btc_p = prices['p']
    if len(btc_t) < 20:
        return []
    sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
    btc_1s = np.interp(sample_t, btc_t, btc_p)

    # Build up/dn arrays
    up_t_arr = []; up_m_arr = []; up_a_arr = []
    dn_t_arr = []; dn_m_arr = []; dn_a_arr = []
    for cs, up_ticks, dn_ticks in candles:
        for t, a, b, m in up_ticks:
            up_t_arr.append(t); up_m_arr.append(m); up_a_arr.append(a)
        for t, a, b, m in dn_ticks:
            dn_t_arr.append(t); dn_m_arr.append(m); dn_a_arr.append(a)
    up_t_np = np.array(up_t_arr); up_m_np = np.array(up_m_arr); up_a_np = np.array(up_a_arr)
    dn_t_np = np.array(dn_t_arr); dn_m_np = np.array(dn_m_arr); dn_a_np = np.array(dn_a_arr)

    # candle winners
    cw = {}
    for cs, up_ticks, dn_ticks in candles:
        cw[cs] = get_winner(up_ticks, dn_ticks)

    trades = []
    last_entry = 0
    LOOKBACK = 15

    for i in range(LOOKBACK, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < 30:
            continue
        move = (btc_1s[i] - btc_1s[i - LOOKBACK]) / btc_1s[i - LOOKBACK] * 100
        if abs(move) < 0.05:
            continue
        direction = 'up' if move > 0 else 'down'

        ui = bisect_right(up_t_np, t_now) - 1
        di = bisect_right(dn_t_np, t_now) - 1
        if ui < 0 or di < 0:
            continue
        if direction == 'up':
            ask = up_a_np[ui]; mid = up_m_np[ui]
        else:
            ask = dn_a_np[di]; mid = dn_m_np[di]
        if ask <= 0.01 or ask > 0.90 or mid > 0.55:
            continue

        cs = (int(t_now) // interval) * interval
        winner = cw.get(cs)
        if winner is None:
            continue

        cost = ask * SH + fee(SH, float(ask))
        if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
            pnl = (1.0 * SH) - cost
        else:
            pnl = 0 - cost
        trades.append(pnl)
        last_entry = t_now

    return trades


# ── STRAT 7: Candle Momentum ─────────────────────────────────
def strat7(candles, prices, interval):
    if prices is None:
        return []
    btc_t = prices['t']; btc_p = prices['p']
    if len(btc_t) < 20:
        return []
    sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
    btc_1s = np.interp(sample_t, btc_t, btc_p)

    candle_opens = {}
    for i, ts in enumerate(btc_t):
        cs = (int(ts) // interval) * interval
        if cs not in candle_opens:
            candle_opens[cs] = float(btc_p[i])

    up_t_arr = []; up_m_arr = []; up_a_arr = []
    dn_t_arr = []; dn_m_arr = []; dn_a_arr = []
    for cs, up_ticks, dn_ticks in candles:
        for t, a, b, m in up_ticks:
            up_t_arr.append(t); up_m_arr.append(m); up_a_arr.append(a)
        for t, a, b, m in dn_ticks:
            dn_t_arr.append(t); dn_m_arr.append(m); dn_a_arr.append(a)
    up_t_np = np.array(up_t_arr); up_m_np = np.array(up_m_arr); up_a_np = np.array(up_a_arr)
    dn_t_np = np.array(dn_t_arr); dn_m_np = np.array(dn_m_arr); dn_a_np = np.array(dn_a_arr)

    cw = {}
    for cs, up_ticks, dn_ticks in candles:
        cw[cs] = get_winner(up_ticks, dn_ticks)

    trades = []
    last_entry = 0
    SH = 100

    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < 30:
            continue
        cs = (int(t_now) // interval) * interval
        offset = t_now - cs
        if offset < 15 or offset > interval - 30:
            continue
        op = candle_opens.get(cs)
        if op is None:
            continue
        move = (btc_1s[i] - op) / op * 100
        if abs(move) < 0.05:
            continue
        direction = 'up' if move > 0 else 'down'

        ui = bisect_right(up_t_np, t_now) - 1
        di = bisect_right(dn_t_np, t_now) - 1
        if ui < 0 or di < 0:
            continue
        if direction == 'up':
            ask = up_a_np[ui]; mid = up_m_np[ui]
        else:
            ask = dn_a_np[di]; mid = dn_m_np[di]
        if ask <= 0.01 or ask > 0.90 or mid > 0.55:
            continue

        winner = cw.get(cs)
        if winner is None:
            continue

        cost = ask * SH + fee(SH, float(ask))
        if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
            pnl = (1.0 * SH) - cost
        else:
            pnl = 0 - cost
        trades.append(pnl)
        last_entry = t_now

    return trades


# ── STRAT 8: Wait-for-Divergence ─────────────────────────────
def strat8(candles, interval):
    results = []
    BUDGET = 100.0
    for cs, up_ticks, dn_ticks in candles:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = sorted(
            [(t, 'Up', a, b, m) for t, a, b, m in up_ticks] +
            [(t, 'Down', a, b, m) for t, a, b, m in dn_ticks]
        )
        lu_m = lu_a = ld_m = ld_a = 0
        entered = False
        ua = da = 0
        ue = de = None
        for t, side, ask, bid, mid in all_t:
            if side == 'Up': lu_m = mid; lu_a = ask
            else: ld_m = mid; ld_a = ask
            if lu_m == 0 or ld_m == 0: continue
            if not entered and (lu_m <= 0.25 or ld_m <= 0.25):
                ua = lu_a; da = ld_a; entered = True
            if entered:
                if side == 'Up' and ue is None and mid <= 0.20:
                    ue = max(0, 2*mid - ask)
                if side == 'Down' and de is None and mid <= 0.20:
                    de = max(0, 2*mid - ask)
        if not entered:
            results.append(0.0); continue
        cpp = ua + da + fee(1, ua) + fee(1, da)
        sh = int(BUDGET / cpp) if cpp > 0 else 0
        if sh < 1:
            results.append(0.0); continue
        uc = ua * sh + fee(sh, ua)
        dc = da * sh + fee(sh, da)
        if winner == 'Up':
            wp = (1.0*sh - uc) if ue is None else (ue*sh - uc)
            lp = (de*sh - dc) if de is not None else (0 - dc)
        else:
            wp = (1.0*sh - dc) if de is None else (de*sh - dc)
            lp = (ue*sh - uc) if ue is not None else (0 - uc)
        results.append(wp + lp)
    return results


# ── STRAT 10: Expensive-Side Momentum ────────────────────────
def strat10(candles, interval):
    results = []
    SH = 100
    for cs, up_ticks, dn_ticks in candles:
        winner = get_winner(up_ticks, dn_ticks)
        all_t = sorted(
            [(t, 'Up', a, b, m) for t, a, b, m in up_ticks] +
            [(t, 'Down', a, b, m) for t, a, b, m in dn_ticks]
        )
        lu_m = lu_a = ld_m = ld_a = 0
        entered = False
        for t, side, ask, bid, mid in all_t:
            if side == 'Up': lu_m = mid; lu_a = ask
            else: ld_m = mid; ld_a = ask
            if entered or lu_m == 0 or ld_m == 0: continue
            if lu_a >= 0.80 and lu_m > ld_m:
                es = 'Up'; ea = lu_a; entered = True
            elif ld_a >= 0.80 and ld_m > lu_m:
                es = 'Down'; ea = ld_a; entered = True
        if not entered:
            results.append(0.0); continue
        cost = ea * SH + fee(SH, ea)
        results.append((1.0*SH - cost) if es == winner else (0 - cost))
    return results


# ── Run everything ───────────────────────────────────────────
def summarize(results, hours):
    active = [r for r in results if r != 0]
    if not active:
        return None
    n_active = len(active)
    wins = sum(1 for r in active if r > 0)
    net = sum(results)
    daily = net / max(hours, 1) * 24
    return {
        'n': len(results), 'active': n_active, 'wins': wins,
        'wr': 100*wins/n_active, 'net': net, 'daily': daily,
        'ppc': net/n_active, 'worst': min(active),
    }


print(f"{'Market':<10} {'Strat':<28} {'Candles':>8} {'Active':>7} {'WR%':>6} {'$/candle':>9} {'Daily':>8} {'Worst':>8}")
print("-" * 95)

for label, (db_file, interval) in DBS.items():
    candles, prices = load_market(db_file, interval)
    if not candles:
        print(f"{label:<10} NO DATA")
        continue

    hours = 0
    if candles:
        all_ts = []
        for cs, up, dn in candles:
            all_ts.extend([t for t, a, b, m in up])
            all_ts.extend([t for t, a, b, m in dn])
        if all_ts:
            hours = (max(all_ts) - min(all_ts)) / 3600

    for name, func in [
        ("Strat 1 (Res. Scalp)", lambda: strat1(candles, interval)),
        ("Strat 5 (Latency Arb)", lambda: strat5(candles, prices, interval)),
        ("Strat 7 (Candle Mom.)", lambda: strat7(candles, prices, interval)),
        ("Strat 8 (Divergence)", lambda: strat8(candles, interval)),
        ("Strat 10 (Exp. Mom.)", lambda: strat10(candles, interval)),
    ]:
        r = func()
        if not r:
            print(f"{label:<10} {name:<28} {'N/A':>8}")
            continue
        s = summarize(r, hours)
        if s is None:
            print(f"{label:<10} {name:<28} {len(r):>8} {'0':>7}")
            continue
        print(f"{label:<10} {name:<28} {s['n']:>8} {s['active']:>7} {s['wr']:>5.1f}% {s['ppc']:>+8.2f} {s['daily']:>+8.0f} {s['worst']:>+8.2f}")

    print()
