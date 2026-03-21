import requests
import json
from datetime import datetime, timezone, timedelta

wallet = "0xd0d6053c3c37e727402d84c14069780d360993aa"
now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=24)
cutoff_ts = int(cutoff.timestamp())

print(f"Fetching all trades since {cutoff.strftime('%Y-%m-%d %H:%M UTC')}...")

all_trades = []
offset = 0
limit = 500

while True:
    url = f"https://data-api.polymarket.com/activity?user={wallet}&limit={limit}&offset={offset}"
    r = requests.get(url)
    batch = r.json()

    if not batch or not isinstance(batch, list):
        print(f"Stopped at offset {offset}: {batch}")
        break

    batch = [t for t in batch if isinstance(t, dict)]
    if not batch:
        break

    # Filter to last 24h
    recent = [t for t in batch if t.get("timestamp", 0) >= cutoff_ts]
    all_trades.extend(recent)

    oldest_ts = min(t.get("timestamp", 0) for t in batch)
    oldest_dt = datetime.fromtimestamp(oldest_ts, tz=timezone.utc)
    print(f"Offset {offset}: got {len(batch)} trades | oldest: {oldest_dt.strftime('%m-%d %H:%M UTC')} | 24h trades so far: {len(all_trades)}")

    # If oldest trade in batch is older than cutoff, we're done
    if oldest_ts < cutoff_ts:
        print("Reached trades older than 24h, stopping.")
        break

    # If we got less than limit, no more pages
    if len(batch) < limit:
        print("Last page reached.")
        break

    offset += limit

print(f"\nTotal trades in last 24h: {len(all_trades)}")

# Save to file
with open("trades_24h.json", "w") as f:
    json.dump(all_trades, f, indent=2)
print("Saved to trades_24h.json")

# Quick breakdown
from collections import defaultdict

def get_tf(t):
    slug = t.get("eventSlug") or t.get("slug") or ""
    title = t.get("title", "")
    for key in ["4h", "1h", "15m", "5m"]:
        if key in slug:
            return key
    if "ET" in title and "-" in title:
        try:
            times = title.split(" - ")[-1].replace(" ET", "").strip()
            start_s, end_s = times.split("-")
            def to_min(s):
                s = s.strip().upper()
                ampm = "PM" if "PM" in s else "AM"
                s = s.replace("PM","").replace("AM","").strip()
                h, m = s.split(":")
                h, m = int(h), int(m)
                if ampm == "PM" and h != 12: h += 12
                if ampm == "AM" and h == 12: h = 0
                return h * 60 + m
            diff = abs(to_min(end_s) - to_min(start_s))
            if diff == 5: return "5m"
            elif diff == 15: return "15m"
            elif diff == 60: return "1h"
            elif diff == 240: return "4h"
        except:
            pass
    return "unknown"

def get_asset(t):
    slug = t.get("eventSlug") or t.get("slug") or ""
    title = t.get("title", "")
    for a, keys in {
        "BTC": ["btc", "bitcoin"],
        "ETH": ["eth", "ethereum"],
        "SOL": ["sol", "solana"],
        "XRP": ["xrp"],
    }.items():
        if any(k in slug.lower() or k in title.lower() for k in keys):
            return a
    return "unknown"

by_tf = defaultdict(int)
by_asset = defaultdict(int)
buys = sells = 0

for t in all_trades:
    by_tf[get_tf(t)] += 1
    by_asset[get_asset(t)] += 1
    if t.get("side") == "BUY": buys += 1
    else: sells += 1

print(f"\nBy timeframe:")
for tf, c in sorted(by_tf.items(), key=lambda x: -x[1]):
    print(f"  {tf}: {c}")

print(f"\nBy asset:")
for a, c in sorted(by_asset.items(), key=lambda x: -x[1]):
    print(f"  {a}: {c}")

print(f"\nBuys: {buys} | Sells: {sells}")
print(f"Total USDC spent on buys: ${sum(float(t.get('usdcSize',0)) for t in all_trades if t.get('side')=='BUY'):,.2f}")
