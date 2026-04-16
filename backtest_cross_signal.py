"""
Backtest Strats 5 & 7 on SOL/XRP using BTC Coinbase price as the signal.
BTC moves first, SOL/XRP Polymarket lags behind.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

BTC_DB = r'C:\Users\James\polybotanalysis\market_btc_5m.db'

TARGETS = {
    'SOL_5m':  ('market_sol_5m.db',  300),
    'SOL_15m': ('market_sol_15m.db', 900),
    'XRP_5m':  ('market_xrp_5m.db',  300),
    'XRP_15m': ('market_xrp_15m.db', 900),
}

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

# Load BTC price feed
conn = sqlite3.connect(BTC_DB)
btc_raw = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
conn.close()
btc_t = np.array([float(r[0]) for r in btc_raw])
btc_p = np.array([float(r[1]) for r in btc_raw])
sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
btc_1s = np.interp(sample_t, btc_t, btc_p)

# BTC candle opens (for Strat 7)
btc_candle_opens_300 = {}
btc_candle_opens_900 = {}
for i, ts in enumerate(btc_t):
    cs5 = (int(ts) // 300) * 300
    cs15 = (int(ts) // 900) * 900
    if cs5 not in btc_candle_opens_300:
        btc_candle_opens_300[cs5] = float(btc_p[i])
    if cs15 not in btc_candle_opens_900:
        btc_candle_opens_900[cs15] = float(btc_p[i])

print(f"BTC price feed: {len(btc_t):,} ticks, {(btc_t[-1]-btc_t[0])/3600:.0f}h\n")

SH = 100

print(f"{'Market':<10} {'Strat':<28} {'Trades':>7} {'WR%':>6} {'$/trade':>9} {'Daily':>8} {'Worst':>8}")
print("-" * 80)

for label, (db_file, interval) in TARGETS.items():
    conn = sqlite3.connect(db_file)
    odds_rows = conn.execute(
        'SELECT unix_time, market_id, outcome, ask, bid, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()

    if not odds_rows:
        print(f"{label:<10} NO DATA")
        continue

    # Build poly arrays
    up_t_l = []; up_m_l = []; up_a_l = []
    dn_t_l = []; dn_m_l = []; dn_a_l = []
    candles_raw = defaultdict(lambda: {'Up': [], 'Down': []})

    for ts, mid_id, out, ask, bid, mid in odds_rows:
        cs = (int(float(ts)) // interval) * interval
        candles_raw[(cs, mid_id)][out].append((float(ts), float(ask), float(bid), float(mid)))
        if out == 'Up':
            up_t_l.append(float(ts)); up_m_l.append(float(mid)); up_a_l.append(float(ask))
        else:
            dn_t_l.append(float(ts)); dn_m_l.append(float(mid)); dn_a_l.append(float(ask))

    up_t_np = np.array(up_t_l); up_m_np = np.array(up_m_l); up_a_np = np.array(up_a_l)
    dn_t_np = np.array(dn_t_l); dn_m_np = np.array(dn_m_l); dn_a_np = np.array(dn_a_l)

    # Candle winners
    cw = {}
    for (cs, mid_id), sides in candles_raw.items():
        if sides['Up'] and sides['Down']:
            cw[cs] = 'Up' if sides['Up'][-1][3] >= sides['Down'][-1][3] else 'Down'

    hours = (max(up_t_l[-1], dn_t_l[-1]) - min(up_t_l[0], dn_t_l[0])) / 3600
    # Only count hours where BTC price data overlaps
    overlap_start = max(btc_t[0], min(up_t_l[0], dn_t_l[0]))
    overlap_end = min(btc_t[-1], max(up_t_l[-1], dn_t_l[-1]))
    overlap_hours = max(0, (overlap_end - overlap_start) / 3600)

    candle_opens = btc_candle_opens_300 if interval == 300 else btc_candle_opens_900

    # ── STRAT 5: BTC Latency Arb → target market ──
    trades_s5 = []
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
            ask = float(up_a_np[ui]); mid = float(up_m_np[ui])
        else:
            ask = float(dn_a_np[di]); mid = float(dn_m_np[di])
        if ask <= 0.01 or ask > 0.90 or mid > 0.55:
            continue

        cs = (int(t_now) // interval) * interval
        winner = cw.get(cs)
        if winner is None:
            continue

        cost = ask * SH + fee(SH, ask)
        if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
            pnl = (1.0 * SH) - cost
        else:
            pnl = 0 - cost
        trades_s5.append(pnl)
        last_entry = t_now

    # ── STRAT 7: BTC Candle Momentum → target market ──
    trades_s7 = []
    last_entry = 0
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
            ask = float(up_a_np[ui]); mid = float(up_m_np[ui])
        else:
            ask = float(dn_a_np[di]); mid = float(dn_m_np[di])
        if ask <= 0.01 or ask > 0.90 or mid > 0.55:
            continue

        winner = cw.get(cs)
        if winner is None:
            continue

        cost = ask * SH + fee(SH, ask)
        if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
            pnl = (1.0 * SH) - cost
        else:
            pnl = 0 - cost
        trades_s7.append(pnl)
        last_entry = t_now

    for name, trades in [("Strat 5 (BTC->Latency)", trades_s5), ("Strat 7 (BTC->Momentum)", trades_s7)]:
        if not trades:
            print(f"{label:<10} {name:<28} {'N/A':>7}")
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t > 0)
        net = sum(trades)
        daily = net / overlap_hours * 24 if overlap_hours > 0 else 0
        worst = min(trades)
        print(f"{label:<10} {name:<28} {n:>7} {100*wins/n:>5.1f}% {net/n:>+8.2f} {daily:>+8.0f} {worst:>+8.2f}")

    print()
