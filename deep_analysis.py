import json
from datetime import datetime, timezone
from collections import defaultdict

with open("trades_24h.json") as f:
    raw = json.load(f)

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
    return "1h"  # "unknown" titles are 1h markets

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

trades = []
for t in raw:
    if not isinstance(t, dict): continue
    trades.append({
        "dt": datetime.fromtimestamp(t.get("timestamp", 0), tz=timezone.utc),
        "side": t.get("side"),
        "outcome": t.get("outcome"),
        "price": float(t.get("price") or 0),
        "usdc": float(t.get("usdcSize") or 0),
        "size": float(t.get("size") or 0),
        "asset": get_asset(t),
        "tf": get_tf(t),
        "title": t.get("title",""),
    })

buys = [t for t in trades if t["side"] == "BUY"]
sells = [t for t in trades if t["side"] == "SELL"]

print(f"{'='*60}")
print(f"  FULL 24H ANALYSIS ({len(trades)} trades)")
print(f"{'='*60}")
print(f"  Buys: {len(buys)} | Sells: {len(sells)}")
print(f"  Total USDC in: ${sum(t['usdc'] for t in buys):,.2f}")

# Entry price distribution — what odds is he buying at?
print(f"\n  BUY PRICE DISTRIBUTION (what odds he enters at):")
buckets = defaultdict(int)
for t in buys:
    p = t["price"]
    if p < 0.10: buckets["<10¢"] += 1
    elif p < 0.20: buckets["10-20¢"] += 1
    elif p < 0.30: buckets["20-30¢"] += 1
    elif p < 0.40: buckets["30-40¢"] += 1
    elif p < 0.50: buckets["40-50¢"] += 1
    elif p < 0.60: buckets["50-60¢"] += 1
    elif p < 0.70: buckets["60-70¢"] += 1
    elif p < 0.80: buckets["70-80¢"] += 1
    elif p < 0.90: buckets["80-90¢"] += 1
    else: buckets["90¢+"] += 1
for k in ["<10¢","10-20¢","20-30¢","30-40¢","40-50¢","50-60¢","60-70¢","70-80¢","80-90¢","90¢+"]:
    count = buckets[k]
    bar = "█" * (count // 20)
    print(f"    {k:8s}: {count:4d} {bar}")

# By timeframe breakdown
print(f"\n  BY TIMEFRAME:")
for tf in ["5m","15m","1h","4h"]:
    tf_buys = [t for t in buys if t["tf"] == tf]
    tf_sells = [t for t in sells if t["tf"] == tf]
    if not tf_buys: continue
    avg_price = sum(t["price"] for t in tf_buys) / len(tf_buys)
    usdc = sum(t["usdc"] for t in tf_buys)
    up_count = sum(1 for t in tf_buys if t["outcome"] == "Up")
    down_count = sum(1 for t in tf_buys if t["outcome"] == "Down")
    print(f"\n    {tf}:")
    print(f"      Buys: {len(tf_buys)} | Sells: {len(tf_sells)}")
    print(f"      Avg entry price: {avg_price:.3f} ({avg_price*100:.1f}¢)")
    print(f"      Total USDC: ${usdc:,.2f}")
    print(f"      Up bets: {up_count} | Down bets: {down_count}")

    # Price distribution for this tf
    print(f"      Entry price breakdown:")
    for k in ["<10¢","10-20¢","20-30¢","30-40¢","40-50¢","50-60¢","60-70¢","70-80¢","80-90¢","90¢+"]:
        ranges = {"<10¢":(0,0.10),"10-20¢":(0.10,0.20),"20-30¢":(0.20,0.30),
                  "30-40¢":(0.30,0.40),"40-50¢":(0.40,0.50),"50-60¢":(0.50,0.60),
                  "60-70¢":(0.60,0.70),"70-80¢":(0.70,0.80),"80-90¢":(0.80,0.90),"90¢+":(0.90,1.01)}
        lo, hi = ranges[k]
        c = sum(1 for t in tf_buys if lo <= t["price"] < hi)
        if c > 0:
            print(f"        {k}: {c}")

# SELL analysis — what price are they exiting at?
print(f"\n  SELL PRICE DISTRIBUTION (cutting losers or taking profit):")
sell_buckets = defaultdict(int)
for t in sells:
    p = t["price"]
    if p < 0.10: sell_buckets["<10¢"] += 1
    elif p < 0.20: sell_buckets["10-20¢"] += 1
    elif p < 0.30: sell_buckets["20-30¢"] += 1
    elif p < 0.40: sell_buckets["30-40¢"] += 1
    elif p < 0.50: sell_buckets["40-50¢"] += 1
    elif p < 0.60: sell_buckets["50-60¢"] += 1
    elif p < 0.70: sell_buckets["60-70¢"] += 1
    elif p < 0.80: sell_buckets["70-80¢"] += 1
    elif p < 0.90: sell_buckets["80-90¢"] += 1
    else: sell_buckets["90¢+"] += 1
for k in ["<10¢","10-20¢","20-30¢","30-40¢","40-50¢","50-60¢","60-70¢","70-80¢","80-90¢","90¢+"]:
    count = sell_buckets[k]
    if count > 0:
        print(f"    {k:8s}: {count}")

# Avg USDC per trade by asset
print(f"\n  AVG USDC PER BUY BY ASSET:")
for asset in ["BTC","ETH","SOL","XRP"]:
    ab = [t for t in buys if t["asset"] == asset]
    if ab:
        print(f"    {asset}: ${sum(t['usdc'] for t in ab)/len(ab):.2f} avg | {len(ab)} trades | ${sum(t['usdc'] for t in ab):,.0f} total")
