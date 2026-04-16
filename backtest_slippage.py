"""
Strat 5 (Latency Arb) with slippage simulation:
Instead of entering at the exact tick when signal fires,
use the average ask of the NEXT 3 Poly ticks as entry price.
This accounts for the fact that by the time your order fills,
the price may have moved.

Run across all 8 markets.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

BTC_DB = r'C:\Users\James\polybotanalysis\market_btc_5m.db'

MARKETS = {
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

# Load BTC price feed
conn = sqlite3.connect(BTC_DB)
btc_raw = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
conn.close()
btc_t = np.array([float(r[0]) for r in btc_raw])
btc_p = np.array([float(r[1]) for r in btc_raw])
sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
btc_1s = np.interp(sample_t, btc_t, btc_p)

print(f"BTC price feed: {len(btc_t):,} ticks, {(btc_t[-1]-btc_t[0])/3600:.0f}h\n")

SH = 100
LOOKBACK = 15
COOLDOWN = 30
MOVE_THRESH = 0.05
N_TICKS_AVG = 3  # average next 3 ticks for entry

print(f"{'Market':<10} {'Version':<22} {'Trades':>7} {'WR%':>6} {'$/trade':>9} {'Daily':>8} {'Worst':>8} {'AvgEntry':>9}")
print("-" * 90)

for label, (db_file, interval) in MARKETS.items():
    conn = sqlite3.connect(db_file)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    odds_rows = conn.execute(
        'SELECT unix_time, outcome, ask, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
    ).fetchall()
    conn.close()

    if not odds_rows:
        print(f"{label:<10} NO DATA")
        continue

    # Build separate Up/Down tick arrays
    up_ticks = []  # (ts, ask, mid)
    dn_ticks = []
    candles_raw = defaultdict(lambda: {'Up': [], 'Down': []})

    for ts, out, ask, mid in odds_rows:
        ts = float(ts); ask = float(ask); mid = float(mid)
        cs = (int(ts) // interval) * interval
        candles_raw[cs][out].append((ts, ask, mid))
        if out == 'Up':
            up_ticks.append((ts, ask, mid))
        else:
            dn_ticks.append((ts, ask, mid))

    up_t = np.array([t[0] for t in up_ticks])
    up_a = np.array([t[1] for t in up_ticks])
    up_m = np.array([t[2] for t in up_ticks])
    dn_t = np.array([t[0] for t in dn_ticks])
    dn_a = np.array([t[1] for t in dn_ticks])
    dn_m = np.array([t[2] for t in dn_ticks])

    # Candle winners
    cw = {}
    for cs, sides in candles_raw.items():
        if sides['Up'] and sides['Down']:
            cw[cs] = 'Up' if sides['Up'][-1][2] >= sides['Down'][-1][2] else 'Down'

    overlap_start = max(btc_t[0], min(up_t[0], dn_t[0]))
    overlap_end = min(btc_t[-1], max(up_t[-1], dn_t[-1]))
    overlap_hours = max(1, (overlap_end - overlap_start) / 3600)

    # Run both versions: instant entry vs avg-of-next-3
    for version, use_avg in [("Instant entry", False), ("Avg next 3 ticks", True)]:
        trades = []
        entry_asks = []
        last_entry = 0

        for i in range(LOOKBACK, len(sample_t)):
            t_now = sample_t[i]
            if t_now - last_entry < COOLDOWN:
                continue
            move = (btc_1s[i] - btc_1s[i - LOOKBACK]) / btc_1s[i - LOOKBACK] * 100
            if abs(move) < MOVE_THRESH:
                continue
            direction = 'up' if move > 0 else 'down'

            if direction == 'up':
                idx = bisect_right(up_t, t_now) - 1
                if idx < 0:
                    continue
                instant_ask = float(up_a[idx])
                instant_mid = float(up_m[idx])

                if use_avg:
                    # Get next 3 ticks AFTER signal
                    start_idx = bisect_right(up_t, t_now)
                    end_idx = min(start_idx + N_TICKS_AVG, len(up_a))
                    if end_idx <= start_idx:
                        continue
                    next_asks = up_a[start_idx:end_idx]
                    # Make sure these ticks are within same candle
                    cs = (int(t_now) // interval) * interval
                    valid = [float(up_a[j]) for j in range(start_idx, end_idx) if (int(up_t[j]) // interval) * interval == cs]
                    if not valid:
                        continue
                    entry_ask = np.mean(valid)
                    # Still check staleness on instant tick
                    if instant_mid > 0.55:
                        continue
                else:
                    entry_ask = instant_ask
                    if instant_mid > 0.55:
                        continue
            else:
                idx = bisect_right(dn_t, t_now) - 1
                if idx < 0:
                    continue
                instant_ask = float(dn_a[idx])
                instant_mid = float(dn_m[idx])

                if use_avg:
                    start_idx = bisect_right(dn_t, t_now)
                    end_idx = min(start_idx + N_TICKS_AVG, len(dn_a))
                    if end_idx <= start_idx:
                        continue
                    cs = (int(t_now) // interval) * interval
                    valid = [float(dn_a[j]) for j in range(start_idx, end_idx) if (int(dn_t[j]) // interval) * interval == cs]
                    if not valid:
                        continue
                    entry_ask = np.mean(valid)
                    if instant_mid > 0.55:
                        continue
                else:
                    entry_ask = instant_ask
                    if instant_mid > 0.55:
                        continue

            if entry_ask <= 0.01 or entry_ask > 0.90:
                continue

            cs = (int(t_now) // interval) * interval
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
            last_entry = t_now

        if not trades:
            print(f"{label:<10} {version:<22} {'N/A':>7}")
            continue

        n = len(trades)
        wins = sum(1 for t in trades if t > 0)
        net = sum(trades)
        daily = net / overlap_hours * 24
        worst = min(trades)
        avg_ea = np.mean(entry_asks)

        print(f"{label:<10} {version:<22} {n:>7} {100*wins/n:>5.1f}% {net/n:>+8.2f} {daily:>+8.0f} {worst:>+8.2f} {avg_ea:>8.3f}")

    print()
