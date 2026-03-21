import sqlite3
import pandas as pd
import numpy as np

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

    odds["candle_id"]  = (odds["unix_time"] // candle_seconds).astype(int)
    price["candle_id"] = (price["unix_time"] // candle_seconds).astype(int)

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
    avg_price = df["price"].mean()
    scale = avg_price / 80000
    thresh_small = max(0.05, 30 * scale)
    thresh_large = max(0.10, 50 * scale)

    candles = []
    for cid, group in df.groupby("candle_id"):
        group = group.sort_values("unix_time")
        if len(group) < 10:
            continue

        open_price  = group["price"].iloc[0]
        close_price = group["price"].iloc[-1]
        price_change = close_price - open_price

        early = group[group["unix_time"] <= group["unix_time"].iloc[0] + 120]
        price_change_2m = (early["price"].iloc[-1] - open_price) if len(early) > 0 else 0

        open_mid_up  = group["mid_up"].iloc[0]
        close_mid_up = group["mid_up"].iloc[-1]
        avg_spread   = group["spread_up"].mean()
        resolution_up = 1.0 if close_mid_up >= 0.95 else 0.0

        candles.append({
            "candle_id":       cid,
            "open_price":      open_price,
            "price_change":    price_change,
            "price_change_2m": price_change_2m,
            "open_mid_up":     open_mid_up,
            "close_mid_up":    close_mid_up,
            "avg_spread":      avg_spread,
            "resolution_up":   resolution_up,
            "thresh_small":    thresh_small,
            "thresh_large":    thresh_large,
        })
    return pd.DataFrame(candles)

def test_strategy(candles, name, signal_col, direction, bet=10.0):
    signals = candles[candles[signal_col]].copy()
    if len(signals) < 5:
        return
    wins = losses = 0
    total_pnl = 0.0
    for _, row in signals.iterrows():
        won_up = row["resolution_up"] == 1.0
        won = won_up if direction == "Up" else not won_up
        entry = row["open_mid_up"] if direction == "Up" else 1 - row["open_mid_up"]
        if entry <= 0.01 or entry >= 0.99:
            continue
        shares = bet / entry
        pnl = (1.0 - entry) * shares if won else (0.0 - entry) * shares
        total_pnl += pnl
        if won: wins += 1
        else: losses += 1
    total = wins + losses
    if total == 0: return
    wr = wins / total * 100
    avg_pnl = total_pnl / total
    print(f"  {name:48s} | n={total:4d} | WR={wr:5.1f}% | avg=${avg_pnl:+.3f} | total=${total_pnl:+.2f}")

def run(db_path, candle_seconds, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    df = load_data(db_path, candle_seconds)
    candles = build_candles(df, candle_seconds)
    print(f"  Candles: {len(candles)}")
    if len(candles) < 20:
        print("  Not enough candles.")
        return

    candles["prev_resolution"] = candles["resolution_up"].shift(1)
    candles["prev_price_change"] = candles["price_change"].shift(1)
    candles.dropna(inplace=True)

    ts = candles["thresh_small"].iloc[0]
    tl = candles["thresh_large"].iloc[0]
    print(f"  Price thresholds: small=${ts:.2f} large=${tl:.2f}\n")

    candles["s1_up"]   = candles["price_change_2m"] > ts
    candles["s1_down"] = candles["price_change_2m"] < -ts
    test_strategy(candles, "S1 Early Momentum UP",   "s1_up",   "Up")
    test_strategy(candles, "S1 Early Momentum DOWN", "s1_down", "Down")

    candles["s2_up"]   = candles["price_change_2m"] > tl
    candles["s2_down"] = candles["price_change_2m"] < -tl
    test_strategy(candles, "S2 Strong Momentum UP",   "s2_up",   "Up")
    test_strategy(candles, "S2 Strong Momentum DOWN", "s2_down", "Down")

    candles["s3_up"]   = (candles["price_change_2m"] > ts*0.7) & (candles["open_mid_up"] < 0.55)
    candles["s3_down"] = (candles["price_change_2m"] < -ts*0.7) & (candles["open_mid_up"] > 0.45)
    test_strategy(candles, "S3 Move + odds cheap UP",   "s3_up",   "Up")
    test_strategy(candles, "S3 Move + odds cheap DOWN", "s3_down", "Down")

    candles["s4_up"]   = candles["open_mid_up"] < 0.25
    candles["s4_down"] = candles["open_mid_up"] > 0.75
    test_strategy(candles, "S4 Fade extreme open UP  (open<25c)", "s4_up",   "Up")
    test_strategy(candles, "S4 Fade extreme open DOWN(open>75c)", "s4_down", "Down")

    candles["s5_up"]   = (candles["prev_resolution"] == 1.0) & (candles["prev_price_change"] > ts)
    candles["s5_down"] = (candles["prev_resolution"] == 0.0) & (candles["prev_price_change"] < -ts)
    test_strategy(candles, "S5 Follow prev candle UP",   "s5_up",   "Up")
    test_strategy(candles, "S5 Follow prev candle DOWN", "s5_down", "Down")

    candles["s6_up"]   = candles["prev_resolution"] == 0.0
    candles["s6_down"] = candles["prev_resolution"] == 1.0
    test_strategy(candles, "S6 Mean revert after Down -> UP",   "s6_up",   "Up")
    test_strategy(candles, "S6 Mean revert after Up  -> DOWN",  "s6_down", "Down")

    candles["s7_up"]   = (candles["avg_spread"] < 0.015) & (candles["price_change_2m"] > ts)
    candles["s7_down"] = (candles["avg_spread"] < 0.015) & (candles["price_change_2m"] < -ts)
    test_strategy(candles, "S7 Tight spread + move UP",   "s7_up",   "Up")
    test_strategy(candles, "S7 Tight spread + move DOWN", "s7_down", "Down")

    candles["s8_up"]   = (candles["price_change_2m"] > tl) & (candles["open_mid_up"] < 0.60)
    candles["s8_down"] = (candles["price_change_2m"] < -tl) & (candles["open_mid_up"] > 0.40)
    test_strategy(candles, "S8 Big move odds lagging UP",   "s8_up",   "Up")
    test_strategy(candles, "S8 Big move odds lagging DOWN", "s8_down", "Down")

BASE = r"C:\Users\James\polybotanalysis"
markets = [
    (f"{BASE}\\market_btc_5m.db", 300, "BTC 5M"),
]
for db, seconds, label in markets:
    try:
        run(db, seconds, label)
    except Exception as e:
        print(f"\nError loading {label}: {e}")
