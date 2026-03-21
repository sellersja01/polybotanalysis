import requests
import csv
from datetime import datetime, timezone

WALLET = "0x61276aba49117fd9299707d5d573652949d5c977"
OUTPUT = "wallet1.csv"

trades = []
for offset in range(0, 1500, 500):
    r = requests.get(
        "https://data-api.polymarket.com/activity",
        params={"user": WALLET, "limit": 500, "offset": offset},
        timeout=10
    )
    if r.status_code != 200 or not r.json():
        break
    data = r.json()
    trades.extend(data)
    print(f"  Fetched {len(trades)} trades...")
    if len(data) < 500:
        break

trades = sorted(trades, key=lambda x: x.get("timestamp", 0))
print(f"Total: {len(trades)} trades")
print(f"Range: {datetime.fromtimestamp(trades[0].get('timestamp',0)/1000 if trades[0].get('timestamp',0)>9999999999 else trades[0].get('timestamp',0), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} → {datetime.fromtimestamp(trades[-1].get('timestamp',0)/1000 if trades[-1].get('timestamp',0)>9999999999 else trades[-1].get('timestamp',0), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

with open(OUTPUT, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["#","timestamp","time_utc","side","outcome","price","size","usdc","market"])
    for i, t in enumerate(trades):
        ts = t.get("timestamp", 0)
        if ts > 9999999999: ts /= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        price = float(t.get("price", 0) or 0)
        size  = float(t.get("size", 0) or 0)
        usdc  = float(t.get("usdcAmt", 0) or price*size)
        w.writerow([i+1, ts, dt, t.get("side",""), t.get("outcome",""),
                    price, size, usdc, t.get("market", t.get("title",""))])

print(f"Saved to {OUTPUT}")

up   = [float(t.get("price",0)) for t in trades if t.get("outcome")=="Up" and t.get("price")]
down = [float(t.get("price",0)) for t in trades if t.get("outcome")=="Down" and t.get("price")]
if up: print(f"Up:   {len(up)} trades | avg {sum(up)/len(up):.3f}")
if down: print(f"Down: {len(down)} trades | avg {sum(down)/len(down):.3f}")
if up and down: print(f"Combined avg: {sum(up)/len(up)+sum(down)/len(down):.3f}")
