import sqlite3
import pandas as pd
import numpy as np
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────────────
BET_SIZE_USDC = 10.0   # dollars per trade
CUT_THRESHOLD = 0.02   # essentially no cut
MIN_ENTRY     = 0.50   # only buy if odds are above this
MAX_ENTRY     = 0.62   # only buy if odds are below this
TIMEFRAMES    = {
    "5m":  ("C:\\Users\\James\\BTC 5m poly trader\\market_btc_5m.db",  300),
    "15m": ("C:\\Users\\James\\BTC 5m poly trader\\market_btc_15m.db", 900),
}

def load_candles(db_path, candle_seconds):
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("""
        SELECT unix_time, outcome, bid, ask, mid
        FROM polymarket_odds
        ORDER BY unix_time ASC
    """, conn)
    conn.close()

    df["candle_id"] = (df["unix_time"] // candle_seconds).astype(int)
    return df

def backtest(df, candle_seconds, label):
    print(f"\n{'='*55}")
    print(f"  STRATEGY BACKTEST — {label}")
    print(f"{'='*55}")

    candles = df.groupby(["candle_id", "outcome"])

    results = []
    total_trades = 0
    total_pnl = 0.0
    wins = losses = cuts = 0

    candle_ids = df["candle_id"].unique()

    for cid in candle_ids:
        candle_data = df[df["candle_id"] == cid]
        if candle_data.empty:
            continue

        for outcome in ["Up", "Down"]:
            rows = candle_data[candle_data["outcome"] == outcome].reset_index(drop=True)
            if len(rows) < 5:
                continue

            # Entry: use the first datapoint of the candle
            entry_row = rows.iloc[0]
            entry_price = entry_row["ask"]  # we buy at ask

            # Only enter if price is in our cheap zone
            if not (MIN_ENTRY <= entry_price <= MAX_ENTRY):
                continue

            total_trades += 1
            shares = BET_SIZE_USDC / entry_price

            # Simulate: track price through candle
            # Resolution = last mid price of candle (close to 0 or 1)
            last_row = rows.iloc[-1]
            resolution_price = last_row["mid"]

            # Check if we would have cut the position
            cut = False
            cut_price = None
            for _, row in rows.iterrows():
                if row["mid"] <= CUT_THRESHOLD:
                    cut = True
                    cut_price = row["bid"]  # sell at bid when cutting
                    break

            if cut and cut_price is not None:
                # Sold early at cut price
                pnl = (cut_price - entry_price) * shares
                cuts += 1
            else:
                # Held to resolution
                # Resolution: if mid > 0.95 it won (=1.00), else lost (=0.00)
                final = 1.0 if resolution_price >= 0.95 else 0.0
                pnl = (final - entry_price) * shares
                if final >= 0.95:
                    wins += 1
                else:
                    losses += 1

            total_pnl += pnl
            results.append({
                "candle_id": cid,
                "outcome": outcome,
                "entry": entry_price,
                "resolution": resolution_price,
                "cut": cut,
                "pnl": pnl,
            })

    if not results:
        print("  No trades found.")
        return

    rdf = pd.DataFrame(results)
    avg_pnl = rdf["pnl"].mean()
    total_invested = total_trades * BET_SIZE_USDC

    print(f"  Total trades:      {total_trades}")
    print(f"  Wins:              {wins} ({wins/total_trades*100:.1f}%)")
    print(f"  Losses (held):     {losses} ({losses/total_trades*100:.1f}%)")
    print(f"  Cut early:         {cuts} ({cuts/total_trades*100:.1f}%)")
    print(f"  Total invested:    ${total_invested:,.2f}")
    print(f"  Total P&L:         ${total_pnl:,.2f}")
    print(f"  Avg P&L per trade: ${avg_pnl:.3f}")
    print(f"  ROI:               {total_pnl/total_invested*100:.2f}%")

    # Breakdown by entry price bucket
    print(f"\n  P&L by entry price:")
    for lo, hi in [(0.10,0.20),(0.20,0.30),(0.30,0.40),(0.40,0.50),(0.50,0.60)]:
        sub = rdf[(rdf["entry"] >= lo) & (rdf["entry"] < hi)]
        if len(sub) == 0: continue
        wr = len(sub[sub["pnl"] > 0]) / len(sub) * 100
        print(f"    {int(lo*100)}-{int(hi*100)}¢: {len(sub):4d} trades | WR {wr:.0f}% | avg P&L ${sub['pnl'].mean():.3f} | total ${sub['pnl'].sum():.2f}")

    # Best entry zone
    best = rdf.groupby(pd.cut(rdf["entry"], bins=[0.10,0.20,0.30,0.40,0.50,0.60]))["pnl"].mean()
    print(f"\n  Best entry zone: {best.idxmax()}")

for tf, (db_path, seconds) in TIMEFRAMES.items():
    try:
        df = load_candles(db_path, seconds)
        candle_count = df["candle_id"].nunique()
        print(f"\nLoaded {tf}: {len(df):,} rows across {candle_count} candles")
        backtest(df, seconds, tf)
    except Exception as e:
        print(f"Error loading {tf}: {e}")
