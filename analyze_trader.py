import requests
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

wallet = "0xd0d6053c3c37e727402d84c14069780d360993aa"

print("Fetching trades...")
r = requests.get(f"https://data-api.polymarket.com/activity?user={wallet}&limit=500")
data = r.json()
print(f"Total trades fetched: {len(data)}")

trades = []
for t in data:
    ts = t.get("timestamp", 0)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None

    slug = t.get("eventSlug") or t.get("slug") or ""
    title = t.get("title", "")

    # Timeframe from slug
    tf = "unknown"
    for key in ["4h", "1h", "15m", "5m"]:
        if key in slug:
            tf = key
            break

    # Fallback: detect from title time window
    if tf == "unknown" and "-" in title and "ET" in title:
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
            if diff == 5: tf = "5m"
            elif diff == 15: tf = "15m"
            elif diff == 60: tf = "1h"
            elif diff == 240: tf = "4h"
        except:
            pass

    # Asset
    asset = "unknown"
    for a, keys in {
        "BTC": ["btc", "bitcoin"],
        "ETH": ["eth", "ethereum"],
        "SOL": ["sol", "solana"],
        "XRP": ["xrp"],
    }.items():
        if any(k in slug.lower() or k in title.lower() for k in keys):
            asset = a
            break

    trades.append({
        "dt": dt,
        "side": t.get("side"),
        "outcome": t.get("outcome"),
        "price": float(t.get("price") or 0),
        "size": float(t.get("size") or 0),
        "usdc": float(t.get("usdcSize") or 0),
        "asset": asset,
        "tf": tf,
        "title": title,
    })

now = datetime.now(timezone.utc)

def analyze(trade_list, label):
    if not trade_list:
        print(f"\n=== {label}: No trades ===")
        return

    print(f"\n{'='*55}")
    print(f"  {label}")
    print(f"{'='*55}")

    buys  = [t for t in trade_list if t["side"] == "BUY"]
    sells = [t for t in trade_list if t["side"] == "SELL"]
    total_usdc = sum(t["usdc"] for t in buys)

    print(f"  Total trades:       {len(trade_list)}")
    print(f"  Buy / Sell:         {len(buys)} / {len(sells)}")
    print(f"  Total USDC in buys: ${total_usdc:,.2f}")
    if buys:
        print(f"  Avg buy size:       ${total_usdc/len(buys):.2f}")
        print(f"  Avg buy price:      ${sum(t['price'] for t in buys)/len(buys):.3f}")

    print(f"\n  By asset:")
    by_asset = defaultdict(int)
    for t in trade_list: by_asset[t["asset"]] += 1
    for a, c in sorted(by_asset.items(), key=lambda x: -x[1]):
        print(f"    {a:8s}: {c}")

    print(f"\n  By timeframe:")
    by_tf = defaultdict(int)
    for t in trade_list: by_tf[t["tf"]] += 1
    for tf, c in sorted(by_tf.items(), key=lambda x: -x[1]):
        print(f"    {tf:8s}: {c}")

    print(f"\n  By direction (outcome):")
    by_dir = defaultdict(int)
    for t in trade_list: by_dir[t["outcome"]] += 1
    for d, c in sorted(by_dir.items(), key=lambda x: -x[1]):
        print(f"    {str(d):8s}: {c}")

    print(f"\n  Last 15 trades:")
    print(f"  {'Time (UTC)':17s} {'Asset':5s} {'TF':6s} {'Side':5s} {'Dir':5s} {'Price':7s} {'USDC':8s}")
    print(f"  {'-'*62}")
    for t in sorted(trade_list, key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:15]:
        dt_str = t["dt"].strftime("%m-%d %H:%M") if t["dt"] else "?"
        print(f"  {dt_str:17s} {t['asset']:5s} {t['tf']:6s} {t['side']:5s} {str(t['outcome']):5s} ${t['price']:.3f}  ${t['usdc']:.2f}")

analyze([t for t in trades if t["dt"] and t["dt"] >= now - timedelta(hours=24)], "LAST 24 HOURS")
analyze([t for t in trades if t["dt"] and t["dt"] >= now - timedelta(days=7)],   "LAST 7 DAYS")
analyze(trades, "ALL TIME (500 trades)")
