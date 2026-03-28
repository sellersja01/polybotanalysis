"""
check_prices.py — Sanity check: print live prices from both platforms side by side.
Run this BEFORE the bot to confirm data is accurate.

Usage:
    cd arb_bot
    python check_prices.py
"""
import asyncio
import json
import time
import base64

import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_KEY_ID   = "d307ccc8-df96-4210-8d42-8d70c75fe71f"
KALSHI_KEY_PATH = r"C:\Users\James\kalshi_key.pem.txt"
KALSHI_API      = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS       = "wss://api.elections.kalshi.com/trade-api/ws/v2"
POLY_WS         = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_GAMMA      = "https://gamma-api.polymarket.com"
KALSHI_SERIES   = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M"}
ASSETS          = ["BTC", "ETH", "SOL", "XRP"]
INTERVAL        = 900


def sign(pk, method, path):
    ts  = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                  salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return ts, base64.b64encode(sig).decode()


async def fetch_kalshi_prices(pk) -> dict:
    """Fetch current Kalshi ticker prices via REST."""
    prices = {}
    path   = "/markets"
    async with aiohttp.ClientSession() as s:
        for asset in ASSETS:
            ts, sig = sign(pk, "GET", path)
            headers = {
                "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
                "KALSHI-ACCESS-TIMESTAMP": str(ts),
                "KALSHI-ACCESS-SIGNATURE": sig,
            }
            async with s.get(KALSHI_API + path, headers=headers,
                             params={"status":"open","series_ticker":KALSHI_SERIES[asset],"limit":1}) as r:
                data = await r.json()
            markets = data.get("markets", [])
            if markets:
                m = markets[0]
                yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask", 0) or 0)
                yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid", 0) or 0)
                if yes_ask > 1.0:
                    yes_ask /= 100
                    yes_bid /= 100
                prices[asset] = {
                    "ticker":  m["ticker"],
                    "up_bid":  yes_bid,
                    "up_ask":  yes_ask,
                    "dn_bid":  round(1 - yes_ask, 4),
                    "dn_ask":  round(1 - yes_bid, 4),
                }
    return prices


async def fetch_poly_prices() -> dict:
    """Fetch current Polymarket prices via WebSocket."""
    prices  = {}
    candle_ts = (int(time.time()) // INTERVAL) * INTERVAL

    async with aiohttp.ClientSession() as s:
        # Get token IDs for each asset
        token_map = {}
        for asset in ASSETS:
            slug = f"{asset.lower()}-updown-15m-{candle_ts}"
            print(f"  [poly] trying slug: {slug}", flush=True)
            async with s.get(f"{POLY_GAMMA}/events", params={"slug": slug}) as r:
                print(f"  [poly] status: {r.status}", flush=True)
                if r.status != 200:
                    continue
                data = await r.json()
            if not data:
                print(f"  [poly] {asset}: empty response", flush=True)
                continue
            if isinstance(data, list):
                events = data
            elif isinstance(data, dict):
                events = data.get("events", data.get("data", []))
            else:
                events = []
            if not events:
                print(f"  [poly] {asset}: no events found", flush=True)
                continue
            markets = events[0].get("markets", [])
            if not markets:
                print(f"  [poly] {asset}: event has no markets", flush=True)
                continue
            m = markets[0]
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            if len(token_ids) < 2:
                print(f"  [poly] {asset}: not enough token IDs: {token_ids}", flush=True)
                continue
            up_tok, dn_tok = token_ids[0], token_ids[1]
            print(f"  [poly] {asset}: up={up_tok[:8]}... dn={dn_tok[:8]}...", flush=True)
            token_map[up_tok] = (asset, "up")
            token_map[dn_tok] = (asset, "dn")
            prices[asset]     = {}

    if not token_map:
        print("  [poly] No markets found via Gamma API")
        return prices

    all_ids = list(token_map.keys())
    buf = {}

    try:
        async with websockets.connect(POLY_WS) as ws:
            await ws.send(json.dumps({
                "auth": {}, "type": "subscribe",
                "assets_ids": all_ids, "markets": []
            }))
            deadline = time.time() + 15  # wait up to 15s for prices
            while time.time() < deadline:
                try:
                    raw  = await asyncio.wait_for(ws.recv(), timeout=5)
                    msgs = json.loads(raw)
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for msg in msgs:
                        event_type = msg.get("event_type") or msg.get("type")
                        asset_id = msg.get("asset_id")
                        if asset_id not in token_map:
                            continue
                        asset, side = token_map[asset_id]
                        bids = msg.get("bids", [])
                        asks = msg.get("asks", [])
                        if bids and asks:
                            bid = float(bids[0]["price"])
                            ask = float(asks[0]["price"])
                        else:
                            bid = float(msg.get("best_bid") or msg.get("bid") or 0)
                            ask = float(msg.get("best_ask") or msg.get("ask") or 0)
                        if bid or ask:
                            if asset not in buf:
                                buf[asset] = {}
                            buf[asset][f"{side}_bid"] = bid
                            buf[asset][f"{side}_ask"] = ask
                    # Check if we have all assets
                    if all(
                        asset in buf and "up_bid" in buf[asset] and "dn_bid" in buf[asset]
                        for asset in prices
                    ):
                        break
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        print(f"  [poly] WS error: {e}")

    for asset, data in buf.items():
        if "up_bid" in data and "dn_bid" in data:
            prices[asset] = data

    return prices


async def main():
    print("\n" + "="*60)
    print("  LIVE PRICE CHECK — Polymarket vs Kalshi")
    print("="*60)

    with open(KALSHI_KEY_PATH, "rb") as f:
        pk = serialization.load_pem_private_key(f.read(), password=None)

    print("\nFetching prices...\n")
    kalshi_prices, poly_prices = await asyncio.gather(
        fetch_kalshi_prices(pk),
        fetch_poly_prices(),
    )

    print(f"{'Asset':<6} {'Platform':<12} {'Up Bid':>8} {'Up Ask':>8} {'Dn Bid':>8} {'Dn Ask':>8}")
    print("-"*60)

    for asset in ASSETS:
        pp = poly_prices.get(asset, {})
        kp = kalshi_prices.get(asset, {})

        if pp:
            print(f"{asset:<6} {'Polymarket':<12} "
                  f"{pp.get('up_bid',0):>8.3f} {pp.get('up_ask',0):>8.3f} "
                  f"{pp.get('dn_bid',0):>8.3f} {pp.get('dn_ask',0):>8.3f}")
        else:
            print(f"{asset:<6} {'Polymarket':<12} {'N/A':>8}")

        if kp:
            print(f"{'':<6} {'Kalshi':<12} "
                  f"{kp.get('up_bid',0):>8.3f} {kp.get('up_ask',0):>8.3f} "
                  f"{kp.get('dn_bid',0):>8.3f} {kp.get('dn_ask',0):>8.3f}")
            print(f"{'':<6} {'  ticker':<12} {kp.get('ticker','')}")
        else:
            print(f"{'':<6} {'Kalshi':<12} {'N/A':>8}")

        # Gap check
        if pp and kp:
            gap_up = round((pp.get('up_ask',0) + kp.get('dn_ask',0)) - 1.0, 4)
            gap_dn = round((pp.get('dn_ask',0) + kp.get('up_ask',0)) - 1.0, 4)
            best   = min(gap_up, gap_dn)
            flag   = " *** ARB OPPORTUNITY ***" if best < -0.01 else ""
            print(f"{'':<6} {'  gap':<12} PolyUp+KalDn={gap_up:+.4f}  PolyDn+KalUp={gap_dn:+.4f}{flag}")
        print()

    print("="*60)
    print("If prices look reasonable (0.10-0.90 range), data is good.")
    print("Negative gap = arb opportunity exists right now.\n")


asyncio.run(main())
