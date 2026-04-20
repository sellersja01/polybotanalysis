"""Backtest S13 (First CEX Move) on fresh 269h data."""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

BASE = 'hetzner_apr19'
INTERVAL = 300
MOVE_THRESH = 0.03
SH = 100

def fee(p): return p * 0.072 * (p * (1-p))

def get_winner(sides):
    """Use Polymarket final tick to determine winner."""
    if not sides['Up'] or not sides['Down']: return None
    # Get last tick's bid/ask for each side
    up_last = sides['Up'][-1]  # (ts, ask, bid, mid)
    dn_last = sides['Down'][-1]
    up_ask, up_bid = up_last[1], up_last[2]
    dn_ask, dn_bid = dn_last[1], dn_last[2]
    # Zero-ask rule
    if up_ask == 0 and dn_ask > 0: return 'Up'
    if dn_ask == 0 and up_ask > 0: return 'Down'
    # Fallback: higher mid
    um = (up_bid + up_ask) / 2
    dm = (dn_bid + dn_ask) / 2
    return 'Up' if um >= dm else 'Down'

MARKETS = [
    ('BTC', f'{BASE}/market_btc_5m.db'),
    ('ETH', f'{BASE}/market_eth_5m.db'),
    ('SOL', f'{BASE}/market_sol_5m.db'),
    ('XRP', f'{BASE}/market_xrp_5m.db'),
]

print(f"S13 First CEX Move (0.03%) — 269h fresh data (Apr 8-19)")
print(f"Winner: zero-ask rule + higher mid fallback")
print()
print(f"{'Market':<8} {'Hours':<7} {'Trades':<8} {'Wins':<6} {'WR%':<7} {'Avg win':<10} {'Avg loss':<10} {'Net PnL':<12} {'$/day':<10}")
print("-" * 90)

grand = 0
for asset, db_path in MARKETS:
    conn = sqlite3.connect(db_path)
    pr = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
    odds = conn.execute(
        'SELECT unix_time, outcome, ask, bid, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time'
    ).fetchall()
    conn.close()

    pt = np.array([float(r[0]) for r in pr])
    pp = np.array([float(r[1]) for r in pr])
    hours = (pt[-1] - pt[0]) / 3600

    st = np.arange(pt[0], pt[-1], 1.0)
    p1s = np.interp(st, pt, pp)

    co = {}
    for i, ts in enumerate(pt):
        cs = (int(ts) // INTERVAL) * INTERVAL
        if cs not in co: co[cs] = float(pp[i])

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    up_t=[]; up_a=[]; up_m=[]; dn_t=[]; dn_a=[]; dn_m=[]
    for ts, out, ask, bid, mid in odds:
        cs = (int(float(ts)) // INTERVAL) * INTERVAL
        candles[cs][out].append((float(ts), float(ask), float(bid), float(mid)))
        if out == 'Up':
            up_t.append(float(ts)); up_a.append(float(ask)); up_m.append(float(mid))
        else:
            dn_t.append(float(ts)); dn_a.append(float(ask)); dn_m.append(float(mid))
    up_t=np.array(up_t); up_a=np.array(up_a); up_m=np.array(up_m)
    dn_t=np.array(dn_t); dn_a=np.array(dn_a); dn_m=np.array(dn_m)

    cw = {}
    for cs, sides in candles.items():
        w = get_winner(sides)
        if w: cw[cs] = w

    entered = set()
    trades = []
    for i in range(len(st)):
        t_now = st[i]
        cs = (int(t_now) // INTERVAL) * INTERVAL
        if cs in entered: continue
        offset = t_now - cs
        if offset < 10 or offset > INTERVAL - 30: continue
        op = co.get(cs)
        if op is None: continue
        move = (p1s[i] - op) / op * 100
        if abs(move) < MOVE_THRESH: continue
        d = 'up' if move > 0 else 'down'
        ui = bisect_right(up_t, t_now) - 1
        di = bisect_right(dn_t, t_now) - 1
        if ui < 0 or di < 0: continue
        if d == 'up': ask = float(up_a[ui]); mid = float(up_m[ui])
        else: ask = float(dn_a[di]); mid = float(dn_m[di])
        if mid > 0.55 or ask <= 0 or ask >= 0.75: continue
        w = cw.get(cs)
        if w is None: continue
        cost = ask + fee(ask)
        if (d == 'up' and w == 'Up') or (d == 'down' and w == 'Down'):
            pnl = (1.0 - cost) * SH
        else:
            pnl = (0 - cost) * SH
        trades.append(pnl)
        entered.add(cs)

    if not trades:
        print(f"{asset:<8} {hours:<6.0f}h no trades"); continue
    n = len(trades)
    wins = sum(1 for t in trades if t > 0)
    net = sum(trades)
    avg_win = np.mean([t for t in trades if t > 0]) if wins else 0
    avg_loss = np.mean([t for t in trades if t <= 0]) if n-wins else 0
    daily = net / hours * 24
    grand += net
    print(f"{asset:<8} {hours:<6.0f}h {n:<8} {wins:<6} {100*wins/n:<6.1f}% {avg_win:<+9.2f} {avg_loss:<+9.2f} {net:<+11.0f} {daily:<+9.0f}")

print(f"\nGRAND TOTAL: ${grand:+,.0f} over 269h  |  ${grand/269*24:+,.0f}/day")
