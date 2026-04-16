"""
Strat 7 (Candle Momentum) — all 8 markets, instant vs slippage.
For markets without their own price feed, use BTC Coinbase price as cross-signal.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

OLD = r'C:\Users\James\polybotanalysis'
NEW = r'C:\Users\James\polybotanalysis\recent_data'
BTC_OLD = f'{OLD}/market_btc_5m.db'

def fee(sh, p):
    return sh * p * 0.072 * (p * (1 - p))

SH = 100
N_TICKS = 3


def load_price_feed(db_path):
    """Try loading native price feed from a DB."""
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'asset_price' not in tables:
        conn.close()
        return None
    pr = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
    conn.close()
    if not pr or len(pr) < 20:
        return None
    return {
        't': np.array([float(r[0]) for r in pr]),
        'p': np.array([float(r[1]) for r in pr]),
    }


def load_odds(db_path, interval):
    conn = sqlite3.connect(db_path)
    odds = conn.execute(
        'SELECT unix_time, outcome, ask, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()
    if not odds:
        return None, None, None, None

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

    cw = {}
    for cs, sides in candles_raw.items():
        if sides['Up'] and sides['Down']:
            cw[cs] = 'Up' if sides['Up'][-1][2] >= sides['Down'][-1][2] else 'Down'

    up = {'t': np.array(up_t), 'a': np.array(up_a), 'm': np.array(up_m)}
    dn = {'t': np.array(dn_t), 'a': np.array(dn_a), 'm': np.array(dn_m)}
    return up, dn, cw, candles_raw


def run_s7(prices, up, dn, cw, interval, use_slippage):
    sample_t = np.arange(prices['t'][0], prices['t'][-1], 1.0)
    price_1s = np.interp(sample_t, prices['t'], prices['p'])

    candle_opens = {}
    for i, ts in enumerate(prices['t']):
        cs = (int(ts) // interval) * interval
        if cs not in candle_opens:
            candle_opens[cs] = float(prices['p'][i])

    hours = (prices['t'][-1] - prices['t'][0]) / 3600
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
            idx = bisect_right(up['t'], t_now) - 1
            if idx < 0: continue
            instant_ask = float(up['a'][idx])
            instant_mid = float(up['m'][idx])
            if instant_mid > 0.55: continue
            if use_slippage:
                si = bisect_right(up['t'], t_now)
                ei = min(si + N_TICKS, len(up['a']))
                if ei <= si: continue
                valid = [float(up['a'][j]) for j in range(si, ei)
                         if (int(up['t'][j]) // interval) * interval == cs]
                if not valid: continue
                entry_ask = np.mean(valid)
            else:
                entry_ask = instant_ask
        else:
            idx = bisect_right(dn['t'], t_now) - 1
            if idx < 0: continue
            instant_ask = float(dn['a'][idx])
            instant_mid = float(dn['m'][idx])
            if instant_mid > 0.55: continue
            if use_slippage:
                si = bisect_right(dn['t'], t_now)
                ei = min(si + N_TICKS, len(dn['a']))
                if ei <= si: continue
                valid = [float(dn['a'][j]) for j in range(si, ei)
                         if (int(dn['t'][j]) // interval) * interval == cs]
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
            trades.append(1.0 * SH - cost)
        else:
            trades.append(0 - cost)
        entry_asks.append(entry_ask)
        last = t_now

    if not trades:
        return None
    n = len(trades)
    wins = sum(1 for t in trades if t > 0)
    net = sum(trades)
    return {
        'n': n, 'wins': wins, 'wr': 100*wins/n, 'net': net,
        'ppt': net/n, 'daily': net/max(hours, 0.1)*24, 'worst': min(trades),
        'avg_entry': np.mean(entry_asks), 'hours': hours,
    }


# Load BTC price feed for cross-signal on old data
btc_prices = load_price_feed(BTC_OLD)

TESTS = [
    # (label, odds_db, interval, price_source_db_or_None_for_btc_cross)
    # OLD DATA
    ('BTC_5m (old)',  f'{OLD}/market_btc_5m.db',  300, f'{OLD}/market_btc_5m.db'),
    ('BTC_15m (old)', f'{OLD}/market_btc_15m.db', 900, f'{OLD}/market_btc_15m.db'),
    ('ETH_5m (old)',  f'{OLD}/market_eth_5m.db',  300, f'{OLD}/market_eth_5m.db'),
    ('ETH_15m (old)', f'{OLD}/market_eth_15m.db', 900, f'{OLD}/market_eth_15m.db'),
    ('SOL_5m (old)',  f'{OLD}/market_sol_5m.db',  300, None),  # use BTC cross
    ('SOL_15m (old)', f'{OLD}/market_sol_15m.db', 900, None),
    ('XRP_5m (old)',  f'{OLD}/market_xrp_5m.db',  300, None),
    ('XRP_15m (old)', f'{OLD}/market_xrp_15m.db', 900, None),
    # NEW DATA
    ('BTC_5m (new)',  f'{NEW}/market_btc_5m.db',  300, f'{NEW}/market_btc_5m.db'),
    ('BTC_15m (new)', f'{NEW}/market_btc_15m.db', 900, f'{NEW}/market_btc_15m.db'),
    ('ETH_5m (new)',  f'{NEW}/market_eth_5m.db',  300, f'{NEW}/market_eth_5m.db'),
    ('ETH_15m (new)', f'{NEW}/market_eth_15m.db', 900, f'{NEW}/market_eth_15m.db'),
    ('SOL_5m (new)',  f'{NEW}/market_sol_5m.db',  300, f'{NEW}/market_sol_5m.db'),
    ('SOL_15m (new)', f'{NEW}/market_sol_15m.db', 900, f'{NEW}/market_sol_15m.db'),
    ('XRP_5m (new)',  f'{NEW}/market_xrp_5m.db',  300, f'{NEW}/market_xrp_5m.db'),
    ('XRP_15m (new)', f'{NEW}/market_xrp_15m.db', 900, f'{NEW}/market_xrp_15m.db'),
]

print(f"{'Market':<16} {'Version':<18} {'Trades':>7} {'WR%':>6} {'$/trade':>9} {'Daily':>8} {'Worst':>8} {'AvgEntry':>9}")
print("-" * 95)

for label, odds_db, interval, price_db in TESTS:
    # Load odds
    up, dn, cw, _ = load_odds(odds_db, interval)
    if up is None:
        print(f"{label:<16} NO ODDS DATA")
        continue

    # Load prices
    if price_db:
        prices = load_price_feed(price_db)
    else:
        prices = btc_prices  # BTC cross-signal

    if prices is None:
        print(f"{label:<16} NO PRICE FEED")
        continue

    for version, slip in [("Instant", False), ("Slippage (3tick)", True)]:
        r = run_s7(prices, up, dn, cw, interval, slip)
        if r is None:
            print(f"{label:<16} {version:<18} {'N/A':>7}")
            continue
        print(f"{label:<16} {version:<18} {r['n']:>7} {r['wr']:>5.1f}% {r['ppt']:>+8.2f} {r['daily']:>+8.0f} {r['worst']:>+8.2f} {r['avg_entry']:>8.3f}")

    if label.endswith('(old)') and label.startswith('XRP_15m'):
        print()
        print(f"{'Market':<16} {'Version':<18} {'Trades':>7} {'WR%':>6} {'$/trade':>9} {'Daily':>8} {'Worst':>8} {'AvgEntry':>9}")
        print("-" * 95)
    elif label.endswith('(new)') and not label.startswith('XRP_15m'):
        pass
    print()
