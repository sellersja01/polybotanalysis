"""
test_data.py — Live BTC price check: Polymarket vs Kalshi.
Uses Gamma API outcomePrices for Polymarket (the real market price).
Uses REST for Kalshi.
Loops every 10 seconds. Ctrl+C to stop. NO execution.

Usage:
    cd arb_bot
    python test_data.py
"""
import asyncio
import json
import time
import base64

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_KEY_ID   = "d307ccc8-df96-4210-8d42-8d70c75fe71f"
KALSHI_KEY_PATH = r"C:\Users\James\kalshi_key.pem.txt"
KALSHI_API      = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA      = "https://gamma-api.polymarket.com"
POLY_CLOB       = "https://clob.polymarket.com"
INTERVAL        = 900


def load_pk():
    with open(KALSHI_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def sign(pk, method, path):
    ts  = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                  salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return ts, base64.b64encode(sig).decode()


async def get_poly_btc(session: aiohttp.ClientSession) -> dict | None:
    """
    Fetch BTC Polymarket prices.
    Primary: outcomePrices from Gamma API (always available).
    Also: CLOB bid/ask (available when market is liquid).
    """
    candle_ts = (int(time.time()) // INTERVAL) * INTERVAL
    slug = f"btc-updown-15m-{candle_ts}"

    try:
        async with session.get(f"{POLY_GAMMA}/events", params={"slug": slug}) as r:
            if r.status != 200:
                return None
            data = await r.json()
        events = data if isinstance(data, list) else data.get("events", [])
        if not events:
            return None
        market = events[0].get("markets", [None])[0]
        if not market:
            return None

        outcomes      = market.get("outcomes", [])         # ["Up", "Down"] or JSON string
        outcome_prices = market.get("outcomePrices", [])   # JSON string: '["0.45","0.55"]'
        token_ids     = json.loads(market.get("clobTokenIds", "[]"))
        # These fields may themselves be JSON strings
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        if len(outcomes) < 2 or len(outcome_prices) < 2 or len(token_ids) < 2:
            return None

        # Map outcome → price and token_id
        up_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "up"), 0)
        dn_idx = 1 - up_idx
        up_mid = float(outcome_prices[up_idx])
        dn_mid = float(outcome_prices[dn_idx])
        up_tok = token_ids[up_idx]
        dn_tok = token_ids[dn_idx]

        result = {
            "up_mid": up_mid,
            "dn_mid": dn_mid,
            "up_tok": up_tok,
            "dn_tok": dn_tok,
            "slug":   slug,
            "candle_ts": candle_ts,
            # CLOB bid/ask — filled below if liquid
            "up_bid": None, "up_ask": None,
            "dn_bid": None, "dn_ask": None,
        }

        # Try CLOB orderbook for real bid/ask
        try:
            up_book, dn_book = await asyncio.gather(
                _fetch_clob_book(session, up_tok),
                _fetch_clob_book(session, dn_tok),
            )
            if up_book:
                result["up_bid"] = up_book["bid"]
                result["up_ask"] = up_book["ask"]
            if dn_book:
                result["dn_bid"] = dn_book["bid"]
                result["dn_ask"] = dn_book["ask"]
        except Exception:
            pass

        return result

    except Exception as e:
        print(f"  [poly] error: {e}")
        return None


async def _fetch_clob_book(session, token_id: str) -> dict | None:
    try:
        async with session.get(f"{POLY_CLOB}/book", params={"token_id": token_id}) as r:
            if r.status != 200:
                return None
            data = await r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None
        bid = float(bids[0]["price"])
        ask = float(asks[0]["price"])
        # Reject illiquid books (spread > 0.50 = no real market)
        if ask - bid > 0.50:
            return None
        return {"bid": bid, "ask": ask}
    except Exception:
        return None


async def get_kalshi_btc(session: aiohttp.ClientSession, pk) -> dict | None:
    path = "/markets"
    try:
        ts, sig = sign(pk, "GET", path)
        headers = {
            "KALSHI-ACCESS-KEY":       KALSHI_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sig,
        }
        async with session.get(KALSHI_API + path, headers=headers,
                               params={"status": "open",
                                       "series_ticker": "KXBTC15M",
                                       "limit": 1}) as r:
            data = await r.json()
        markets = data.get("markets", [])
        if not markets:
            return None
        m = markets[0]
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        yes_bid = float(m.get("yes_bid_dollars") or 0)
        if yes_ask > 1.0:          # fallback: cents field
            yes_ask /= 100
            yes_bid /= 100
        if yes_ask == 0 and yes_bid == 0:
            return None
        return {
            "ticker": m["ticker"],
            "up_bid": yes_bid,
            "up_ask": yes_ask,
            "dn_bid": round(1 - yes_ask, 4),
            "dn_ask": round(1 - yes_bid, 4),
        }
    except Exception as e:
        print(f"  [kalshi] error: {e}")
        return None


def print_snapshot(pp: dict, kp: dict):
    ts = time.strftime("%H:%M:%S UTC", time.gmtime())

    print(f"\n{'='*62}  {ts}")

    # Polymarket row
    if pp:
        up_mid = pp["up_mid"]
        dn_mid = pp["dn_mid"]
        clob_ok = pp["up_ask"] is not None
        clob_str = (f"  CLOB ask: up={pp['up_ask']:.3f} dn={pp['dn_ask']:.3f}"
                    if clob_ok else "  CLOB: illiquid (near candle end)")
        print(f"  Polymarket BTC  up_mid={up_mid:.3f}  dn_mid={dn_mid:.3f}")
        print(f"  {clob_str}")
    else:
        print("  Polymarket BTC  N/A")

    # Kalshi row
    if kp:
        print(f"  Kalshi BTC      up_bid={kp['up_bid']:.3f}  up_ask={kp['up_ask']:.3f}"
              f"  dn_bid={kp['dn_bid']:.3f}  dn_ask={kp['dn_ask']:.3f}")
        print(f"  Ticker: {kp['ticker']}")
    else:
        print("  Kalshi BTC      N/A")

    # Gap check (using mid prices vs Kalshi ask)
    if pp and kp:
        poly_up_ask = pp["up_ask"] if pp["up_ask"] else pp["up_mid"]
        poly_dn_ask = pp["dn_ask"] if pp["dn_ask"] else pp["dn_mid"]
        gap_up = round((poly_up_ask + kp["dn_ask"]) - 1.0, 4)
        gap_dn = round((poly_dn_ask + kp["up_ask"]) - 1.0, 4)
        best   = min(gap_up, gap_dn)
        src    = "CLOB" if pp["up_ask"] else "mid (indicative)"
        flag   = "  *** ARB ***" if best < -0.01 else ""
        print(f"  Gap [{src}]: PolyUp+KalDn={gap_up:+.4f}  PolyDn+KalUp={gap_dn:+.4f}{flag}")

        # Health check
        poly_price = pp["up_mid"]
        kals_price = kp["up_ask"]
        if 0.05 < poly_price < 0.95 and 0.05 < kals_price < 0.95:
            print(f"  [ok] Both platforms show active prices")
        else:
            secs_left = INTERVAL - (int(time.time()) % INTERVAL)
            print(f"  [info] Prices near boundary — {secs_left}s until next candle")


async def main():
    print("\nLoading Kalshi key...")
    pk = load_pk()
    print("OK\n")

    poly_session   = aiohttp.ClientSession()
    kalshi_session = aiohttp.ClientSession()

    last_candle = (int(time.time()) // INTERVAL) * INTERVAL
    tick = 0

    print("Starting BTC price loop — Ctrl+C to stop")
    print("(Best to run in the middle of a candle for active CLOB prices)\n")

    try:
        while True:
            current_candle = (int(time.time()) // INTERVAL) * INTERVAL
            if current_candle != last_candle:
                print(f"\n[candle rollover — new candle {current_candle}]")
                last_candle = current_candle

            tick += 1
            pp, kp = await asyncio.gather(
                get_poly_btc(poly_session),
                get_kalshi_btc(kalshi_session, pk),
            )
            print_snapshot(pp, kp)
            await asyncio.sleep(10)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        await poly_session.close()
        await kalshi_session.close()


asyncio.run(main())
