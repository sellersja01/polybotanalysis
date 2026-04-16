"""
test_buy_sell.py — Buy $1 of Up, wait 5s, sell it. Connectivity test.

Usage on Hetzner:
    POLY_PRIVATE_KEY="0x..." python3 -u test_buy_sell.py
"""
import asyncio
import json
import os
import time
import aiohttp


POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "")


def build_clob():
    from py_clob_client.client import ClobClient
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=POLY_PRIVATE_KEY,
        chain_id=137,
        signature_type=2,
        funder="0x6826c3197fff281144b07fe6c3e72636854769ab",
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print(f"[CLOB] Ready: {creds.api_key[:8]}...")
    return client


async def get_up_token():
    now = time.time()
    async with aiohttp.ClientSession() as s:
        for offset in range(3):
            candle_ts = int(now // 300) * 300 - (offset * 300)
            slug = f"btc-updown-5m-{candle_ts}"
            async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10) as r:
                data = await r.json()
            if data:
                mkt = data[0].get("markets", [{}])[0]
                tokens = json.loads(mkt.get("clobTokenIds", "[]"))
                outcomes = json.loads(mkt.get("outcomes", "[]"))
                if len(tokens) >= 2:
                    up_idx = 0 if outcomes[0] == "Up" else 1
                    question = mkt.get("question") or slug
                    return tokens[up_idx], question
    return None, None


async def main():
    print("=" * 50)
    print("  BUY/SELL TEST — $1 Up, wait 5s, sell")
    print("=" * 50)

    if not POLY_PRIVATE_KEY:
        print("ERROR: Set POLY_PRIVATE_KEY")
        return

    clob = build_clob()
    from py_clob_client.clob_types import MarketOrderArgs

    up_token, question = await get_up_token()
    if not up_token:
        print("ERROR: No market found")
        return

    print(f"Market: {question}")
    print(f"Token: {up_token[:20]}...")
    print()

    # BUY
    print("BUYING $1 of Up...")
    t0 = time.perf_counter_ns()
    args = MarketOrderArgs(token_id=up_token, amount=1.0, side="BUY", price=0.99)
    signed = clob.create_market_order(args)
    resp = clob.post_order(signed, "FAK")
    ms = (time.perf_counter_ns() - t0) / 1e6

    shares = float(resp.get("takingAmount", 0))
    spent = float(resp.get("makingAmount", 0))
    status = resp.get("status")
    print(f"  {ms:.0f}ms | {shares:.4f} shares | spent=${spent:.4f} | status={status}")

    if shares <= 0:
        print("ERROR: 0 shares, can't sell")
        print(f"  Full response: {resp}")
        return

    # WAIT
    print(f"\nWaiting 5 seconds...\n")
    await asyncio.sleep(5)

    # SELL
    safe = round(shares * 0.98, 6)
    print(f"SELLING {safe:.4f} shares...")
    t0 = time.perf_counter_ns()
    args = MarketOrderArgs(token_id=up_token, amount=safe, side="SELL", price=0.01)
    signed = clob.create_market_order(args)
    resp = clob.post_order(signed, "FAK")
    ms = (time.perf_counter_ns() - t0) / 1e6

    received = float(resp.get("takingAmount", 0))
    status = resp.get("status")
    print(f"  {ms:.0f}ms | received=${received:.4f} | status={status}")

    pnl = received - spent
    print(f"\nPnL: ${pnl:+.4f} (spent=${spent:.4f}, received=${received:.4f})")
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
