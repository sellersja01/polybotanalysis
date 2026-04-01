"""
test_sell.py — Buy $1 of BTC UP token then immediately sell it back.
Confirms the full buy→sell cycle works before running the live bot.
"""
import asyncio
import json
import math
import time
import aiohttp
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from polymarket_client import PolymarketClient

async def get_btc_market():
    async with aiohttp.ClientSession() as s:
        now = time.time()
        for interval, label in [(300, "5m"), (900, "15m")]:
            for offset in range(5):
                candle_ts = int(now // interval) * interval - (offset * interval)
                slug = f"btc-updown-{label}-{candle_ts}"
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}") as r:
                    data = await r.json()
                if data:
                    mkt = data[0].get("markets", [{}])[0]
                    tokens = json.loads(mkt.get("clobTokenIds", "[]"))
                    outcomes = json.loads(mkt.get("outcomes", "[]"))
                    if len(tokens) >= 2:
                        up_idx = 0 if outcomes[0] == "Up" else 1
                        return tokens[up_idx], mkt.get("question", slug)
    return None, None

async def main():
    print("=== BUY → SELL TEST ===\n")

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=5),
        timeout=aiohttp.ClientTimeout(total=10),
    )
    client = PolymarketClient(session=session)

    try:
        print("Fetching BTC market...")
        token_id, question = await get_btc_market()
        if not token_id:
            print("ERROR: no market found")
            return
        print(f"Market: {question}")
        print(f"Token:  {token_id[:16]}...\n")

        # BUY $1 worth
        price = 0.50  # rough estimate — market order will fill at actual ask
        size = math.ceil(1.0 / price)
        print(f"Placing BUY: ~{size} shares (~$1.00)...")
        buy_resp = await client.place_order(token_id, price, size)
        shares_bought = float(buy_resp.get("takingAmount", 0))
        print(f"BUY result:  takingAmount={shares_bought} status={buy_resp.get('status')}")
        print(f"Full response: {buy_resp}\n")

        if shares_bought <= 0:
            print("ERROR: BUY filled 0 shares — cannot test sell")
            return

        print(f"Waiting 3.5s for on-chain settlement before selling {shares_bought} shares...")
        await asyncio.sleep(3.5)

        # SELL shares back
        print(f"Placing SELL: {shares_bought} shares...")
        sell_resp = await client.sell_order(token_id, shares_bought)
        print(f"SELL result: status={sell_resp.get('status')} takingAmount={sell_resp.get('takingAmount')}")
        print(f"Full response: {sell_resp}\n")
        print("=== TEST COMPLETE ===")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
