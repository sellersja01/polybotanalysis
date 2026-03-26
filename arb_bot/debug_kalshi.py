"""Quick debug — find the right Kalshi series for 15m up/down markets."""
import asyncio, sys, json, base64, time
import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def headers(key_id, pk, method, path):
    ts = int(time.time() * 1000)
    sig = pk.sign(f"{ts}{method}{path}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

async def main(key_id, key_path):
    pk = load_key(key_path)
    async with aiohttp.ClientSession() as s:

        # 1. List all series to find 15m up/down
        print("=== ALL SERIES (searching for up/down) ===")
        h = headers(key_id, pk, "GET", "/series")
        async with s.get("https://api.elections.kalshi.com/trade-api/v2/series", headers=h) as r:
            data = await r.json()
        series_list = data.get("series", [])
        for ser in series_list:
            title = ser.get("title","").lower()
            ticker = ser.get("ticker","")
            if any(x in title for x in ["up", "down", "btc", "eth", "sol", "xrp", "crypto", "bitcoin", "ethereum"]):
                print(f"  {ticker:<30} {ser.get('title','')}")

        print(f"\nTotal series: {len(series_list)}")

        # 2. Dump one raw Kalshi market to see all fields
        print("\n=== RAW MARKET SAMPLE (first KXBTC market) ===")
        path = "/markets"
        h = headers(key_id, pk, "GET", path)
        async with s.get("https://api.elections.kalshi.com/trade-api/v2" + path,
                         headers=h, params={"status":"open","series_ticker":"KXBTC","limit":1}) as r:
            data = await r.json()
        markets = data.get("markets", [])
        if markets:
            print(json.dumps(markets[0], indent=2))

asyncio.run(main(sys.argv[1], sys.argv[2]))
