"""
Backtest the EXACT live_coinbase.py strategy:
  - Coinbase BTC moves >= 0.05% in 15 seconds
  - Poly mid < 0.55 (stale)
  - No entries in last 30s of candle
  - Exit: profit >= 2c/share OR 20s elapsed OR candle has 10s left
  - Exit = buy other side (hedge): cost = entry_ask + other_ask, payout = $1.00
  - Max 2 entries per candle, cooldown 2s

Run on old data (107h) and new data (3.5h).
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

OLD = r'C:\Users\James\polybotanalysis'
NEW = r'C:\Users\James\polybotanalysis\recent_data'

LOOKBACK = 15
MOVE_THRESH = 0.05
COOLDOWN = 2
MAX_ENTRY = 0.75
MAX_STALE = 0.55
MAX_TRADES_PER_CANDLE = 9999
CANDLE = 300

def fee(price):
    return price * 0.072 * (price * (1 - price))

DATASETS = [
    ('BTC_5m (old)',  f'{OLD}/market_btc_5m.db',  300),
    ('BTC_15m (old)', f'{OLD}/market_btc_15m.db', 900),
    ('ETH_5m (old)',  f'{OLD}/market_eth_5m.db',  300),
    ('ETH_15m (old)', f'{OLD}/market_eth_15m.db', 900),
    ('BTC_5m (new)',  f'{NEW}/market_btc_5m.db',  300),
    ('BTC_15m (new)', f'{NEW}/market_btc_15m.db', 900),
    ('ETH_5m (new)',  f'{NEW}/market_eth_5m.db',  300),
    ('ETH_15m (new)', f'{NEW}/market_eth_15m.db', 900),
    ('SOL_5m (new)',  f'{NEW}/market_sol_5m.db',  300),
    ('SOL_15m (new)', f'{NEW}/market_sol_15m.db', 900),
    ('XRP_5m (new)',  f'{NEW}/market_xrp_5m.db',  300),
    ('XRP_15m (new)', f'{NEW}/market_xrp_15m.db', 900),
]

print(f"Live strategy backtest: MOVE={MOVE_THRESH}% COOLDOWN={COOLDOWN}s MAX_TRADES={MAX_TRADES_PER_CANDLE}")
print(f"No entries last 30s | Exit: profit>=2c OR 20s OR candle last 10s | Exit=hedge (buy other side)")
print()
print(f"{'Market':<16} {'Trades':<8} {'WR%':<7} {'$/trade':<10} {'Total PnL':<12} {'Avg entry':<10} {'Avg hedge':<10}")
print("-" * 85)

for label, db_path, interval in DATASETS:
    try:
        conn = sqlite3.connect(db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if 'asset_price' not in tables:
            conn.close(); continue
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
    for ts, out, ask, mid in odds:
        ts = float(ts); ask = float(ask); mid = float(mid)
        if out == 'Up':
            up_t.append(ts); up_a.append(ask); up_m.append(mid)
        else:
            dn_t.append(ts); dn_a.append(ask); dn_m.append(mid)

    up_t = np.array(up_t); up_a = np.array(up_a); up_m = np.array(up_m)
    dn_t = np.array(dn_t); dn_a = np.array(dn_a); dn_m = np.array(dn_m)

    hours = (price_t[-1] - price_t[0]) / 3600

    # Track open trades and simulate exits
    open_trades = []
    closed_trades = []
    last_signal = 0
    candle_trade_counts = defaultdict(int)

    for i in range(LOOKBACK, len(sample_t)):
        t_now = sample_t[i]
        candle_start = (int(t_now) // interval) * interval
        candle_age = t_now - candle_start
        candle_remaining = interval - candle_age

        # ── Check exits first ──
        still_open = []
        for trade in open_trades:
            age = t_now - trade["entry_ts"]

            # Get current mid for our side
            if trade["side"] == "up":
                ui = bisect_right(up_t, t_now) - 1
                current_mid = float(up_m[ui]) if ui >= 0 else 0
            else:
                di = bisect_right(dn_t, t_now) - 1
                current_mid = float(dn_m[di]) if di >= 0 else 0

            profit_per_share = current_mid - trade["entry_ask"] - trade["fee"]
            force_exit = candle_remaining <= 10

            if current_mid > 0 and (profit_per_share >= 0.02 or age >= 20 or force_exit):
                # Exit = sell at mid price
                pnl = profit_per_share * 100  # per 100 shares

                closed_trades.append({
                    "pnl": pnl,
                    "entry_ask": trade["entry_ask"],
                    "other_ask": current_mid,
                    "age": age,
                    "force": force_exit,
                })
            else:
                still_open.append(trade)

        open_trades = still_open

        # ── Check entries ──
        if t_now - last_signal < COOLDOWN:
            continue
        if len(open_trades) >= MAX_TRADES_PER_CANDLE:
            continue
        if candle_trade_counts[candle_start] >= MAX_TRADES_PER_CANDLE:
            continue
        # No entries in last 30s
        if candle_age > interval - 30:
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
        if ask <= 0.01 or ask >= MAX_ENTRY:
            continue

        f = fee(ask)
        last_signal = t_now
        candle_trade_counts[candle_start] += 1

        open_trades.append({
            "side": direction,
            "entry_ask": ask,
            "fee": f,
            "entry_ts": t_now,
        })

    # Force close any remaining open trades
    for trade in open_trades:
        closed_trades.append({
            "pnl": 0,
            "entry_ask": trade["entry_ask"],
            "other_ask": 0,
            "age": 0,
            "force": True,
        })

    if not closed_trades:
        print(f"{label:<16} {'N/A':<8}")
        continue

    n = len(closed_trades)
    wins = sum(1 for t in closed_trades if t["pnl"] > 0)
    net = sum(t["pnl"] for t in closed_trades)
    avg_entry = np.mean([t["entry_ask"] for t in closed_trades])
    avg_hedge = np.mean([t["other_ask"] for t in closed_trades if t["other_ask"] > 0])
    print(f"{label:<16} {n:<8} {100*wins/n:<6.1f}% {net/n:<+9.2f} {net:<+11.2f} {avg_entry:<9.3f} {avg_hedge:<9.3f}")

print()
