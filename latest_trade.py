import requests, json

wallet = "0xd0d6053c3c37e727402d84c14069780d360993aa"
r = requests.get(f"https://data-api.polymarket.com/activity?user={wallet}&limit=1")
data = r.json()
print(json.dumps(data[0], indent=2))
