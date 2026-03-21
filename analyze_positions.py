import requests, json
from collections import defaultdict

wallet = "0xd0d6053c3c37e727402d84c14069780d360993aa"

print("Fetching positions...")
r = requests.get(f"https://data-api.polymarket.com/positions?user={wallet}&limit=500")
data = r.json()
print(f"Total open positions: {len(data)}")

r2 = requests.get(f"https://data-api.polymarket.com/value?user={wallet}")
v = r2.json()
portfolio_value = v[0].get("value", 0) if isinstance(v, list) else v.get("value", 0)
print(f"Total portfolio value: ${portfolio_value:,.2f}\n")

# Parse positions
positions = []
for p in data:
    slug = p.get("eventSlug") or p.get("slug") or ""
    title = p.get("title", "")

    tf = "unknown"
    for key in ["4h", "1h", "15m", "5m"]:
        if key in slug:
            tf = key
            break
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

    positions.append({
        "asset": asset,
        "tf": tf,
        "outcome": p.get("outcome"),
        "size": float(p.get("size") or 0),
        "avg_price": float(p.get("avgPrice") or 0),
        "cur_price": float(p.get("curPrice") or 0),
        "initial_value": float(p.get("initialValue") or 0),
        "current_value": float(p.get("currentValue") or 0),
        "cash_pnl": float(p.get("cashPnl") or 0),
        "pct_pnl": float(p.get("percentPnl") or 0),
        "title": title,
    })

total_invested = sum(p["initial_value"] for p in positions)
total_current = sum(p["current_value"] for p in positions)
total_pnl = sum(p["cash_pnl"] for p in positions)
winners = [p for p in positions if p["cash_pnl"] > 0]
losers  = [p for p in positions if p["cash_pnl"] <= 0]

print(f"{'='*60}")
print(f"  OPEN POSITIONS SUMMARY")
print(f"{'='*60}")
print(f"  Total positions:     {len(positions)}")
print(f"  Total invested:      ${total_invested:,.2f}")
print(f"  Total current value: ${total_current:,.2f}")
print(f"  Unrealized P&L:      ${total_pnl:,.2f}")
print(f"  Winners / Losers:    {len(winners)} / {len(losers)}")
if positions:
    print(f"  Win rate:            {len(winners)/len(positions)*100:.1f}%")

print(f"\n  By asset:")
by_asset = defaultdict(lambda: {"count": 0, "pnl": 0.0, "invested": 0.0})
for p in positions:
    by_asset[p["asset"]]["count"] += 1
    by_asset[p["asset"]]["pnl"] += p["cash_pnl"]
    by_asset[p["asset"]]["invested"] += p["initial_value"]
for a, v in sorted(by_asset.items(), key=lambda x: -x[1]["pnl"]):
    print(f"    {a:6s}: {v['count']} positions | invested ${v['invested']:,.0f} | P&L ${v['pnl']:,.2f}")

print(f"\n  By timeframe:")
by_tf = defaultdict(lambda: {"count": 0, "pnl": 0.0})
for p in positions:
    by_tf[p["tf"]]["count"] += 1
    by_tf[p["tf"]]["pnl"] += p["cash_pnl"]
for tf, v in sorted(by_tf.items(), key=lambda x: -x[1]["pnl"]):
    print(f"    {tf:8s}: {v['count']} positions | P&L ${v['pnl']:,.2f}")

print(f"\n  TOP 10 WINNERS:")
print(f"  {'Asset':5s} {'TF':5s} {'Dir':5s} {'Avg$':7s} {'Cur$':7s} {'Size':10s} {'P&L':10s} {'%':8s}")
print(f"  {'-'*65}")
for p in sorted(winners, key=lambda x: -x["cash_pnl"])[:10]:
    print(f"  {p['asset']:5s} {p['tf']:5s} {str(p['outcome']):5s} ${p['avg_price']:.3f}  ${p['cur_price']:.3f}  {p['size']:10.1f} ${p['cash_pnl']:>9,.2f} {p['pct_pnl']:>7.1f}%")

print(f"\n  TOP 10 LOSERS:")
print(f"  {'Asset':5s} {'TF':5s} {'Dir':5s} {'Avg$':7s} {'Cur$':7s} {'Size':10s} {'P&L':10s} {'%':8s}")
print(f"  {'-'*65}")
for p in sorted(losers, key=lambda x: x["cash_pnl"])[:10]:
    print(f"  {p['asset']:5s} {p['tf']:5s} {str(p['outcome']):5s} ${p['avg_price']:.3f}  ${p['cur_price']:.3f}  {p['size']:10.1f} ${p['cash_pnl']:>9,.2f} {p['pct_pnl']:>7.1f}%")
