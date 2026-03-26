"""
live_prices.py
==============
Fetches current bid/ask from Kalshi 15m up/down markets + matching Polymarket markets.
Shows side-by-side prices and flags any arb opportunities.
"""
import asyncio, sys, json, base64, time, re
import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_API   = "https://clob.polymarket.com"

KALSHI_15M_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]

POLY_KEYWORDS = {
    "KXBTC15M": "bitcoin up or down",
    "KXETH15M": "ethereum up or down",
    "KXSOL15M": "solana up or down",
    "KXXRP15M": "xrp up or down",
}

POLY_TAKER_FEE_COEFF = 0.25
KALSHI_MAKER_FEE     = 0.0
KALSHI_TAKER_FEE     = 0.07

def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def kalshi_headers(key_id, pk, method, path):
    ts = int(time.time() * 1000)
    sig = pk.sign(f"{ts}{method}{path}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

def poly_fee(p): return p * POLY_TAKER_FEE_COEFF * (p * (1-p))**2
def kalshi_taker_fee(p): return KALSHI_TAKER_FEE * p * (1-p)

async def fetch_kalshi_15m(session, key_id, pk):
    markets = []
    for series in KALSHI_15M_SERIES:
        path = "/markets"
        h = kalshi_headers(key_id, pk, "GET", path)
        async with session.get(KALSHI_API + path, headers=h,
                               params={"status":"open","series_ticker":series,"limit":10}) as r:
            data = await r.json()
        for m in data.get("markets", []):
            m["_series"] = series
            markets.append(m)
    return markets

async def fetch_clob_prices_ws(token_ids: list) -> dict:
    """Get best bid/ask via Polymarket WebSocket — same method as collector_v2."""
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    books = {tid: {"bids": {}, "asks": {}} for tid in token_ids}
    received = set()

    async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
        await ws.send(json.dumps({
            "auth": {}, "type": "subscribe",
            "assets_ids": token_ids, "markets": []
        }))
        # Wait until we receive the initial book snapshot for all tokens
        deadline = time.time() + 8  # 8s timeout
        while time.time() < deadline and len(received) < len(token_ids):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            events = json.loads(msg)
            if not isinstance(events, list):
                events = [events]
            for event in events:
                etype    = event.get("event_type", "")
                asset_id = event.get("asset_id", "")
                if asset_id not in books:
                    continue
                book = books[asset_id]
                if etype == "book":
                    book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids", [])}
                    book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks", [])}
                    received.add(asset_id)
                elif etype == "price_change":
                    for ch in event.get("changes", []):
                        p, sz = ch["price"], float(ch["size"])
                        d = book["bids"] if ch["side"] == "BUY" else book["asks"]
                        if sz == 0: d.pop(p, None)
                        else:       d[p] = sz

    prices = {}
    for tid, book in books.items():
        bids = [float(p) for p, s in book["bids"].items() if s > 0]
        asks = [float(p) for p, s in book["asks"].items() if s > 0]
        prices[tid] = {
            "bid": max(bids) if bids else 0.0,
            "ask": min(asks) if asks else 1.0,
        }
    return prices

async def fetch_poly_15m(session):
    """Fetch live Polymarket 15m up/down markets using slug-based lookup + CLOB prices."""
    markets = []
    assets = ["btc", "eth", "sol", "xrp"]
    interval = 900  # 15 minutes
    ts = (int(time.time()) // interval) * interval
    gamma_url = "https://gamma-api.polymarket.com/events"

    for asset in assets:
        slug = f"{asset}-updown-15m-{ts}"
        async with session.get(gamma_url, params={"slug": slug}) as r:
            if r.status != 200:
                continue
            data = await r.json()
        if not data:
            continue
        event = data[0]
        for m in event.get("markets", []):
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            if len(token_ids) < 2:
                continue
            # Fetch live prices from CLOB WebSocket
            prices = await fetch_clob_prices_ws(token_ids)
            up_tid, dn_tid = token_ids[0], token_ids[1]
            m["_asset"]   = asset
            m["_up_bid"]  = prices.get(up_tid, {}).get("bid", 0)
            m["_up_ask"]  = prices.get(up_tid, {}).get("ask", 1)
            m["_dn_bid"]  = prices.get(dn_tid, {}).get("bid", 0)
            m["_dn_ask"]  = prices.get(dn_tid, {}).get("ask", 1)
            print(f"  {asset.upper()} 15m: up={m['_up_bid']:.3f}/{m['_up_ask']:.3f}  dn={m['_dn_bid']:.3f}/{m['_dn_ask']:.3f}  | {m.get('question','')[:50]}")
            markets.append(m)
    return markets

async def main(key_id, key_path):
    pk = load_key(key_path)

    async with aiohttp.ClientSession() as session:
        k_task = asyncio.create_task(fetch_kalshi_15m(session, key_id, pk))
        p_task = asyncio.create_task(fetch_poly_15m(session))
        kalshi_markets, poly_markets = await asyncio.gather(k_task, p_task)

    print(f"Kalshi 15m markets: {len(kalshi_markets)}")
    print(f"Polymarket 15m markets: {len(poly_markets)}\n")

    # Print raw Kalshi market to understand price fields
    if kalshi_markets:
        m = kalshi_markets[0]
        print("=== RAW KALSHI MARKET FIELDS ===")
        for k, v in m.items():
            if v is not None and v != "" and v != 0:
                print(f"  {k}: {v}")
        print()

    # Print Kalshi prices
    print("=" * 70)
    print("KALSHI 15M MARKETS (live)")
    print("=" * 70)
    for m in kalshi_markets:
        ticker  = m.get("ticker","")
        title   = m.get("title","")
        yes_bid = m.get("yes_bid_dollars")
        yes_ask = m.get("yes_ask_dollars")
        no_bid  = m.get("no_bid_dollars")
        no_ask  = m.get("no_ask_dollars")
        def fmt(x): return f"{float(x):.3f}" if x is not None else "?"
        print(f"  {ticker:<40} yes={fmt(yes_bid)}/{fmt(yes_ask)}  no={fmt(no_bid)}/{fmt(no_ask)}  | {title[:35]}")

    # Print Polymarket prices
    print(f"\n{'=' * 70}")
    print("POLYMARKET 15M MARKETS (live)")
    print("=" * 70)
    for m in poly_markets[:30]:
        q = m.get("question","")[:60]
        print(f"  up={m['_up_bid']:.3f}/{m['_up_ask']:.3f}  dn={m['_dn_bid']:.3f}/{m['_dn_ask']:.3f}  | {q}")

    # Check for arb opportunities
    print(f"\n{'=' * 70}")
    print("ARB CHECK (Poly taker + Kalshi maker, 0% Kalshi fee)")
    print("=" * 70)
    opps = 0
    for km in kalshi_markets:
        yes_ask = km.get("yes_ask_dollars")
        no_ask  = km.get("no_ask_dollars")
        if yes_ask is None or no_ask is None:
            continue
        yes_ask = float(yes_ask)
        no_ask  = float(no_ask)

        series_to_asset = {"KXBTC15M":"btc","KXETH15M":"eth","KXSOL15M":"sol","KXXRP15M":"xrp"}
        asset = series_to_asset.get(km.get("_series",""))
        if not asset:
            continue

        # Find matching Poly market by asset
        for pm in poly_markets:
            if pm.get("_asset") != asset:
                continue
            tokens = pm.get("tokens",[])
            up_tok = next((t for t in tokens if t.get("outcome","").lower()=="up"),  {})
            dn_tok = next((t for t in tokens if t.get("outcome","").lower()=="down"),{})
            p_up = pm.get("_up_ask", 0)
            p_dn = pm.get("_dn_ask", 0)
            if not p_up or not p_dn:
                continue

            # Arb direction 1: Buy Up on Poly + No on Kalshi (maker)
            cost1  = p_up + no_ask
            fee1   = poly_fee(p_up)
            profit1 = 1.0 - cost1 - fee1

            # Arb direction 2: Buy Down on Poly + Yes on Kalshi (maker)
            cost2  = p_dn + yes_ask
            fee2   = poly_fee(p_dn)
            profit2 = 1.0 - cost2 - fee2

            best = max(profit1, profit2)
            if best > 0.005:  # >0.5 cents profit
                opps += 1
                dir_label = "PolyUp+KalshiNo" if profit1 > profit2 else "PolyDown+KalshiYes"
                print(f"  ARB! {km['ticker']:<35} {dir_label}  profit={best*100:.2f}¢  ROI={best/min(cost1,cost2)*100:.1f}%")
                print(f"       Kalshi yes_ask={yes_ask:.3f}  no_ask={no_ask:.3f}  |  Poly up={p_up:.3f} dn={p_dn:.3f}")
            break  # only check first matching poly market

    if opps == 0:
        print("  No arb opportunities found at current prices.")
    print(f"\nDone. Checked {len(kalshi_markets)} Kalshi × {len(poly_markets)} Poly markets.")

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
