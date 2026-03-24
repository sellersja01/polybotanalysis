import asyncio
import websockets
import requests
import sqlite3
import json
import time
from datetime import datetime, timezone

ASSETS = {
    "btc": {"coinbase_id": "BTC-USD"},
    "eth": {"coinbase_id": "ETH-USD"},
    "sol": {"coinbase_id": "SOL-USD"},
    "xrp": {"coinbase_id": "XRP-USD"},
}

TIMEFRAMES = {
    "5m":  {"interval": 300},
    "15m": {"interval": 900},
}

def get_db_path(asset, tf):
    return f"market_{asset}_{tf}.db"

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS asset_price (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            unix_time REAL,
            price REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS polymarket_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            unix_time REAL,
            market_id TEXT,
            question TEXT,
            outcome TEXT,
            bid REAL,
            ask REAL,
            mid REAL,
            spread REAL
        )
    """)
    conn.commit()
    conn.close()

def get_slug(asset, tf_key):
    interval = TIMEFRAMES[tf_key]["interval"]
    now = int(time.time())
    ts = (now // interval) * interval
    return f"{asset}-updown-{tf_key}-{ts}"

def fetch_tokens(slug):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10)
        data = r.json()
        if data:
            market = data[0]["markets"][0]
            tokens = json.loads(market.get("clobTokenIds", "[]"))
            if len(tokens) >= 2:
                return tokens[0], tokens[1], market.get("question", ""), str(market.get("id", ""))
    except:
        pass
    return None, None, "", ""

def write_price(asset, price, ts, unix):
    for tf in TIMEFRAMES:
        db = get_db_path(asset, tf)
        try:
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO asset_price (timestamp, unix_time, price) VALUES (?, ?, ?)", (ts, unix, price))
            conn.commit()
            conn.close()
        except:
            pass

def write_odds(db, ts, unix, market_id, question, outcome, bid, ask):
    mid = round((bid + ask) / 2, 4)
    spread = round(ask - bid, 4)
    try:
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO polymarket_odds (timestamp, unix_time, market_id, question, outcome, bid, ask, mid, spread) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, unix, market_id, question, outcome, bid, ask, mid, spread)
        )
        conn.commit()
        conn.close()
    except:
        pass

async def stream_price(asset):
    coinbase_id = ASSETS[asset]["coinbase_id"]
    label = f"[{asset.upper()}]"
    while True:
        try:
            async with websockets.connect("wss://advanced-trade-ws.coinbase.com") as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": [coinbase_id],
                    "channel": "ticker"
                }))
                print(f"{label} Price stream connected.")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("channel") == "ticker":
                        for event in data.get("events", []):
                            for ticker in event.get("tickers", []):
                                price = float(ticker.get("price", 0))
                                if price > 0:
                                    ts = datetime.now(timezone.utc).isoformat()
                                    unix = time.time()
                                    write_price(asset, price, ts, unix)
                                    print(f"{label} ${price:,.4f}")
        except Exception as e:
            print(f"{label} Price error: {e} - reconnecting in 5s...")
            await asyncio.sleep(5)

async def stream_clob(asset, tf_key, token_up, token_down, market_id, question, stop_event):
    db = get_db_path(asset, tf_key)
    label = f"[{asset.upper()} {tf_key}]"
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    books = {
        token_up:   {"bids": {}, "asks": {}},
        token_down: {"bids": {}, "asks": {}},
    }

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                sub_msg = json.dumps({
                    "auth": {},
                    "type": "subscribe",
                    "assets_ids": [token_up, token_down],
                    "markets": []
                })
                await ws.send(sub_msg)
                print(f"{label} CLOB WS connected: {question}")

                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        events = json.loads(msg)

                        if not isinstance(events, list):
                            events = [events]

                        for event in events:
                            event_type = event.get("event_type", "")
                            asset_id = event.get("asset_id", "")

                            if asset_id not in books:
                                continue

                            book = books[asset_id]

                            if event_type == "book":
                                book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids", [])}
                                book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks", [])}

                            elif event_type == "price_change":
                                for change in event.get("changes", []):
                                    side = change.get("side", "")
                                    price = change.get("price", "")
                                    size = float(change.get("size", 0))
                                    if side == "BUY":
                                        if size == 0:
                                            book["bids"].pop(price, None)
                                        else:
                                            book["bids"][price] = size
                                    elif side == "SELL":
                                        if size == 0:
                                            book["asks"].pop(price, None)
                                        else:
                                            book["asks"][price] = size

                            bids = [float(p) for p, s in book["bids"].items() if s > 0]
                            asks = [float(p) for p, s in book["asks"].items() if s > 0]

                            if bids and asks:
                                best_bid = max(bids)
                                best_ask = min(asks)
                                outcome = "Up" if asset_id == token_up else "Down"
                                ts = datetime.now(timezone.utc).isoformat()
                                unix = time.time()
                                write_odds(db, ts, unix, market_id, question, outcome, best_bid, best_ask)
                                print(f"{label} {outcome} | Bid:{best_bid:.3f} Ask:{best_ask:.3f} Spread:{round(best_ask-best_bid,3):.3f}")

                    except asyncio.TimeoutError:
                        await ws.ping()

        except Exception as e:
            if not stop_event.is_set():
                print(f"{label} WS error: {e} - reconnecting in 5s...")
                await asyncio.sleep(5)

async def manage_market(asset, tf_key):
    label = f"[{asset.upper()} {tf_key}]"
    last_slug = None
    ws_task = None
    stop_event = asyncio.Event()

    while True:
        try:
            slug = get_slug(asset, tf_key)
            if slug != last_slug:
                print(f"{label} New candle: {slug}")
                token_up, token_down, question, market_id = fetch_tokens(slug)

                if token_up and token_down:
                    if ws_task and not ws_task.done():
                        stop_event.set()
                        ws_task.cancel()
                        try:
                            await ws_task
                        except (asyncio.CancelledError, Exception):
                            pass

                    stop_event = asyncio.Event()
                    ws_task = asyncio.create_task(
                        stream_clob(asset, tf_key, token_up, token_down, market_id, question, stop_event)
                    )
                    last_slug = slug
                else:
                    print(f"{label} No market found for {slug}")

        except Exception as e:
            print(f"{label} Manager error: {e}")

        await asyncio.sleep(1)

async def main():
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            init_db(get_db_path(asset, tf))

    print(f"\nCollector v2 -- {len(ASSETS)} assets x {len(TIMEFRAMES)} timeframes (5m+15m only)\n")

    tasks = []
    for asset in ASSETS:
        tasks.append(stream_price(asset))
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            tasks.append(manage_market(asset, tf))

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
