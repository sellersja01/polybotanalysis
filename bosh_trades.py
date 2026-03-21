import requests
import time
import csv

WALLET = "0x29bc82f761749e67fa00d62896bc6855097b683c"

print(f"Fetching trades for {WALLET}")
trades = []
for offset in range(0, 3000, 100):
    url = f"https://data-api.polymarket.com/activity?user={WALLET}&limit=100&offset={offset}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if not data:
            print(f"  No more data at offset {offset}")
            break
        trades.extend(data)
        print(f"  offset {offset} -> {len(data)} trades (total: {len(trades)})")
        time.sleep(0.3)
    except Exception as e:
        print(f"  Error at offset {offset}: {e}")
        break

print(f"\nTotal trades fetched: {len(trades)}")

# Print all trades to terminal
print(f"\n{'='*100}")
print(f"{'#':<5} {'Timestamp':<22} {'Type':<5} {'Outcome':<6} {'Price':<8} {'Shares':<10} {'USDC':<10} {'Market'}")
print(f"{'='*100}")

for i, t in enumerate(trades):
    ts = t.get("timestamp", t.get("createdAt", ""))
    outcome = t.get("outcome", t.get("side", "?"))
    price = float(t.get("price", t.get("pricePerShare", 0)) or 0)
    size = float(t.get("size", t.get("shares", 0)) or 0)
    usdc = price * size
    ttype = t.get("type", "BUY")
    market = t.get("market", t.get("title", ""))[:55]
    print(f"{i+1:<5} {str(ts):<22} {ttype:<5} {outcome:<6} {price:<8.3f} {size:<10.1f} ${usdc:<9.2f} {market}")

# Save to CSV
fname = "bosh_trades.csv"
if trades:
    keys = ["timestamp", "type", "outcome", "price", "size", "market", "title"]
    with open(fname, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = {
                "timestamp": t.get("timestamp", t.get("createdAt", "")),
                "type": t.get("type", "BUY"),
                "outcome": t.get("outcome", t.get("side", "")),
                "price": float(t.get("price", t.get("pricePerShare", 0)) or 0),
                "size": float(t.get("size", t.get("shares", 0)) or 0),
                "market": t.get("market", t.get("title", "")),
                "title": t.get("title", ""),
            }
            w.writerow(row)
    print(f"\nSaved to {fname}")

# Quick summary
from collections import Counter
outcomes = Counter(t.get("outcome","?") for t in trades)
print(f"\nOutcome breakdown: {dict(outcomes)}")
up_trades = [t for t in trades if "up" in t.get("outcome","").lower()]
down_trades = [t for t in trades if "down" in t.get("outcome","").lower()]
if up_trades:
    avg_up = sum(float(t.get("price",0) or 0) for t in up_trades) / len(up_trades)
    print(f"Up trades:   {len(up_trades)} | avg price {avg_up:.3f}")
if down_trades:
    avg_down = sum(float(t.get("price",0) or 0) for t in down_trades) / len(down_trades)
    print(f"Down trades: {len(down_trades)} | avg price {avg_down:.3f}")
