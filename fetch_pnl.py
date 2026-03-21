import requests, json

wallet = "0xd0d6053c3c37e727402d84c14069780d360993aa"

# Try portfolio/positions endpoint for P&L
endpoints = [
    f"https://data-api.polymarket.com/portfolio?user={wallet}",
    f"https://data-api.polymarket.com/positions?user={wallet}&limit=500",
    f"https://data-api.polymarket.com/value?user={wallet}",
    f"https://gamma-api.polymarket.com/positions?user={wallet}&limit=500",
]

for url in endpoints:
    print(f"\nTrying: {url}")
    try:
        r = requests.get(url, timeout=5)
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if data:
                print(json.dumps(data[0] if isinstance(data, list) else data, indent=2))
                print(f"...total records: {len(data) if isinstance(data, list) else 1}")
    except Exception as e:
        print(f"Error: {e}")
