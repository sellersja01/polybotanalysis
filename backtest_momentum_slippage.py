"""
Strat 7 (Candle Momentum) with slippage simulation:
Instead of entering at the exact tick when signal fires,
use the average ask of the NEXT 3 Poly ticks as entry price.

Run on both old data (market_btc_5m.db etc) and recent VPS data.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

OLD_BASE = r'C:\Users\James\polybotanalysis'
NEW_BASE = r'C:\Users\James\polybotanalysis\recent_data'

# Old data (107h) + Recent data (3.5h)
DATASETS = {
    # Old data
    'BTC_5m (old)':   (f'{OLD_BASE}/market_btc_5m.db',  300),
    'BTC_15m (old)':  (f'{OLD_BASE}/market_btc_15m.db', 900),
    'ETH_5m (old)':   (f'{OLD_BASE}/market_eth_5m.db',  300),
    'ETH_15m (old)':  (f'{OLD_BASE}/market_eth_15m.db', 900),
    # Recent data
    'BTC_5m (new)':   (f'{NEW_BASE}/market_btc_5m.db',  300),
    'BTC_15m (new)':  (f'{NEW_BASE}/market_btc_15m.db', 900),
    'ETH_5m (new)':   (f'{NEW_BASE}/market_eth_5m.db',  300),
    'ETH_15m (new)':  (f'{NEW_BASE}/market_eth_15m.db', 900),
    'SOL_5m (new)':   (f'{NEW_BASE}/market_sol_5m.db',  300),
    'SOL_15m (new)':  (f'{NEW_BASE}/market_sol_15m.db', 900),
    'XRP_5m (new)':   (f'{NEW_BASE}/market_xrp_5m.db',  300),
    'XRP_15m (new)':  (f'{NEW_BASE}/market_xrp_15m.db', 900),
}

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

SH = 100
N_TICKS = 3


def run_s7(db_path, interval, use_slippage):
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'asset_price' not in tables:
        conn.close()
        return None

    pr = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
    odds = conn.execute(
        'SELECT unix_time, outcome, ask, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()

    if not pr or len(pr) < 20 or not odds:
        return None

    price_t = np.array([float(r[0]) for r in pr])
    price_p = np.array([float(r[1]) for r in pr])
    sample_t = np.arange(price_t[0], price_t[-1], 1.0)
    price_1s = np.interp(sample_t, price_t, price_p)

    candle_opens = {}
    for i, ts in enumerate(price_t):
        cs = (int(ts) // interval) * interval
        if cs not in candle_opens:
            candle_opens[cs] = float(price_p[i])

    # Build poly tick arrays
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

    trades = []
    entry_asks = []
    last = 0

    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last < 30:
            continue
        cs = (int(t_now) // interval) * interval
        offset = t_now - cs
        if offset < 15 or offset > interval - 30:
            continue
        op = candle_opens.get(cs)
        if op is None:
            continue
        move = (price_1s[i] - op) / op * 100
        if abs(move) < 0.05:
            continue

        direction = 'up' if move > 0 else 'down'

        if direction == 'up':
            idx = bisect_right(up_t, t_now) - 1
            if idx < 0: continue
            instant_ask = float(up_a[idx])
            instant_mid = float(up_m[idx])
            if instant_mid > 0.55: continue

            if use_slippage:
                start_idx = bisect_right(up_t, t_now)
                end_idx = min(start_idx + N_TICKS, len(up_a))
                if end_idx <= start_idx: continue
                valid = [float(up_a[j]) for j in range(start_idx, end_idx)
                         if (int(up_t[j]) // interval) * interval == cs]
                if not valid: continue
                entry_ask = np.mean(valid)
            else:
                entry_ask = instant_ask
        else:
            idx = bisect_right(dn_t, t_now) - 1
            if idx < 0: continue
            instant_ask = float(dn_a[idx])
            instant_mid = float(dn_m[idx])
            if instant_mid > 0.55: continue

            if use_slippage:
                start_idx = bisect_right(dn_t, t_now)
                end_idx = min(start_idx + N_TICKS, len(dn_a))
                if end_idx <= start_idx: continue
                valid = [float(dn_a[j]) for j in range(start_idx, end_idx)
                         if (int(dn_t[j]) // interval) * interval == cs]
                if not valid: continue
                entry_ask = np.mean(valid)
            else:
                entry_ask = instant_ask

        if entry_ask <= 0.01 or entry_ask > 0.90:
            continue

        winner = cw.get(cs)
        if winner is None:
            continue

        cost = entry_ask * SH + fee(SH, entry_ask)
        if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
            pnl = (1.0 * SH) - cost
        else:
            pnl = 0 - cost

        trades.append(pnl)
        entry_asks.append(entry_ask)
        last = t_now

    if not trades:
        return None

    n = len(trades)
    wins = sum(1 for t in trades if t > 0)
    net = sum(trades)
    daily = net / max(hours, 0.1) * 24
    worst = min(trades)
    avg_ea = np.mean(entry_asks)

    return {
        'n': n, 'wins': wins, 'wr': 100*wins/n, 'net': net,
        'ppt': net/n, 'daily': daily, 'worst': worst, 'avg_entry': avg_ea,
        'hours': hours,
    }


print(f"{'Market':<16} {'Version':<18} {'Trades':>7} {'WR%':>6} {'$/trade':>9} {'Daily':>8} {'Worst':>8} {'AvgEntry':>9}")
print("-" * 95)

for label, (db_path, interval) in DATASETS.items():
    for version, slip in [("Instant", False), ("Slippage (3tick)", True)]:
        r = run_s7(db_path, interval, slip)
        if r is None:
            print(f"{label:<16} {version:<18} {'N/A':>7}")
            continue
        print(f"{label:<16} {version:<18} {r['n']:>7} {r['wr']:>5.1f}% {r['ppt']:>+8.2f} {r['daily']:>+8.0f} {r['worst']:>+8.2f} {r['avg_entry']:>8.3f}")
    print()
