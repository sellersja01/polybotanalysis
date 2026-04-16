"""Backtest strats 5-7 with $100/candle budget cap"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

DB = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300

def poly_fee_new(price):
    return price * 0.072 * (price * (1 - price))

conn = sqlite3.connect(DB)
btc_raw = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
up_raw = conn.execute("SELECT unix_time, mid, ask FROM polymarket_odds WHERE outcome='Up' AND mid > 0 ORDER BY unix_time").fetchall()
dn_raw = conn.execute("SELECT unix_time, mid, ask FROM polymarket_odds WHERE outcome='Down' AND mid > 0 ORDER BY unix_time").fetchall()
conn.close()

btc_t = np.array([float(r[0]) for r in btc_raw])
btc_p = np.array([float(r[1]) for r in btc_raw])
up_t = np.array([float(r[0]) for r in up_raw])
up_m = np.array([float(r[1]) for r in up_raw])
up_a = np.array([float(r[2]) for r in up_raw])
dn_t = np.array([float(r[0]) for r in dn_raw])
dn_m = np.array([float(r[1]) for r in dn_raw])
dn_a = np.array([float(r[2]) for r in dn_raw])

hours = (btc_t[-1] - btc_t[0]) / 3600
sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
btc_1s = np.interp(sample_t, btc_t, btc_p)

candle_opens = {}
for i, ts in enumerate(btc_t):
    cs = (int(ts) // INTERVAL) * INTERVAL
    if cs not in candle_opens:
        candle_opens[cs] = float(btc_p[i])

candle_winners = {}
poly_candles = defaultdict(lambda: {'Up': [], 'Down': []})
for i in range(len(up_t)):
    cs = (int(up_t[i]) // INTERVAL) * INTERVAL
    poly_candles[cs]['Up'].append(up_m[i])
for i in range(len(dn_t)):
    cs = (int(dn_t[i]) // INTERVAL) * INTERVAL
    poly_candles[cs]['Down'].append(dn_m[i])
for cs, sides in poly_candles.items():
    if sides['Up'] and sides['Down']:
        candle_winners[cs] = 'Up' if sides['Up'][-1] >= sides['Down'][-1] else 'Down'


def try_entry(t_now, direction, budget_remaining):
    ui = bisect_right(up_t, t_now) - 1
    di = bisect_right(dn_t, t_now) - 1
    if ui < 0 or di < 0:
        return None
    if direction == 'up':
        ask = float(up_a[ui]); mid = float(up_m[ui])
    else:
        ask = float(dn_a[di]); mid = float(dn_m[di])
    if ask <= 0.01 or ask > 0.90 or mid > 0.55:
        return None
    fee = poly_fee_new(ask)
    cps = ask + fee
    if budget_remaining < cps:
        return None
    shares = min(int(budget_remaining / cps), 100)
    if shares < 1:
        return None
    cost = cps * shares
    cs = (int(t_now) // INTERVAL) * INTERVAL
    winner = candle_winners.get(cs)
    if winner is None:
        return None
    if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
        pnl = (1.0 * shares) - cost
    else:
        pnl = 0 - cost
    return cost, pnl


BUDGET = 100.0

# STRAT 5
def run_s5():
    cs_spent = defaultdict(float)
    cs_pnl = defaultdict(float)
    cs_trades = defaultdict(int)
    last_entry = 0
    for i in range(15, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < 30:
            continue
        move = (btc_1s[i] - btc_1s[i-15]) / btc_1s[i-15] * 100
        if abs(move) < 0.05:
            continue
        cs = (int(t_now) // INTERVAL) * INTERVAL
        rem = BUDGET - cs_spent[cs]
        r = try_entry(t_now, 'up' if move > 0 else 'down', rem)
        if r is None:
            continue
        cost, pnl = r
        cs_spent[cs] += cost
        cs_pnl[cs] += pnl
        cs_trades[cs] += 1
        last_entry = t_now
    return cs_spent, cs_pnl, cs_trades

# STRAT 6
def run_s6():
    cs_spent = defaultdict(float)
    cs_pnl = defaultdict(float)
    cs_trades = defaultdict(int)
    oracle_t = np.arange(sample_t[0], sample_t[-1], 60)
    oracle_p = np.interp(oracle_t, sample_t, btc_1s)
    last_entry = 0
    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < 30:
            continue
        oi = bisect_right(oracle_t, t_now) - 1
        if oi < 0:
            continue
        dev = (btc_1s[i] - oracle_p[oi]) / oracle_p[oi] * 100
        if abs(dev) < 0.05:
            continue
        cs = (int(t_now) // INTERVAL) * INTERVAL
        rem = BUDGET - cs_spent[cs]
        r = try_entry(t_now, 'up' if dev > 0 else 'down', rem)
        if r is None:
            continue
        cost, pnl = r
        cs_spent[cs] += cost
        cs_pnl[cs] += pnl
        cs_trades[cs] += 1
        last_entry = t_now
    return cs_spent, cs_pnl, cs_trades

# STRAT 7
def run_s7():
    cs_spent = defaultdict(float)
    cs_pnl = defaultdict(float)
    cs_trades = defaultdict(int)
    last_entry = 0
    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < 30:
            continue
        cs = (int(t_now) // INTERVAL) * INTERVAL
        offset = t_now - cs
        if offset < 15 or offset > INTERVAL - 30:
            continue
        op = candle_opens.get(cs)
        if op is None:
            continue
        move = (btc_1s[i] - op) / op * 100
        if abs(move) < 0.05:
            continue
        rem = BUDGET - cs_spent[cs]
        r = try_entry(t_now, 'up' if move > 0 else 'down', rem)
        if r is None:
            continue
        cost, pnl = r
        cs_spent[cs] += cost
        cs_pnl[cs] += pnl
        cs_trades[cs] += 1
        last_entry = t_now
    return cs_spent, cs_pnl, cs_trades


def report(name, spent, pnl, trades):
    active = {k: v for k, v in spent.items() if v > 0}
    if not active:
        print(f"{name}: no trades\n")
        return
    n = len(active)
    total_pnl = sum(pnl[k] for k in active)
    total_spent = sum(spent[k] for k in active)
    wins = sum(1 for k in active if pnl[k] > 0)
    avg_spent = total_spent / n
    worst = min(pnl[k] for k in active)
    best = max(pnl[k] for k in active)
    daily = total_pnl / hours * 24
    avg_trades = np.mean([trades[k] for k in active])

    print(f"=== {name} ($100/candle cap) ===")
    print(f"  Active candles: {n} | Avg trades/candle: {avg_trades:.1f}")
    print(f"  Avg spent/candle: ${avg_spent:.2f}")
    print(f"  Candle WR: {100*wins/n:.1f}% | Total PnL: ${total_pnl:+,.2f}")
    print(f"  Avg PnL/candle: ${total_pnl/n:+.2f}")
    print(f"  Best: ${best:+.2f} | Worst: ${worst:+.2f}")
    print(f"  Daily: ${daily:+,.0f} | ROI: {100*total_pnl/total_spent:.1f}%")
    print()


print(f"Budget: ${BUDGET}/candle | Data: {hours:.0f}h\n")

s5 = run_s5(); report("Strat 5 (Latency Arb)", *s5)
s6 = run_s6(); report("Strat 6 (Oracle Lag 60s)", *s6)
s7 = run_s7(); report("Strat 7 (Candle Momentum 0.05%)", *s7)
