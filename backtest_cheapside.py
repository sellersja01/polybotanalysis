"""
backtest_cheapside.py — Replicate the "buy cheap side + expensive side" wallet strategy
========================================================================================
For each candle:
  - Enter cheap side (<= CHEAP_THRESH ask) the FIRST time it crosses that price
  - Enter expensive side (>= EXP_THRESH ask) the FIRST time it crosses that price
  - Hold to settlement, collect $1 if win / $0 if lose
  - Report win rate, avg profit, daily PnL for each scenario
"""
import sqlite3
import numpy as np
from collections import defaultdict

DATABASES = [
    ("market_btc_5m.db",  "BTC 5m"),
    ("market_btc_15m.db", "BTC 15m"),
    ("market_eth_5m.db",  "ETH 5m"),
    ("market_eth_15m.db", "ETH 15m"),
]

# Entry thresholds to test
CHEAP_THRESHOLDS = [0.15, 0.20, 0.25, 0.30]
EXP_THRESHOLDS   = [0.70, 0.75, 0.80, 0.85, 0.90]

def poly_fee(price):
    return price * 0.25 * (price * (1 - price)) ** 2

def pnl_per_100(entry_ask, won):
    """P&L on a $100 position at entry_ask (held to settlement)."""
    shares = 100.0 / entry_ask
    fee    = shares * poly_fee(entry_ask)
    if won:
        return shares - fee - 100   # received $1/share, paid entry + fee
    else:
        return -100 - fee * 0      # lost the $100 (fee already paid at entry)
        # simplify: just return -100 on loss (fee is sunk in the entry cost)

def run(db_path, label):
    conn = sqlite3.connect(db_path)

    # Load all ticks: market_id, unix_time, outcome, bid, ask, mid
    rows = conn.execute("""
        SELECT market_id, unix_time, outcome, bid, ask, mid
        FROM polymarket_odds
        ORDER BY market_id, unix_time
    """).fetchall()
    conn.close()

    if not rows:
        print(f"  {label}: no data")
        return

    # Group by candle (market_id)
    candles = defaultdict(lambda: {"Up": [], "Down": []})
    for market_id, ts, outcome, bid, ask, mid in rows:
        if outcome not in ("Up", "Down"):
            continue
        candles[market_id][outcome].append((ts, bid, ask, mid))

    hours = 0
    if rows:
        all_ts = [r[1] for r in rows]
        hours = (max(all_ts) - min(all_ts)) / 3600

    print(f"\n{'='*65}")
    print(f"  {label}  |  {len(candles):,} candles  |  {hours:.0f} hours")
    print(f"{'='*65}")

    # ── CHEAP SIDE ANALYSIS ──────────────────────────────────────────────
    print(f"\n  CHEAP SIDE (buy when ask <= threshold, hold to settlement)")
    print(f"  {'Thresh':>7} {'Entries':>8} {'Win%':>6} {'Avg$/100':>10} {'$/day':>10}")
    print(f"  {'-'*50}")

    for thresh in CHEAP_THRESHOLDS:
        trades = []
        for cid, sides in candles.items():
            up_ticks   = sides["Up"]
            down_ticks = sides["Down"]
            if not up_ticks or not down_ticks:
                continue

            # Determine winner from last ticks
            last_up_mid   = up_ticks[-1][3]
            last_dn_mid   = down_ticks[-1][3]
            if last_up_mid >= 0.85:
                winner = "Up"
            elif last_dn_mid >= 0.85:
                winner = "Down"
            else:
                continue  # unresolved candle

            # First tick where Up ask <= thresh
            for ts, bid, ask, mid in up_ticks:
                if ask > 0 and ask <= thresh:
                    won = (winner == "Up")
                    trades.append(pnl_per_100(ask, won))
                    break

            # First tick where Down ask <= thresh
            for ts, bid, ask, mid in down_ticks:
                if ask > 0 and ask <= thresh:
                    won = (winner == "Down")
                    trades.append(pnl_per_100(ask, won))
                    break

        if not trades:
            print(f"  <={thresh:.0%}  {'0':>8}")
            continue

        wins    = sum(1 for p in trades if p > 0)
        wr      = wins / len(trades) * 100
        avg_pnl = np.mean(trades)
        per_day = avg_pnl * len(trades) / hours * 24 if hours > 0 else 0

        print(f"  <={thresh:.0%}   {len(trades):>8,}  {wr:>5.1f}%  ${avg_pnl:>8.2f}  ${per_day:>8.2f}")

    # ── EXPENSIVE SIDE ANALYSIS ──────────────────────────────────────────
    print(f"\n  EXPENSIVE SIDE (buy when ask >= threshold, hold to settlement)")
    print(f"  {'Thresh':>7} {'Entries':>8} {'Win%':>6} {'Avg$/100':>10} {'$/day':>10}")
    print(f"  {'-'*50}")

    for thresh in EXP_THRESHOLDS:
        trades = []
        for cid, sides in candles.items():
            up_ticks   = sides["Up"]
            down_ticks = sides["Down"]
            if not up_ticks or not down_ticks:
                continue

            last_up_mid = up_ticks[-1][3]
            last_dn_mid = down_ticks[-1][3]
            if last_up_mid >= 0.85:
                winner = "Up"
            elif last_dn_mid >= 0.85:
                winner = "Down"
            else:
                continue

            # First tick where Up ask >= thresh
            for ts, bid, ask, mid in up_ticks:
                if ask > 0 and ask >= thresh:
                    won = (winner == "Up")
                    trades.append(pnl_per_100(ask, won))
                    break

            # First tick where Down ask >= thresh
            for ts, bid, ask, mid in down_ticks:
                if ask > 0 and ask >= thresh:
                    won = (winner == "Down")
                    trades.append(pnl_per_100(ask, won))
                    break

        if not trades:
            print(f"  >={thresh:.0%}  {'0':>8}")
            continue

        wins    = sum(1 for p in trades if p > 0)
        wr      = wins / len(trades) * 100
        avg_pnl = np.mean(trades)
        per_day = avg_pnl * len(trades) / hours * 24 if hours > 0 else 0

        print(f"  >={thresh:.0%}   {len(trades):>8,}  {wr:>5.1f}%  ${avg_pnl:>8.2f}  ${per_day:>8.2f}")

    # ── COMBINED (cheap + expensive same candle) ─────────────────────────
    print(f"\n  COMBINED: cheap <=0.20 + expensive >=0.80 (same candle, $100 each)")
    combo_trades = []
    for cid, sides in candles.items():
        up_ticks   = sides["Up"]
        down_ticks = sides["Down"]
        if not up_ticks or not down_ticks:
            continue
        last_up_mid = up_ticks[-1][3]
        last_dn_mid = down_ticks[-1][3]
        if last_up_mid >= 0.85:
            winner = "Up"
        elif last_dn_mid >= 0.85:
            winner = "Down"
        else:
            continue

        candle_pnl = 0
        legs = 0

        for outcome, ticks in [("Up", up_ticks), ("Down", down_ticks)]:
            for ts, bid, ask, mid in ticks:
                if ask > 0 and ask <= 0.20:
                    candle_pnl += pnl_per_100(ask, winner == outcome)
                    legs += 1
                    break

        for outcome, ticks in [("Up", up_ticks), ("Down", down_ticks)]:
            for ts, bid, ask, mid in ticks:
                if ask > 0 and ask >= 0.80:
                    candle_pnl += pnl_per_100(ask, winner == outcome)
                    legs += 1
                    break

        if legs > 0:
            combo_trades.append((candle_pnl, legs))

    if combo_trades:
        all_pnl  = [c[0] for c in combo_trades]
        wins     = sum(1 for p in all_pnl if p > 0)
        avg_legs = np.mean([c[1] for c in combo_trades])
        per_day  = sum(all_pnl) / hours * 24 if hours > 0 else 0
        print(f"  Candles with entries: {len(combo_trades):,}")
        print(f"  Avg legs per candle:  {avg_legs:.1f}")
        print(f"  Win rate (candle net > 0): {wins/len(combo_trades)*100:.1f}%")
        print(f"  Avg net PnL/candle:  ${np.mean(all_pnl):+.2f}")
        print(f"  Total PnL:           ${sum(all_pnl):+,.2f}")
        print(f"  Daily PnL ($100/leg): ${per_day:+,.2f}")


for db_path, label in DATABASES:
    try:
        run(db_path, label)
    except Exception as e:
        print(f"  {label}: ERROR — {e}")
        import traceback; traceback.print_exc()
