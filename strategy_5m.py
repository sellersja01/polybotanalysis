import sqlite3
import pandas as pd
import numpy as np

print("Loading data...")

conn = sqlite3.connect("market_data_5m.db")
poly_up = pd.read_sql("SELECT unix_time, market_id, mid, spread FROM polymarket_odds WHERE outcome='Up' ORDER BY unix_time", conn)
poly_down = pd.read_sql("SELECT unix_time, market_id, mid FROM polymarket_odds WHERE outcome='Down' ORDER BY unix_time", conn)
btc = pd.read_sql("SELECT unix_time, price FROM btc_price ORDER BY unix_time", conn)
conn.close()

if poly_up.empty:
    print("No data yet.")
    exit()

print(f"Candles: {poly_up['market_id'].nunique()}")
print()

poly = pd.merge(poly_up, poly_down, on=['unix_time', 'market_id'], suffixes=('_up', '_down'))
poly = poly.sort_values('unix_time').reset_index(drop=True)
btc = btc.sort_values('unix_time').reset_index(drop=True)
poly = pd.merge_asof(poly, btc, on='unix_time', direction='nearest')

TRADE_SIZE = 100

def simulate(name, tp_cents, sl_cents, signal_fn):
    results = []
    tp = tp_cents / 100
    sl = sl_cents / 100

    candles = list(poly.groupby('market_id'))
    candles.sort(key=lambda x: x[1].iloc[0]['unix_time'])

    for idx, (market_id, candle) in enumerate(candles):
        candle = candle.sort_values('unix_time').reset_index(drop=True)
        if len(candle) < 5:
            continue

        prev_candle = candles[idx-1][1].sort_values('unix_time').reset_index(drop=True) if idx > 0 else None
        signal = signal_fn(candle, prev_candle)
        if signal is None:
            continue

        entry_idx, direction = signal
        if entry_idx >= len(candle) - 1:
            continue

        entry_price = candle.loc[entry_idx, 'mid_up'] if direction == 1 else candle.loc[entry_idx, 'mid_down']
        spread = candle.loc[entry_idx, 'spread'] if 'spread' in candle.columns else 0.01
        spread_cost = spread * 2 * TRADE_SIZE

        future = candle.iloc[entry_idx + 1:]
        outcome = 'timeout'
        for _, row in future.iterrows():
            current = row['mid_up'] if direction == 1 else row['mid_down']
            pnl = (current - entry_price) * direction
            if pnl >= tp:
                outcome = 'win'
                break
            elif pnl <= -sl:
                outcome = 'loss'
                break

        if outcome == 'win':
            net = tp * TRADE_SIZE - spread_cost
        elif outcome == 'loss':
            net = -sl * TRADE_SIZE - spread_cost
        else:
            if len(future) > 0:
                last = future.iloc[-1]['mid_up'] if direction == 1 else future.iloc[-1]['mid_down']
                net = (last - entry_price) * direction * TRADE_SIZE - spread_cost
            else:
                net = -spread_cost

        results.append(net)

    if not results:
        print(f"{name}: No signals\n")
        return None

    results = np.array(results)
    wr = (results > 0).mean() * 100
    avg = results.mean()
    total = results.sum()
    print(f"{name}")
    print(f"  Signals: {len(results)} | Win Rate: {wr:.1f}% | Avg P&L: ${avg:.2f} | Total: ${total:.2f}\n")
    return results


# S1: Early Momentum
def s1(c, p):
    open_price = c.iloc[0]['price']
    for i in range(len(c)):
        if c.iloc[i]['unix_time'] - c.iloc[0]['unix_time'] > 120: break
        move = c.iloc[i]['price'] - open_price
        if abs(move) > 30:
            return (i, 1 if move > 0 else -1)
    return None
simulate("S1 - Early Momentum >$30 first 2min | TP10 SL5", 10, 5, s1)

# S2: Fade extreme odds early (mean reversion)
def s2(c, p):
    for i in range(len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 90: break  # only first 90s
        mid = c.iloc[i]['mid_up']
        if mid > 0.82:
            return (i, -1)  # buy Down, expect reversion
        if mid < 0.18:
            return (i, 1)   # buy Up, expect reversion
    return None
simulate("S2 - Fade Extremes >82/<18 in first 90s | TP10 SL5", 10, 5, s2)

# S3: Odds stuck near 50/50 then break
def s3(c, p):
    if len(c) < 10: return None
    for i in range(5, len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 150: break
        recent = c.iloc[i-5:i]['mid_up']
        if recent.std() < 0.03:  # odds been flat/stuck
            move = c.iloc[i]['price'] - c.iloc[0]['price']
            if abs(move) > 25:
                return (i, 1 if move > 0 else -1)
    return None
simulate("S3 - Flat odds then BTC breaks >$25 | TP10 SL5", 10, 5, s3)

# S4: Follow previous candle direction
def s4(c, p):
    if p is None or len(p) < 2: return None
    prev_open = p.iloc[0]['price']
    prev_close = p.iloc[-1]['price']
    prev_dir = 1 if prev_close > prev_open else -1
    open_price = c.iloc[0]['price']
    for i in range(len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 60: break
        move = c.iloc[i]['price'] - open_price
        if abs(move) > 15 and (1 if move > 0 else -1) == prev_dir:
            return (i, prev_dir)
    return None
simulate("S4 - Follow prev candle direction | TP8 SL4", 8, 4, s4)

# S5: Fade previous candle direction (mean reversion across candles)
def s5(c, p):
    if p is None or len(p) < 2: return None
    prev_open = p.iloc[0]['price']
    prev_close = p.iloc[-1]['price']
    prev_dir = 1 if prev_close > prev_open else -1
    fade_dir = -prev_dir
    open_price = c.iloc[0]['price']
    for i in range(len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 60: break
        move = c.iloc[i]['price'] - open_price
        if abs(move) > 10:
            return (i, fade_dir)
    return None
simulate("S5 - Fade prev candle direction | TP8 SL4", 8, 4, s5)

# S6: Buy when odds cheap AND BTC moving in that direction
def s6(c, p):
    open_price = c.iloc[0]['price']
    for i in range(len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 180: break
        move = c.iloc[i]['price'] - open_price
        mid_up = c.iloc[i]['mid_up']
        mid_down = c.iloc[i]['mid_down']
        if move > 20 and mid_up < 0.55:  # BTC up but Up odds still cheap
            return (i, 1)
        if move < -20 and mid_down < 0.55:  # BTC down but Down odds still cheap
            return (i, -1)
    return None
simulate("S6 - BTC moved but odds still cheap | TP12 SL5", 12, 5, s6)

# S7: Odds reverting to 50 from extreme in mid candle
def s7(c, p):
    if len(c) < 10: return None
    for i in range(5, len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed < 60 or elapsed > 240: continue
        prev_mid = c.iloc[i-5]['mid_up']
        curr_mid = c.iloc[i]['mid_up']
        if prev_mid > 0.80 and curr_mid < 0.75:  # was extreme, now reverting
            return (i, -1)  # buy Down
        if prev_mid < 0.20 and curr_mid > 0.25:  # was extreme, now reverting
            return (i, 1)   # buy Up
    return None
simulate("S7 - Odds reverting from extreme mid-candle | TP8 SL4", 8, 4, s7)

# S8: Big BTC move + odds haven't fully priced it yet
def s8(c, p):
    open_price = c.iloc[0]['price']
    for i in range(len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 120: break
        move = c.iloc[i]['price'] - open_price
        mid_up = c.iloc[i]['mid_up']
        expected = 0.5 + move / 400
        expected = max(0.05, min(0.95, expected))
        gap = expected - mid_up
        if move > 40 and gap > 0.08:
            return (i, 1)
        if move < -40 and gap < -0.08:
            return (i, -1)
    return None
simulate("S8 - Big move odds underreacting | TP10 SL5", 10, 5, s8)

# S9: Low volatility candle then sudden move
def s9(c, p):
    if len(c) < 15: return None
    early_prices = c.iloc[:10]['price']
    if early_prices.std() > 15: return None  # skip high vol candles
    for i in range(10, len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 180: break
        move = c.iloc[i]['price'] - c.iloc[0]['price']
        if abs(move) > 30:
            return (i, 1 if move > 0 else -1)
    return None
simulate("S9 - Low vol candle then breakout | TP10 SL5", 10, 5, s9)

# S10: Enter when spread is tight (better execution)
def s10(c, p):
    open_price = c.iloc[0]['price']
    for i in range(len(c)):
        elapsed = c.iloc[i]['unix_time'] - c.iloc[0]['unix_time']
        if elapsed > 120: break
        spread = c.iloc[i]['spread'] if 'spread' in c.columns else 0.01
        move = c.iloc[i]['price'] - open_price
        if abs(move) > 30 and spread <= 0.01:
            return (i, 1 if move > 0 else -1)
    return None
simulate("S10 - Momentum + tight spread | TP10 SL5", 10, 5, s10)
