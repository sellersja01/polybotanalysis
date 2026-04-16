"""
Backtest the EXACT paper_test.py strategy (Strat 5 / Latency Arb):
  - Binance/Coinbase BTC moves >= MOVE_THRESH% in 15 seconds
  - Poly mid < 0.55 (stale)
  - Entry at ask price, MIN 0.25, MAX 0.75
  - Exit A: sell after 30s (mid at that point)
  - Exit B: hold to candle resolution ($1.00 or $0.00)
  - Cooldown: 2 seconds between trades

Run on old data (107h) and new data (3.5h), all markets.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

OLD = r'C:\Users\James\polybotanalysis'
NEW = r'C:\Users\James\polybotanalysis\recent_data'

LOOKBACK = 15
MOVE_THRESH = 0.07
COOLDOWN = 2
MIN_ENTRY = 0.25
MAX_ENTRY = 0.75
MAX_STALE = 0.55
SH = 100

def fee(price):
    return price * 0.072 * (price * (1 - price))

DATASETS = [
    # Old data
    ('BTC_5m (old)',  f'{OLD}/market_btc_5m.db',  300),
    ('BTC_15m (old)', f'{OLD}/market_btc_15m.db', 900),
    ('ETH_5m (old)',  f'{OLD}/market_eth_5m.db',  300),
    ('ETH_15m (old)', f'{OLD}/market_eth_15m.db', 900),
    # New data
    ('BTC_5m (new)',  f'{NEW}/market_btc_5m.db',  300),
    ('BTC_15m (new)', f'{NEW}/market_btc_15m.db', 900),
    ('ETH_5m (new)',  f'{NEW}/market_eth_5m.db',  300),
    ('ETH_15m (new)', f'{NEW}/market_eth_15m.db', 900),
    ('SOL_5m (new)',  f'{NEW}/market_sol_5m.db',  300),
    ('SOL_15m (new)', f'{NEW}/market_sol_15m.db', 900),
    ('XRP_5m (new)',  f'{NEW}/market_xrp_5m.db',  300),
    ('XRP_15m (new)', f'{NEW}/market_xrp_15m.db', 900),
]

print(f"paper_test.py exact strategy: LOOKBACK={LOOKBACK}s MOVE={MOVE_THRESH}% COOLDOWN={COOLDOWN}s")
print(f"Entry: ask {MIN_ENTRY}-{MAX_ENTRY} | Stale: mid < {MAX_STALE}")
print()
print(f"{'Market':<16} {'Exit':<12} {'Trades':<8} {'WR%':<7} {'$/trade':<10} {'Total PnL':<12} {'Avg entry':<10}")
print("-" * 85)

for label, db_path, interval in DATASETS:
    try:
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if 'asset_price' not in tables:
            conn.close()
            continue
        pr = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
        odds = conn.execute(
            'SELECT unix_time, outcome, ask, mid FROM polymarket_odds '
            'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time'
        ).fetchall()
        conn.close()
    except:
        continue

    if not pr or len(pr) < 20 or not odds:
        continue

    price_t = np.array([float(r[0]) for r in pr])
    price_p = np.array([float(r[1]) for r in pr])
    sample_t = np.arange(price_t[0], price_t[-1], 1.0)
    price_1s = np.interp(sample_t, price_t, price_p)

    up_t = []; up_a = []; up_m = []
    dn_t = []; dn_a = []; dn_m = []
    candles_raw = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, out, ask, mid in odds:
        ts = float(ts); ask = float(ask); mid = float(mid)
        cs = (int(ts) // interval) * interval
        candles_raw[cs][out].append((ts, ask, mid))
        if out == 'Up':
            up_t.append(ts); up_a.append(ask); up_m.append(mid)
        else:
            dn_t.append(ts); dn_a.append(ask); dn_m.append(mid)

    up_t = np.array(up_t); up_a = np.array(up_a); up_m = np.array(up_m)
    dn_t = np.array(dn_t); dn_a = np.array(dn_a); dn_m = np.array(dn_m)

    cw = {}
    for cs, sides in candles_raw.items():
        if sides['Up'] and sides['Down']:
            cw[cs] = 'Up' if sides['Up'][-1][2] >= sides['Down'][-1][2] else 'Down'

    hours = (price_t[-1] - price_t[0]) / 3600

    # Run with both exit modes
    for exit_mode in ['30s', 'resolution']:
        trades = []
        entry_asks = []
        last = 0

        for i in range(LOOKBACK, len(sample_t)):
            t_now = sample_t[i]
            if t_now - last < COOLDOWN:
                continue

            move = (price_1s[i] - price_1s[i - LOOKBACK]) / price_1s[i - LOOKBACK] * 100
            if abs(move) < MOVE_THRESH:
                continue

            direction = 'up' if move > 0 else 'down'

            ui = bisect_right(up_t, t_now) - 1
            di = bisect_right(dn_t, t_now) - 1
            if ui < 0 or di < 0:
                continue

            if direction == 'up':
                ask = float(up_a[ui]); mid = float(up_m[ui])
            else:
                ask = float(dn_a[di]); mid = float(dn_m[di])

            if mid > MAX_STALE:
                continue
            if ask < MIN_ENTRY or ask > MAX_ENTRY:
                continue

            f = fee(ask)
            cost = ask + f  # per share

            if exit_mode == 'resolution':
                cs = (int(t_now) // interval) * interval
                winner = cw.get(cs)
                if winner is None:
                    continue
                if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
                    pnl = (1.0 - cost) * SH
                else:
                    pnl = (0 - cost) * SH
            else:
                # Exit after 30s — get mid at t+30
                t_exit = t_now + 30
                if direction == 'up':
                    ei = bisect_right(up_t, t_exit) - 1
                    if ei < 0: continue
                    exit_mid = float(up_m[ei])
                else:
                    ei = bisect_right(dn_t, t_exit) - 1
                    if ei < 0: continue
                    exit_mid = float(dn_m[ei])
                pnl = (exit_mid - cost) * SH

            trades.append(pnl)
            entry_asks.append(ask)
            last = t_now

        if not trades:
            print(f"{label:<16} {exit_mode:<12} {'N/A':<8}")
            continue

        n = len(trades)
        wins = sum(1 for t in trades if t > 0)
        net = sum(trades)
        avg_ea = np.mean(entry_asks)
        print(f"{label:<16} {exit_mode:<12} {n:<8} {100*wins/n:<6.1f}% {net/n:<+9.2f} {net:<+11.2f} {avg_ea:<.3f}")

    print()
