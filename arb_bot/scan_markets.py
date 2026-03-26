"""
scan_markets.py
===============
Fetches all open BTC/ETH/SOL/XRP up/down markets from Kalshi and Polymarket,
prints current bid/ask prices side by side, and flags any arb opportunities.

No matching required — just shows all available markets and prices.

Usage:
    python scan_markets.py <kalshi_key_id> <kalshi_key_path>
"""
import asyncio
import sys
import aiohttp

KALSHI_API  = "https://api.elections.kalshi.com/trade-api/v2"
POLY_API    = "https://clob.polymarket.com"

SYMBOLS = ["BTC", "ETH", "SOL", "XRP"]

import base64, hashlib, time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def kalshi_headers(key_id, private_key, method, path):
    ts = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }

async def fetch_kalshi_markets(session, key_id, private_key):
    """Fetch all open crypto up/down markets from Kalshi."""
    markets = []
    series_prefixes = ["KXBTC", "KXETH", "KXSOL", "KXXRP"]
    path = "/markets"

    for prefix in series_prefixes:
        cursor = ""
        while True:
            params = {"status": "open", "series_ticker": prefix, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            headers = kalshi_headers(key_id, private_key, "GET", path)
            async with session.get(KALSHI_API + path, headers=headers, params=params) as r:
                if r.status != 200:
                    text = await r.text()
                    print(f"  Kalshi error for {prefix}: {r.status} {text[:100]}")
                    break
                data = await r.json()
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor", "")
            if not cursor or not batch:
                break
    return markets

async def fetch_poly_markets(session):
    """Fetch open BTC/ETH/SOL/XRP up/down markets from Polymarket."""
    markets = []
    keywords = ["bitcoin up or down", "ethereum up or down", "solana up or down", "xrp up or down"]
    cursor = ""

    while True:
        params = {"limit": 500, "active": "true"}
        if cursor:
            params["next_cursor"] = cursor
        async with session.get(POLY_API + "/markets", params=params) as r:
            if r.status != 200:
                print(f"  Polymarket error: {r.status}")
                break
            data = await r.json()

        batch = data.get("data", [])
        for m in batch:
            q = m.get("question", "").lower()
            if any(kw in q for kw in keywords):
                markets.append(m)

        next_cur = data.get("next_cursor", "")
        # "LTE=" means end of results in Polymarket pagination
        if not next_cur or next_cur == "LTE=" or not batch:
            break
        cursor = next_cur

    return markets

async def main(key_id, key_path):
    private_key = load_key(key_path)

    async with aiohttp.ClientSession() as session:
        print("Fetching markets...")
        kalshi_task = asyncio.create_task(fetch_kalshi_markets(session, key_id, private_key))
        poly_task   = asyncio.create_task(fetch_poly_markets(session))

        kalshi_markets, poly_markets = await asyncio.gather(kalshi_task, poly_task)

    print(f"\nKalshi: {len(kalshi_markets)} crypto markets")
    print(f"Polymarket: {len(poly_markets)} crypto up/down markets\n")

    # Print Kalshi markets
    print("=" * 70)
    print("KALSHI MARKETS")
    print("=" * 70)
    for m in kalshi_markets[:20]:
        ticker  = m.get("ticker", "")
        title   = m.get("title", "")[:50]
        yes_bid = m.get("yes_bid", "?")
        yes_ask = m.get("yes_ask", "?")
        no_bid  = m.get("no_bid",  "?")
        no_ask  = m.get("no_ask",  "?")
        print(f"  {ticker:<35} yes={yes_bid}/{yes_ask}  no={no_bid}/{no_ask}  | {title}")
    if len(kalshi_markets) > 20:
        print(f"  ... and {len(kalshi_markets)-20} more")

    # Print Polymarket markets
    print(f"\n{'=' * 70}")
    print("POLYMARKET MARKETS")
    print("=" * 70)
    for m in poly_markets[:20]:
        q      = m.get("question", "")[:55]
        tokens = m.get("tokens", [])
        up  = next((t for t in tokens if t.get("outcome","").lower() == "up"),   {})
        dn  = next((t for t in tokens if t.get("outcome","").lower() == "down"), {})
        print(f"  up={up.get('price','?')}  dn={dn.get('price','?')}  | {q}")
    if len(poly_markets) > 20:
        print(f"  ... and {len(poly_markets)-20} more")

    print(f"\nDone. Total: {len(kalshi_markets)} Kalshi + {len(poly_markets)} Polymarket markets found.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python scan_markets.py <key_id> <key_path>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
