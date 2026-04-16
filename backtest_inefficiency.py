"""
Backtest strategies that exploit pricing inefficiencies between
Coinbase real-time BTC price and Polymarket odds.

Strat 5 (Latency Arb): BTC moves on Coinbase >= threshold in N seconds,
    Poly mid still < 0.55 (stale). Buy correct side, exit after M seconds.

Strat 6 (Simulated Oracle Lag): Simulate Chainlink-style oracle that only
    updates every ~60s. When Coinbase deviates from "oracle" by >= threshold,
    buy the direction of the deviation on Poly while odds are stale.

Strat 7 (Candle Open Momentum): Compare current Coinbase price vs price at
    candle start. If BTC has moved >= threshold from candle open, buy that
    direction on Poly if odds are still stale.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

DB = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300

def poly_fee_new(price):
    """Post-Mar30 fee formula"""
    return price * 0.072 * (price * (1 - price))

# Load data
conn = sqlite3.connect(DB)
btc_raw = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
up_raw = conn.execute("SELECT unix_time, mid, ask, bid FROM polymarket_odds WHERE outcome='Up' AND mid > 0 ORDER BY unix_time").fetchall()
dn_raw = conn.execute("SELECT unix_time, mid, ask, bid FROM polymarket_odds WHERE outcome='Down' AND mid > 0 ORDER BY unix_time").fetchall()
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
print(f"Data: {len(btc_t):,} BTC ticks, {len(up_t):,} Up ticks, {len(dn_t):,} Down ticks")
print(f"Range: {hours:.0f} hours\n")

# Sample BTC every 1s for fast lookups
sample_t = np.arange(btc_t[0], btc_t[-1], 1.0)
btc_1s = np.interp(sample_t, btc_t, btc_p)

# Build candle open prices
candle_opens = {}
for i, ts in enumerate(btc_t):
    cs = (int(ts) // INTERVAL) * INTERVAL
    if cs not in candle_opens:
        candle_opens[cs] = float(btc_p[i])

# Build candle winners from Poly data
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
        final_up = sides['Up'][-1]
        final_dn = sides['Down'][-1]
        candle_winners[cs] = 'Up' if final_up >= final_dn else 'Down'


def get_poly_state(t_now):
    """Get current Up/Down mid and ask at time t_now"""
    ui = bisect_right(up_t, t_now) - 1
    di = bisect_right(dn_t, t_now) - 1
    if ui < 0 or di < 0:
        return None
    return {
        'up_mid': up_m[ui], 'up_ask': up_a[ui],
        'dn_mid': dn_m[di], 'dn_ask': dn_a[di],
    }


def get_poly_exit(t_exit, direction):
    """Get exit mid for the side we bought"""
    if direction == 'up':
        idx = bisect_right(up_t, t_exit) - 1
        return up_m[idx] if idx >= 0 else None
    else:
        idx = bisect_right(dn_t, t_exit) - 1
        return dn_m[idx] if idx >= 0 else None


def get_candle_winner(t_now):
    cs = (int(t_now) // INTERVAL) * INTERVAL
    return candle_winners.get(cs)


def simulate_trade(t_now, direction, exit_mode='30s'):
    """Simulate a single trade entry. Returns (pnl_per_dollar, entry_ask, cost)"""
    poly = get_poly_state(t_now)
    if poly is None:
        return None

    if direction == 'up':
        entry_ask = poly['up_ask']
        entry_mid = poly['up_mid']
    else:
        entry_ask = poly['dn_ask']
        entry_mid = poly['dn_mid']

    if entry_ask <= 0.01 or entry_ask > 0.90:
        return None
    if entry_mid > 0.55:
        return None  # already repriced

    fee = poly_fee_new(entry_ask)
    cost = entry_ask + fee  # per share

    if exit_mode == 'resolution':
        winner = get_candle_winner(t_now)
        if winner is None:
            return None
        if (direction == 'up' and winner == 'Up') or (direction == 'down' and winner == 'Down'):
            pnl = 1.0 - cost
        else:
            pnl = 0.0 - cost
    else:
        secs = int(exit_mode.replace('s', ''))
        exit_mid = get_poly_exit(t_now + secs, direction)
        if exit_mid is None:
            return None
        pnl = exit_mid - cost

    return {'pnl': pnl, 'entry_ask': entry_ask, 'cost': cost}


# ============================================================
# STRAT 5: Latency Arb (BTC move in N seconds)
# ============================================================
def strat5_latency(lookback=15, move_thresh=0.05, cooldown=30, exit_mode='30s', max_entry_mid=0.55):
    trades = []
    last_entry = 0

    for i in range(lookback, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < cooldown:
            continue

        move_pct = (btc_1s[i] - btc_1s[i - lookback]) / btc_1s[i - lookback] * 100
        if abs(move_pct) < move_thresh:
            continue

        direction = 'up' if move_pct > 0 else 'down'
        result = simulate_trade(t_now, direction, exit_mode)
        if result is None:
            continue

        last_entry = t_now
        trades.append(result)

    return trades


# ============================================================
# STRAT 6: Simulated Oracle Lag (Chainlink-style 60s stale price)
# ============================================================
def strat6_oracle_lag(oracle_interval=60, deviation_thresh=0.05, cooldown=30, exit_mode='30s'):
    """Simulate an oracle that snapshots BTC price every oracle_interval seconds.
    When Coinbase deviates from the stale oracle price by >= threshold%, trade."""
    trades = []
    last_entry = 0

    # Build oracle prices (snapshot every N seconds)
    oracle_times = np.arange(sample_t[0], sample_t[-1], oracle_interval)
    oracle_prices = np.interp(oracle_times, sample_t, btc_1s)

    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < cooldown:
            continue

        # Find most recent oracle price
        oi = bisect_right(oracle_times, t_now) - 1
        if oi < 0:
            continue
        oracle_price = oracle_prices[oi]
        current_price = btc_1s[i]

        deviation_pct = (current_price - oracle_price) / oracle_price * 100
        if abs(deviation_pct) < deviation_thresh:
            continue

        direction = 'up' if deviation_pct > 0 else 'down'
        result = simulate_trade(t_now, direction, exit_mode)
        if result is None:
            continue

        last_entry = t_now
        trades.append(result)

    return trades


# ============================================================
# STRAT 7: Candle Open Momentum
# ============================================================
def strat7_candle_momentum(move_thresh=0.05, cooldown=30, exit_mode='resolution', min_offset=15):
    """Compare current BTC price vs candle open price.
    If deviation >= threshold, buy that direction on Poly."""
    trades = []
    last_entry = 0

    for i in range(len(sample_t)):
        t_now = sample_t[i]
        if t_now - last_entry < cooldown:
            continue

        cs = (int(t_now) // INTERVAL) * INTERVAL
        offset = t_now - cs
        if offset < min_offset or offset > INTERVAL - 30:
            continue

        open_price = candle_opens.get(cs)
        if open_price is None:
            continue

        current_price = btc_1s[i]
        move_pct = (current_price - open_price) / open_price * 100
        if abs(move_pct) < move_thresh:
            continue

        direction = 'up' if move_pct > 0 else 'down'
        result = simulate_trade(t_now, direction, exit_mode)
        if result is None:
            continue

        last_entry = t_now
        trades.append(result)

    return trades


def report(name, trades, shares=100):
    if not trades:
        print(f"=== {name} ===  NO TRADES\n")
        return
    n = len(trades)
    wins = sum(1 for t in trades if t['pnl'] > 0)
    net = sum(t['pnl'] for t in trades) * shares
    avg_win = np.mean([t['pnl'] for t in trades if t['pnl'] > 0]) * shares if wins else 0
    avg_loss = np.mean([t['pnl'] for t in trades if t['pnl'] <= 0]) * shares if n - wins > 0 else 0
    worst = min(t['pnl'] for t in trades) * shares
    avg_cost = np.mean([t['cost'] for t in trades]) * shares
    daily = net / hours * 24

    print(f"=== {name} ===")
    print(f"  Trades: {n} ({n/hours*24:.0f}/day) | WR: {100*wins/n:.1f}%")
    print(f"  Avg cost: ${avg_cost:.2f} | Net PnL: ${net:+,.2f} | Daily: ${daily:+,.0f}")
    print(f"  Avg win: ${avg_win:+.2f} | Avg loss: ${avg_loss:+.2f} | Worst: ${worst:+.2f}")
    print()


print("=" * 70)
print("  INEFFICIENCY BACKTEST — BTC 5m")
print("=" * 70)

# Strat 5: Latency Arb at different exit horizons
for exit_mode in ['30s', '60s', 'resolution']:
    trades = strat5_latency(lookback=15, move_thresh=0.05, cooldown=30, exit_mode=exit_mode)
    report(f"Strat 5 (Latency Arb, exit={exit_mode})", trades)

print("-" * 70)

# Strat 6: Oracle Lag at different oracle intervals
for interval in [30, 60, 120]:
    for exit_mode in ['30s', 'resolution']:
        trades = strat6_oracle_lag(oracle_interval=interval, deviation_thresh=0.05, cooldown=30, exit_mode=exit_mode)
        report(f"Strat 6 (Oracle Lag {interval}s, exit={exit_mode})", trades)

print("-" * 70)

# Strat 7: Candle Open Momentum
for thresh in [0.03, 0.05, 0.10]:
    trades = strat7_candle_momentum(move_thresh=thresh, cooldown=30, exit_mode='resolution')
    report(f"Strat 7 (Candle Momentum >= {thresh}%, hold to resolution)", trades)

print("-" * 70)

# Strat 7 with early exit
for thresh in [0.05, 0.10]:
    for exit_mode in ['30s', '60s']:
        trades = strat7_candle_momentum(move_thresh=thresh, cooldown=30, exit_mode=exit_mode)
        report(f"Strat 7 (Candle Momentum >= {thresh}%, exit={exit_mode})", trades)
