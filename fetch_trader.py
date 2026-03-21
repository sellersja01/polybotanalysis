import requests
import json
from datetime import datetime, timezone, timedelta

wallet = "0xd0d6053c3c37e727402d84c14069780d360993aa"
url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=500"

r = requests.get(url)
data = r.json()

# Save raw data
with open("trades_raw.json", "w") as f:
    json.dump(data, f, indent=2)

print(f"Total trades fetched: {len(data)}")

# Filter last 24h
now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=24)

recent = []
for t in data:
    ts = t.get("timestamp") or t.get("createdAt") or t.get("time")
    if ts:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt >= cutoff:
            recent.append(t)

print(f"\n=== LAST 24H: {len(recent)} trades ===")
print(json.dumps(recent[:5], indent=2))  # preview first 5

# Print all keys from first trade so we know the schema
if data:
    print("\n=== TRADE SCHEMA (keys) ===")
    print(list(data[0].keys()))
