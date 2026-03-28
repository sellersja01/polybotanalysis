"""
test_order.py — Places ONE real test order on each platform to verify execution works.
Picks the current cheapest arb direction (or just tests whichever side is available).
Uses 5 shares (Polymarket minimum for limit orders).

Usage:
    $env:POLY_PRIVATE_KEY = "0x..."
    $env:KALSHI_KEY_PATH = "C:\\Users\\James\\kalshi_key.pem.txt"
    python test_order.py
"""
import asyncio
import json
import time
import base64
import os

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_KEY_ID   = "d307ccc8-df96-4210-8d42-8d70c75fe71f"
KALSHI_KEY_PATH = os.environ.get("KALSHI_KEY_PATH", r"C:\Users\James\kalshi_key.pem.txt")
KALSHI_API      = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA      = "https://gamma-api.polymarket.com"
POLY_CLOB_URL   = "https://clob.polymarket.com"
POLY_ADDRESS    = os.environ.get("POLY_ADDRESS", "0x6826c3197fff281144b07fe6c3e72636854769ab")
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "")
INTERVAL        = 900
SHARES          = 1  # test with 1 share


# ── Kalshi auth ───────────────────────────────────────────────────────────────
def load_pk():
    with open(KALSHI_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

KALSHI_PATH_PREFIX = "/trade-api/v2"

def sign(pk, method, path):
    ts  = int(time.time() * 1000)
    full_path = KALSHI_PATH_PREFIX + path
    msg = f"{ts}{method}{full_path}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                  salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return ts, base64.b64encode(sig).decode()


# ── Fetch current BTC prices ──────────────────────────────────────────────────
async def get_current_prices(session, pk):
    candle_ts = (int(time.time()) // INTERVAL) * INTERVAL

    # Polymarket
    slug = f"btc-updown-15m-{candle_ts}"
    async with session.get(f"{POLY_GAMMA}/events", params={"slug": slug}) as r:
        data = await r.json()
    events = data if isinstance(data, list) else []
    market = events[0]["markets"][0] if events else None

    poly = None
    if market:
        outcomes       = json.loads(market.get("outcomes", "[]"))
        outcome_prices = json.loads(market.get("outcomePrices", "[]"))
        token_ids      = json.loads(market.get("clobTokenIds", "[]"))
        up_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "up"), 0)
        poly = {
            "up_mid":   float(outcome_prices[up_idx]),
            "dn_mid":   float(outcome_prices[1 - up_idx]),
            "up_token": token_ids[up_idx],
            "dn_token": token_ids[1 - up_idx],
            "cond":     market.get("conditionId"),
        }

    # Kalshi
    ts, sig = sign(pk, "GET", "/markets")
    headers = {
        "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    async with session.get(KALSHI_API + "/markets", headers=headers,
                           params={"status": "open", "series_ticker": "KXBTC15M", "limit": 1}) as r:
        data = await r.json()
    m = data.get("markets", [None])[0]
    kalshi = None
    if m:
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        yes_bid = float(m.get("yes_bid_dollars") or 0)
        kalshi = {
            "ticker": m["ticker"],
            "up_ask": yes_ask,
            "dn_ask": round(1 - yes_bid, 4),
        }

    return poly, kalshi


# ── Place Kalshi market order (~$1) ──────────────────────────────────────────
async def place_kalshi_order(session, pk, ticker, side, price_dollars):
    """Market order: IOC at max price to guarantee fill. Buys ~$1 worth of shares."""
    path  = "/portfolio/orders"
    ts, sig = sign(pk, "POST", path)
    headers = {
        "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type":            "application/json",
    }
    # Buy enough shares to spend ~$1; market order = IOC at 99c (always fills)
    shares = max(1, int(round(1.0 / price_dollars)))
    body = {
        "ticker":        ticker,
        "side":          side,
        "action":        "buy",
        "count":         shares,
        "yes_price":     99 if side == "yes" else 1,   # max price = market order
    }
    print(f"  Kalshi order: {side.upper()} {shares} shares @ market (~${price_dollars*shares:.2f})")
    async with session.post(KALSHI_API + path, headers=headers, json=body) as r:
        return r.status, await r.json()


# ── Place Polymarket market order ($1 USDC) ───────────────────────────────────
def check_poly_wallet():
    """Print the wallet address derived from POLY_PRIVATE_KEY so we can verify it matches."""
    try:
        from py_clob_client.client import ClobClient
        clob = ClobClient(host=POLY_CLOB_URL, key=POLY_PRIVATE_KEY, chain_id=137)
        print(f"\n  Derived wallet address: {clob.get_address()}")
        print(f"  Expected:               {POLY_ADDRESS}")
    except Exception as e:
        print(f"\n  Could not derive address: {e}")

async def place_poly_order(token_id, price):
    """Limit IOC order at current AMM price — uses Polymarket internal balance."""
    if not POLY_PRIVATE_KEY:
        return None, {"error": "POLY_PRIVATE_KEY not set"}
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs

        # key = MetaMask EOA private key (signer)
        # funder = Polymarket proxy address (where funds sit)
        clob = ClobClient(host=POLY_CLOB_URL, key=POLY_PRIVATE_KEY, chain_id=137,
                          signature_type=2, funder="0x6826c3197fff281144b07fe6c3e72636854769ab")
        creds = clob.create_or_derive_api_creds()
        clob.set_api_creds(creds)

        size = 5  # Polymarket minimum for limit orders
        args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=size,
            side="BUY",
        )
        signed = clob.create_order(args)
        print(f"  Polymarket order: BUY {size} shares @ {price:.4f} (IOC, ~${size*price:.2f})")
        resp = clob.post_order(signed, "IOC")
        return 200, resp
    except Exception as e:
        return None, {"error": str(e)}


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("\n" + "="*55)
    print("  ORDER PLACEMENT TEST — BTC 15m")
    print("="*55)

    if not POLY_PRIVATE_KEY:
        print("\nERROR: Set POLY_PRIVATE_KEY env var first")
        return

    pk = load_pk()
    check_poly_wallet()
    async with aiohttp.ClientSession() as session:
        print("\nFetching current prices...")
        poly, kalshi = await get_current_prices(session, pk)

        if not poly or not kalshi:
            print("ERROR: Could not fetch prices")
            return

        print(f"\n  Polymarket BTC: up={poly['up_mid']:.3f}  dn={poly['dn_mid']:.3f}")
        print(f"  Kalshi BTC:     up={kalshi['up_ask']:.3f}  dn={kalshi['dn_ask']:.3f}")

        # Reject near-settled markets
        prices = [poly['up_mid'], poly['dn_mid'], kalshi['up_ask'], kalshi['dn_ask']]
        if any(p < 0.05 or p > 0.95 for p in prices):
            print("\nERROR: Market is nearly settled (price outside 0.05-0.95). Wait for next candle.")
            return

        # Pick best direction
        gap_up = (poly["up_mid"] + kalshi["dn_ask"]) - 1.0
        gap_dn = (poly["dn_mid"] + kalshi["up_ask"]) - 1.0
        print(f"\n  Gap PolyUp+KalDn: {gap_up:+.4f}")
        print(f"  Gap PolyDn+KalUp: {gap_dn:+.4f}")

        if gap_up <= gap_dn:
            poly_side   = "up"
            kalshi_side = "no"   # kalshi DOWN = no
            poly_token  = poly["up_token"]
            poly_price  = poly["up_mid"]
            kalshi_price = kalshi["dn_ask"]
            print(f"\n  Best direction: BUY Poly UP ({poly_price:.3f}) + Kalshi DOWN ({kalshi_price:.3f})")
        else:
            poly_side   = "dn"
            kalshi_side = "yes"  # kalshi UP = yes
            poly_token  = poly["dn_token"]
            poly_price  = poly["dn_mid"]
            kalshi_price = kalshi["up_ask"]
            print(f"\n  Best direction: BUY Poly DOWN ({poly_price:.3f}) + Kalshi UP ({kalshi_price:.3f})")

        k_shares_approx = max(1, int(round(1.0 / kalshi_price)))
        print(f"  ~$1 on each platform  |  {'ARBING' if (poly_price + kalshi_price) < 1.0 else 'WARNING: NOT AN ARB'}")
        print(f"  Kalshi: ~{k_shares_approx} share(s) @ {kalshi_price:.3f} = ${kalshi_price*k_shares_approx:.2f}")
        print(f"  Polymarket: 5 shares @ {poly_price:.3f} = ~${5*poly_price:.2f}")

        confirm = input("\nPlace these orders for REAL? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Cancelled.")
            return

        print("\nFiring orders...")
        k_status, k_result = await place_kalshi_order(
            session, pk, kalshi["ticker"], kalshi_side, kalshi_price
        )
        p_status, p_result = await place_poly_order(poly_token, poly_price)

        print(f"\n  Kalshi [{k_status}]: {json.dumps(k_result, indent=2)[:300]}")
        print(f"\n  Polymarket [{p_status}]: {json.dumps(p_result, indent=2)[:300]}")
        print("\n" + "="*55)


asyncio.run(main())
