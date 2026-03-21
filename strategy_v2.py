import sqlite3
import pandas as pd
import numpy as np

DB_5M  = r"C:\Users\James\BTC 5m poly trader\market_btc_5m.db"
DB_15M = r"C:\Users\James\BTC 5m poly trader\market_btc_15m.db"

def load_data(db_path, candle_seconds):
    conn = sqlite3.connect(db_path)

    odds = pd.read_sql_query("""
        SELECT unix_time, outcome, bid, ask, mid, spread, market_id
        FROM polymarket_odds ORDER BY unix_time ASC
    """, conn)

    price = pd.read_sql_query("""
        SELECT unix_time, price FROM asset_price ORDER BY unix_time ASC
    """, conn)
    conn.close()

    odds["candle_id"] = (odds["unix_time"] // candle_seconds).astype(int)
    price["candle_id"] = (price["unix_time"] // candle_seconds).astype(int)

    # Pivot odds to have Up and Down side by side
    up   = odds[odds["outcome"] == "Up"].copy()
    down = odds[odds["outcome"] == "Down"].copy()

    up   = up.rename(columns={"mid":"mid_up","bid":"bid_up","ask":"ask_up","spread":"spread_up"})
    down = down.rename(columns={"mid":"mid_down","bid":"bid_down","ask":"ask_down","spread":"spread_down"})

    merged = pd.merge_asof(
        up[["unix_time","candle_id","mid_up","bid_up","ask_up","spread_up"]].sort_values("unix_time"),
        down[["unix_time","mid_down","bid_down","ask_down","spread_down"]].sort_values("unix_time"),
        on="unix_time", direction="nearest", tolerance=2
    )

    merged = pd.merge_asof(
        merged.sort_values("unix_time"),
        price[["unix_time","price"]].sort_values("unix_time"),
        on="unix_time", direction="nearest", tolerance=5
    )

    merged.dropna(inplace=True)
    return merged

def build_candles(df, candle_seconds):
    candles = []
    for cid, group in df.groupby("candle_id"):
        group = group.sort_values("unix_time")
        if len(group) < 10:
            continue

        open_price   = group["price"].iloc[0]
        close_price  = group["price"].iloc[-1]
        high_price   = group["price"].max()
        low_price    = group["price"].min()
        btc_change   = close_price - open_price
        btc_change_2m = group[group["unix_time"] <= group["unix_time"].iloc[0] + 120]["price"].iloc[-1] - open_price if len(group[group["unix_time"] <= group["unix_time"].iloc[0] + 120]) > 0 else 0

        open_mid_up  = group["mid_up"].iloc[0]
        close_mid_up = group["mid_up"].iloc[-1]
        avg_spread   = group["spread_up"].mean()

        # Resolution: last mid_up value — close to 1 = Up won, close to 0 = Down won
        resolution_up = 1.0 if close_mid_up >= 0.95 else 0.0

        candles.append({
            "candle_id":     cid,
            "open_price":    open_price,
            "close_price":   close_price,
            "btc_change":    btc_change,
            "btc_change_2m": btc_change_2m,
            "open_mid_up":   open_mid_up,
            "close_mid_up":  close_mid_up,
            "avg_spread":    avg_spread,
            "resolution_up": resolution_up,
            "n_rows":        len(group),
        })
    return pd.DataFrame(candles)

def test_strategy(candles, name, signal_col, direction, tp=0.10, sl=0.05, bet=10.0):
    """direction: 'Up' or 'Down'. Signal col is boolean."""
    signals = candles[candles[signal_col]].copy()
    if len(signals) == 0:
        return

    wins = losses = 0
    total_pnl = 0.0

    for _, row in signals.iterrows():
        won_up = row["resolution_up"] == 1.0
        if direction == "Up":
            won = won_up
            entry = row["open_mid_up"]
        else:
            won = not won_up
            entry = 1 - row["open_mid_up"]

        if entry <= 0.01 or entry >= 0.99:
            continue

        shares = bet / entry
        if won:
            pnl = (1.0 - entry) * shares
            wins += 1
        else:
            pnl = (0.0 - entry) * shares
            losses += 1
        total_pnl += pnl

    total = wins + losses
    if total == 0:
        return
    wr = wins / total * 100
    avg_pnl = total_pnl / total
    print(f"  {name:45s} | n={total:4d} | WR={wr:5.1f}% | avg=${avg_pnl:+.3f} | total=${total_pnl:+.2f}")

def run(db_path, candle_seconds, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")

    df = load_data(db_path, candle_seconds)
    candles = build_candles(df, candle_seconds)
    print(f"  Candles: {len(candles)}\n")

    if len(candles) < 20:
        print("  Not enough candles.")
        return

    # Lag features
    candles["prev_resolution"] = candles["resolution_up"].shift(1)
    candles["prev_btc_change"] = candles["btc_change"].shift(1)
    candles["prev_mid_up"]     = candles["open_mid_up"].shift(1)
    candles.dropna(inplace=True)

    # ── STRATEGIES ──────────────────────────────────────────────────────

    # S1: BTC moved >$30 in first 2min, follow direction
    candles["s1_up"]   = candles["btc_change_2m"] > 30
    candles["s1_down"] = candles["btc_change_2m"] < -30
    test_strategy(candles, "S1 Early Momentum UP   (BTC +$30 in 2m)", "s1_up",   "Up")
    test_strategy(candles, "S1 Early Momentum DOWN (BTC -$30 in 2m)", "s1_down", "Down")

    # S2: BTC moved >$50, strong momentum
    candles["s2_up"]   = candles["btc_change_2m"] > 50
    candles["s2_down"] = candles["btc_change_2m"] < -50
    test_strategy(candles, "S2 Strong Momentum UP  (BTC +$50 in 2m)", "s2_up",   "Up")
    test_strategy(candles, "S2 Strong Momentum DOWN(BTC -$50 in 2m)", "s2_down", "Down")

    # S3: Odds cheap but BTC already moved (S6 original)
    candles["s3_up"]   = (candles["btc_change_2m"] > 20) & (candles["open_mid_up"] < 0.55)
    candles["s3_down"] = (candles["btc_change_2m"] < -20) & (candles["open_mid_up"] > 0.45)
    test_strategy(candles, "S3 BTC moved odds cheap UP",               "s3_up",   "Up")
    test_strategy(candles, "S3 BTC moved odds cheap DOWN",             "s3_down", "Down")

    # S4: Opening odds extreme fade
    candles["s4_up"]   = candles["open_mid_up"] < 0.25
    candles["s4_down"] = candles["open_mid_up"] > 0.75
    test_strategy(candles, "S4 Fade extreme open UP  (open<25c)",      "s4_up",   "Up")
    test_strategy(candles, "S4 Fade extreme open DOWN(open>75c)",      "s4_down", "Down")

    # S5: Previous candle strongly won Up, follow momentum
    candles["s5_up"]   = (candles["prev_resolution"] == 1.0) & (candles["prev_btc_change"] > 20)
    candles["s5_down"] = (candles["prev_resolution"] == 0.0) & (candles["prev_btc_change"] < -20)
    test_strategy(candles, "S5 Prev candle Up + BTC up, follow",       "s5_up",   "Up")
    test_strategy(candles, "S5 Prev candle Down + BTC down, follow",   "s5_down", "Down")

    # S6: Previous candle won, fade (mean reversion)
    candles["s6_up"]   = candles["prev_resolution"] == 0.0
    candles["s6_down"] = candles["prev_resolution"] == 1.0
    test_strategy(candles, "S6 Mean revert after Down candle -> Up",   "s6_up",   "Up")
    test_strategy(candles, "S6 Mean revert after Up candle -> Down",   "s6_down", "Down")

    # S7: Low spread entry (tight market = better pricing)
    candles["s7_up"]   = (candles["avg_spread"] < 0.015) & (candles["btc_change_2m"] > 20)
    candles["s7_down"] = (candles["avg_spread"] < 0.015) & (candles["btc_change_2m"] < -20)
    test_strategy(candles, "S7 Tight spread + BTC up",                 "s7_up",   "Up")
    test_strategy(candles, "S7 Tight spread + BTC down",               "s7_down", "Down")

    # S8: BTC moved >$40, odds still lagging
    candles["s8_up"]   = (candles["btc_change_2m"] > 40) & (candles["open_mid_up"] < 0.60)
    candles["s8_down"] = (candles["btc_change_2m"] < -40) & (candles["open_mid_up"] > 0.40)
    test_strategy(candles, "S8 Big move odds lagging UP",              "s8_up",   "Up")
    test_strategy(candles, "S8 Big move odds lagging DOWN",            "s8_down", "Down")

run(DB_5M,  300, "BTC 5M STRATEGIES")
run(DB_15M, 900, "BTC 15M STRATEGIES")
