"""
Run all 5 top strategies on RECENT VPS data (Apr 5, ~3.5h)
Uses native price feeds from each market's own asset_price table.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

BASE = r'C:\Users\James\polybotanalysis\recent_data'

MARKETS = {
    'BTC_5m':  (f'{BASE}/market_btc_5m.db',  300),
    'BTC_15m': (f'{BASE}/market_btc_15m.db', 900),
    'ETH_5m':  (f'{BASE}/market_eth_5m.db',  300),
    'ETH_15m': (f'{BASE}/market_eth_15m.db', 900),
    'SOL_5m':  (f'{BASE}/market_sol_5m.db',  300),
    'SOL_15m': (f'{BASE}/market_sol_15m.db', 900),
    'XRP_5m':  (f'{BASE}/market_xrp_5m.db',  300),
    'XRP_15m': (f'{BASE}/market_xrp_15m.db', 900),
}

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

SH = 100


def load_data(db_path, interval):
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    has_prices = 'asset_price' in tables
    prices = None
    if has_prices:
        pr = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
        if pr:
            prices = {
                't': np.array([float(r[0]) for r in pr]),
                'p': np.array([float(r[1]) for r in pr]),
            }

    odds = conn.execute(
        'SELECT unix_time, market_id, outcome, ask, bid, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()

    # Build candles
    candles_raw = defaultdict(lambda: {'Up': [], 'Down': []})
    up_all = []; dn_all = []
    for ts, mid_id, out, ask, bid, mid in odds:
        cs = (int(float(ts)) // interval) * interval
        row = (float(ts), float(ask), float(bid), float(mid))
        candles_raw[(cs, mid_id)][out].append(row)
        if out == 'Up': up_all.append(row)
        else: dn_all.append(row)

    candles = []
    for (cs, mid_id), sides in candles_raw.items():
        if sides['Up'] and sides['Down']:
            candles.append((cs, sides['Up'], sides['Down']))

    return candles, prices, up_all, dn_all


def get_winner(up_ticks, dn_ticks):
    return 'Up' if up_ticks[-1][3] >= dn_ticks[-1][3] else 'Down'


# ── STRAT 1: Resolution Scalp ──
def run_s1(candles, interval):
    results = []
    t_entry = int(interval * 0.8)
    for cs, up_t, dn_t in candles:
        winner = get_winner(up_t, dn_t)
        all_t = sorted(
            [(t, 'Up', a, b, m) for t, a, b, m in up_t] +
            [(t, 'Down', a, b, m) for t, a, b, m in dn_t]
        )
        lu = la = ld = lda = 0; entered = False
        for t, s, a, b, m in all_t:
            if s == 'Up': lu = m; la = a
            else: ld = m; lda = a
            if t - cs < t_entry or entered or lu == 0 or ld == 0: continue
            if lu >= 0.85 and lu > ld:
                es = 'Up'; ea = la; entered = True
            elif ld >= 0.85 and ld > lu:
                es = 'Down'; ea = lda; entered = True
        if not entered: results.append(0.0); continue
        cost = ea * SH + fee(SH, ea)
        results.append((1.0*SH - cost) if es == winner else (0 - cost))
    return results


# ── STRAT 5: Latency Arb ──
def run_s5(candles, prices, up_all, dn_all, interval):
    if prices is None or len(prices['t']) < 20: return []
    sample_t = np.arange(prices['t'][0], prices['t'][-1], 1.0)
    btc_1s = np.interp(sample_t, prices['t'], prices['p'])

    up_t = np.array([r[0] for r in up_all]); up_a = np.array([r[1] for r in up_all]); up_m = np.array([r[3] for r in up_all])
    dn_t = np.array([r[0] for r in dn_all]); dn_a = np.array([r[1] for r in dn_all]); dn_m = np.array([r[3] for r in dn_all])

    cw = {}
    for cs, ut, dt in candles:
        cw[cs] = get_winner(ut, dt)

    trades = []; last = 0
    for i in range(15, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last < 30: continue
        move = (btc_1s[i] - btc_1s[i-15]) / btc_1s[i-15] * 100
        if abs(move) < 0.05: continue
        d = 'up' if move > 0 else 'down'
        ui = bisect_right(up_t, t_now) - 1
        di = bisect_right(dn_t, t_now) - 1
        if ui < 0 or di < 0: continue
        if d == 'up': ask = float(up_a[ui]); mid = float(up_m[ui])
        else: ask = float(dn_a[di]); mid = float(dn_m[di])
        if ask <= 0.01 or ask > 0.90 or mid > 0.55: continue
        cs = (int(t_now) // interval) * interval
        w = cw.get(cs)
        if w is None: continue
        cost = ask * SH + fee(SH, ask)
        if (d == 'up' and w == 'Up') or (d == 'down' and w == 'Down'):
            trades.append(1.0*SH - cost)
        else:
            trades.append(0 - cost)
        last = t_now
    return trades


# ── STRAT 7: Candle Momentum ──
def run_s7(candles, prices, up_all, dn_all, interval):
    if prices is None or len(prices['t']) < 20: return []
    sample_t = np.arange(prices['t'][0], prices['t'][-1], 1.0)
    btc_1s = np.interp(sample_t, prices['t'], prices['p'])

    candle_opens = {}
    for i, ts in enumerate(prices['t']):
        cs = (int(ts) // interval) * interval
        if cs not in candle_opens:
            candle_opens[cs] = float(prices['p'][i])

    up_t = np.array([r[0] for r in up_all]); up_a = np.array([r[1] for r in up_all]); up_m = np.array([r[3] for r in up_all])
    dn_t = np.array([r[0] for r in dn_all]); dn_a = np.array([r[1] for r in dn_all]); dn_m = np.array([r[3] for r in dn_all])

    cw = {}
    for cs, ut, dt in candles:
        cw[cs] = get_winner(ut, dt)

    trades = []; last = 0
    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last < 30: continue
        cs = (int(t_now) // interval) * interval
        offset = t_now - cs
        if offset < 15 or offset > interval - 30: continue
        op = candle_opens.get(cs)
        if op is None: continue
        move = (btc_1s[i] - op) / op * 100
        if abs(move) < 0.05: continue
        d = 'up' if move > 0 else 'down'
        ui = bisect_right(up_t, t_now) - 1
        di = bisect_right(dn_t, t_now) - 1
        if ui < 0 or di < 0: continue
        if d == 'up': ask = float(up_a[ui]); mid = float(up_m[ui])
        else: ask = float(dn_a[di]); mid = float(dn_m[di])
        if ask <= 0.01 or ask > 0.90 or mid > 0.55: continue
        w = cw.get(cs)
        if w is None: continue
        cost = ask * SH + fee(SH, ask)
        if (d == 'up' and w == 'Up') or (d == 'down' and w == 'Down'):
            trades.append(1.0*SH - cost)
        else:
            trades.append(0 - cost)
        last = t_now
    return trades


# ── STRAT 8: Wait-for-Divergence ──
def run_s8(candles, interval):
    results = []
    BUDGET = 100.0
    for cs, up_t, dn_t in candles:
        winner = get_winner(up_t, dn_t)
        all_t = sorted(
            [(t, 'Up', a, b, m) for t, a, b, m in up_t] +
            [(t, 'Down', a, b, m) for t, a, b, m in dn_t]
        )
        lu = la = ld = lda = 0; entered = False
        ua = da = 0; ue = de = None
        for t, s, a, b, m in all_t:
            if s == 'Up': lu = m; la = a
            else: ld = m; lda = a
            if lu == 0 or ld == 0: continue
            if not entered and (lu <= 0.25 or ld <= 0.25):
                ua = la; da = lda; entered = True
            if entered:
                if s == 'Up' and ue is None and m <= 0.20: ue = max(0, 2*m - a)
                if s == 'Down' and de is None and m <= 0.20: de = max(0, 2*m - a)
        if not entered: results.append(0.0); continue
        cpp = ua + da + fee(1, ua) + fee(1, da)
        sh = int(BUDGET / cpp) if cpp > 0 else 0
        if sh < 1: results.append(0.0); continue
        uc = ua*sh + fee(sh, ua); dc = da*sh + fee(sh, da)
        if winner == 'Up':
            wp = (1.0*sh - uc) if ue is None else (ue*sh - uc)
            lp = (de*sh - dc) if de is not None else (0 - dc)
        else:
            wp = (1.0*sh - dc) if de is None else (de*sh - dc)
            lp = (ue*sh - uc) if ue is not None else (0 - uc)
        results.append(wp + lp)
    return results


# ── STRAT 10: Expensive Momentum ──
def run_s10(candles, interval):
    results = []
    for cs, up_t, dn_t in candles:
        winner = get_winner(up_t, dn_t)
        all_t = sorted(
            [(t, 'Up', a, b, m) for t, a, b, m in up_t] +
            [(t, 'Down', a, b, m) for t, a, b, m in dn_t]
        )
        lu = la = ld = lda = 0; entered = False
        for t, s, a, b, m in all_t:
            if s == 'Up': lu = m; la = a
            else: ld = m; lda = a
            if entered or lu == 0 or ld == 0: continue
            if la >= 0.80 and lu > ld:
                es = 'Up'; ea = la; entered = True
            elif lda >= 0.80 and ld > lu:
                es = 'Down'; ea = lda; entered = True
        if not entered: results.append(0.0); continue
        cost = ea * SH + fee(SH, ea)
        results.append((1.0*SH - cost) if es == winner else (0 - cost))
    return results


# ── RUN ──
print(f"{'Market':<10} {'Strat':<28} {'Candles':>8} {'Active':>7} {'WR%':>6} {'$/candle':>9} {'Daily':>8} {'Worst':>8}")
print("-" * 90)

for label, (db_path, interval) in MARKETS.items():
    candles, prices, up_all, dn_all = load_data(db_path, interval)
    if not candles:
        print(f"{label:<10} NO DATA")
        continue

    all_ts = []
    for cs, u, d in candles:
        all_ts.extend([t for t, a, b, m in u])
        all_ts.extend([t for t, a, b, m in d])
    hours = (max(all_ts) - min(all_ts)) / 3600 if all_ts else 1

    for name, func in [
        ("S1 (Resolution Scalp)", lambda: run_s1(candles, interval)),
        ("S5 (Latency Arb)", lambda: run_s5(candles, prices, up_all, dn_all, interval)),
        ("S7 (Candle Momentum)", lambda: run_s7(candles, prices, up_all, dn_all, interval)),
        ("S8 (Divergence)", lambda: run_s8(candles, interval)),
        ("S10 (Expensive Mom.)", lambda: run_s10(candles, interval)),
    ]:
        r = func()
        if not r:
            print(f"{label:<10} {name:<28} {'N/A':>8}")
            continue
        active = [x for x in r if x != 0]
        if not active:
            print(f"{label:<10} {name:<28} {len(r):>8} {'0':>7}")
            continue
        n_a = len(active)
        wins = sum(1 for x in active if x > 0)
        net = sum(r)
        daily = net / hours * 24
        worst = min(active)
        print(f"{label:<10} {name:<28} {len(r):>8} {n_a:>7} {100*wins/n_a:>5.1f}% {net/n_a:>+8.2f} {daily:>+8.0f} {worst:>+8.2f}")

    print()

print("NOTE: This is ~3.5 hours of data from Apr 5 evening. Small sample.")
