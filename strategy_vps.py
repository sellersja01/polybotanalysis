import sqlite3
import csv
from datetime import datetime, timezone

BASE = "/home/opc"

MARKETS = [
    (f"{BASE}/market_btc_5m.db",  300,  "BTC 5M"),
    (f"{BASE}/market_btc_15m.db", 900,  "BTC 15M"),
    (f"{BASE}/market_eth_5m.db",  300,  "ETH 5M"),
    (f"{BASE}/market_eth_15m.db", 900,  "ETH 15M"),
    (f"{BASE}/market_sol_5m.db",  300,  "SOL 5M"),
    (f"{BASE}/market_sol_15m.db", 900,  "SOL 15M"),
    (f"{BASE}/market_xrp_5m.db",  300,  "XRP 5M"),
    (f"{BASE}/market_xrp_15m.db", 900,  "XRP 15M"),
]

def build_candles_fast(db_path, candle_seconds):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=10000")

    # get avg price for thresholds
    avg_price = conn.execute("SELECT AVG(price) FROM asset_price").fetchone()[0] or 80000
    scale = avg_price / 80000
    ts = max(0.05, 30 * scale)
    tl = max(0.10, 50 * scale)

    # load all odds in one query, grouped by candle
    print(f"  Loading odds...", flush=True)
    odds_rows = conn.execute(f"""
        SELECT CAST(unix_time / {candle_seconds} AS INT) as cid,
               outcome, unix_time, mid, spread
        FROM polymarket_odds
        ORDER BY cid, unix_time ASC
    """).fetchall()

    print(f"  Loading prices...", flush=True)
    price_rows = conn.execute(f"""
        SELECT CAST(unix_time / {candle_seconds} AS INT) as cid,
               unix_time, price
        FROM asset_price
        ORDER BY cid, unix_time ASC
    """).fetchall()
    conn.close()

    # group by candle_id
    from collections import defaultdict
    odds_by_candle  = defaultdict(list)
    price_by_candle = defaultdict(list)

    for cid, outcome, ut, mid, spread in odds_rows:
        odds_by_candle[cid].append((outcome, ut, mid, spread))
    for cid, ut, price in price_rows:
        price_by_candle[cid].append((ut, price))

    print(f"  Building candles...", flush=True)
    candles = []
    for cid in sorted(odds_by_candle.keys()):
        odds  = odds_by_candle[cid]
        prices = price_by_candle.get(cid, [])
        if len(odds) < 10 or len(prices) < 2:
            continue

        up_rows   = [(ut, mid, sp) for out, ut, mid, sp in odds if out == "Up"]
        down_rows = [(ut, mid, sp) for out, ut, mid, sp in odds if out == "Down"]
        if not up_rows or not down_rows:
            continue

        candle_start = cid * candle_seconds
        open_price   = prices[0][1]
        close_price  = prices[-1][1]
        price_change = close_price - open_price

        early = [p for ut, p in prices if ut <= candle_start + 120]
        price_change_2m = (early[-1] - open_price) if len(early) >= 2 else 0

        open_mid_up  = up_rows[0][2]
        close_mid_up = up_rows[-1][2]
        avg_spread   = sum(sp for _, _, sp in up_rows) / len(up_rows)
        resolution_up = 1.0 if close_mid_up >= 0.95 else 0.0

        candles.append({
            "candle_id": cid, "open_price": open_price,
            "price_change": price_change, "price_change_2m": price_change_2m,
            "open_mid_up": open_mid_up, "close_mid_up": close_mid_up,
            "avg_spread": avg_spread, "resolution_up": resolution_up,
            "ts": ts, "tl": tl,
        })

    return candles, avg_price, ts, tl

def test_strategy(candles, signal_fn, direction, bet=10.0):
    wins = losses = 0
    total_pnl = 0.0
    for i, row in enumerate(candles):
        if not signal_fn(row, candles, i):
            continue
        won_up = row["resolution_up"] == 1.0
        won = won_up if direction == "Up" else not won_up
        entry = row["open_mid_up"] if direction == "Up" else 1 - row["open_mid_up"]
        if entry <= 0.01 or entry >= 0.99:
            continue
        shares = bet / entry
        pnl = (1.0 - entry) * shares if won else -entry * shares
        total_pnl += pnl
        if won: wins += 1
        else: losses += 1
    total = wins + losses
    if total < 5:
        return None
    wr = wins / total * 100
    return {"n": total, "wr": round(wr,1), "avg_pnl": round(total_pnl/total,3), "total_pnl": round(total_pnl,2)}

def run(db_path, candle_seconds, label, all_results):
    print(f"\n{'='*55}\n  {label}\n{'='*55}", flush=True)
    try:
        candles, avg_price, ts, tl = build_candles_fast(db_path, candle_seconds)
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    print(f"  Candles: {len(candles)} | Avg price: ${avg_price:,.2f} | ts=${ts:.2f} tl=${tl:.2f}\n", flush=True)
    if len(candles) < 20:
        print("  Not enough candles.")
        return

    strategies = [
        ("S1_UP",   lambda r,c,i: r["price_change_2m"] > r["ts"],  "Up"),
        ("S1_DOWN", lambda r,c,i: r["price_change_2m"] < -r["ts"], "Down"),
        ("S2_UP",   lambda r,c,i: r["price_change_2m"] > r["tl"],  "Up"),
        ("S2_DOWN", lambda r,c,i: r["price_change_2m"] < -r["tl"], "Down"),
        ("S3_UP",   lambda r,c,i: r["price_change_2m"] > r["ts"]*0.7 and r["open_mid_up"] < 0.55, "Up"),
        ("S3_DOWN", lambda r,c,i: r["price_change_2m"] < -r["ts"]*0.7 and r["open_mid_up"] > 0.45, "Down"),
        ("S4_UP",   lambda r,c,i: r["open_mid_up"] < 0.25, "Up"),
        ("S4_DOWN", lambda r,c,i: r["open_mid_up"] > 0.75, "Down"),
        ("S5_UP",   lambda r,c,i: i > 0 and c[i-1]["resolution_up"] == 1.0 and c[i-1]["price_change"] > r["ts"], "Up"),
        ("S5_DOWN", lambda r,c,i: i > 0 and c[i-1]["resolution_up"] == 0.0 and c[i-1]["price_change"] < -r["ts"], "Down"),
        ("S6_UP",   lambda r,c,i: i > 0 and c[i-1]["resolution_up"] == 0.0, "Up"),
        ("S6_DOWN", lambda r,c,i: i > 0 and c[i-1]["resolution_up"] == 1.0, "Down"),
        ("S7_UP",   lambda r,c,i: r["avg_spread"] < 0.015 and r["price_change_2m"] > r["ts"], "Up"),
        ("S7_DOWN", lambda r,c,i: r["avg_spread"] < 0.015 and r["price_change_2m"] < -r["ts"], "Down"),
        ("S8_UP",   lambda r,c,i: r["price_change_2m"] > r["tl"] and r["open_mid_up"] < 0.60, "Up"),
        ("S8_DOWN", lambda r,c,i: r["price_change_2m"] < -r["tl"] and r["open_mid_up"] > 0.40, "Down"),
    ]

    for name, fn, direction in strategies:
        r = test_strategy(candles, fn, direction)
        if r:
            print(f"  {name:<12} | n={r['n']:4d} | WR={r['wr']:5.1f}% | avg=${r['avg_pnl']:+.3f} | total=${r['total_pnl']:+.2f}")
            all_results.append({"market": label, "strategy": name, "direction": direction,
                                 "n": r["n"], "wr": r["wr"], "avg_pnl": r["avg_pnl"], "total_pnl": r["total_pnl"]})

if __name__ == "__main__":
    print(f"Strategy analysis — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    all_results = []
    for db_path, candle_sec, label in MARKETS:
        try:
            run(db_path, candle_sec, label, all_results)
        except Exception as e:
            print(f"\nError on {label}: {e}")

    out = "/home/opc/strategy_results.csv"
    if all_results:
        with open(out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["market","strategy","direction","n","wr","avg_pnl","total_pnl"])
            writer.writeheader()
            writer.writerows(all_results)
        print(f"\nSaved to {out}")

    if all_results:
        print("\n=== TOP 10 BY TOTAL PNL ===")
        for r in sorted(all_results, key=lambda x: -x["total_pnl"])[:10]:
            print(f"  {r['market']:<10} {r['strategy']:<12} WR={r['wr']:5.1f}% total=${r['total_pnl']:+.2f}")
