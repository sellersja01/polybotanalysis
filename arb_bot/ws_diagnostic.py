"""
30-min diagnostic: during each Coinbase move >=0.03%, capture all Polymarket
WS events for 2s to answer: is the book getting (A) walked by taker trades or
(B) repriced by maker cancel+repost?

For each triggered window, counts:
  - price_change events where size=0 (cancellations)
  - price_change events where size>0 (new / resized orders)
  - trade events from live_activity channel (actual fills)

Writes results to /root/ws_diagnostic.json every 60s (incremental save).
"""
import asyncio, json, time, websockets, aiohttp, os
from datetime import datetime, timezone

INTERVAL = 300
MOVE_THRESH = 0.03
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CB_WS   = "wss://ws-feed.exchange.coinbase.com"
RESULTS = "/root/ws_diagnostic.json"

ASSETS = [
    {"label": "BTC", "slug": "btc", "cb": "BTC-USD"},
    {"label": "ETH", "slug": "eth", "cb": "ETH-USD"},
    {"label": "SOL", "slug": "sol", "cb": "SOL-USD"},
    {"label": "XRP", "slug": "xrp", "cb": "XRP-USD"},
]

state = {}        # label -> per-asset state
windows = []      # list of completed windows
stop_flag = False

async def get_market(slug):
    now = time.time()
    async with aiohttp.ClientSession() as s:
        for off in range(5):
            cs = int(now // INTERVAL) * INTERVAL - (off * INTERVAL)
            sl = f"{slug}-updown-5m-{cs}"
            try:
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={sl}", timeout=10) as r:
                    d = await r.json()
                if d:
                    m = d[0]["markets"][0]
                    t = json.loads(m.get("clobTokenIds", "[]"))
                    o = json.loads(m.get("outcomes", "[]"))
                    if len(t) >= 2:
                        ui = 0 if o[0] == "Up" else 1
                        return t[ui], t[1-ui], cs
            except Exception:
                pass
    return None, None, None

async def refresh_markets():
    while not stop_flag:
        for cfg in ASSETS:
            s = state[cfg["label"]]
            now = time.time()
            cs = int(now // INTERVAL) * INTERVAL
            if cs != s.get("candle_ts"):
                up, dn, cs2 = await get_market(cfg["slug"])
                if up:
                    s["up_token"] = up
                    s["dn_token"] = dn
                    s["candle_ts"] = cs2
                    s["candle_open"] = None
                    s["token_to_side"] = {up: "Up", dn: "Down"}
                    print(f"[{cfg['label']}] candle={cs2} up_token={up[:16]}...", flush=True)
        await asyncio.sleep(5)

async def cb_ws():
    while not stop_flag:
        try:
            async with websockets.connect(CB_WS, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channels": [{"name": "ticker", "product_ids": [a["cb"] for a in ASSETS]}]
                }))
                print("[CB] connected", flush=True)
                async for msg in ws:
                    if stop_flag: break
                    d = json.loads(msg)
                    if d.get("type") != "ticker":
                        continue
                    pid = d.get("product_id")
                    p = float(d.get("price", 0) or 0)
                    if not pid or p <= 0:
                        continue
                    for label, s in state.items():
                        if s["cb"] != pid: continue
                        s["cb_price"] = p
                        now = time.time()
                        if s["candle_open"] is None and s["candle_ts"] and now >= s["candle_ts"]:
                            s["candle_open"] = p
                        elif s["candle_open"]:
                            move = (p - s["candle_open"]) / s["candle_open"] * 100
                            if abs(move) >= MOVE_THRESH and s.get("current_window") is None:
                                s["current_window"] = {
                                    "trigger_ts": now,
                                    "asset": label,
                                    "move": move,
                                    "cb_before": s["candle_open"],
                                    "cb_trigger": p,
                                    "events": [],
                                    "book_before": {
                                        "up_bid": s.get("up_bid", 0), "up_ask": s.get("up_ask", 0),
                                        "dn_bid": s.get("dn_bid", 0), "dn_ask": s.get("dn_ask", 0),
                                    }
                                }
                                print(f"[{label}] TRIGGER move={move:+.3f}% at {datetime.fromtimestamp(now, tz=timezone.utc).strftime('%H:%M:%S')}", flush=True)
                        break
        except Exception as e:
            print(f"[CB] {e}", flush=True)
            await asyncio.sleep(2)

async def poly_ws(label):
    s = state[label]
    # Also maintain local top-of-book so we know book_before
    s.setdefault("up_bid", 0); s.setdefault("up_ask", 0)
    s.setdefault("dn_bid", 0); s.setdefault("dn_ask", 0)
    s.setdefault("up_book", {"bids": {}, "asks": {}})
    s.setdefault("dn_book", {"bids": {}, "asks": {}})
    while not stop_flag:
        if not s.get("up_token"):
            await asyncio.sleep(2); continue
        try:
            async with websockets.connect(POLY_WS, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "type": "market",
                    "assets_ids": [s["up_token"], s["dn_token"]]
                }))
                async for msg in ws:
                    if stop_flag: break
                    now = time.time()
                    data = json.loads(msg)
                    if not isinstance(data, list):
                        data = [data]
                    for item in data:
                        etype = item.get("event_type", "?")
                        asset_id = item.get("asset_id", "")
                        side = s["token_to_side"].get(asset_id)
                        if not side: continue
                        book = s["up_book"] if side == "Up" else s["dn_book"]
                        # Maintain local book
                        if etype == "book":
                            book["bids"] = {b["price"]: float(b["size"]) for b in item.get("bids", [])}
                            book["asks"] = {a["price"]: float(a["size"]) for a in item.get("asks", [])}
                        elif etype == "price_change":
                            for ch in item.get("changes", []):
                                d = book["bids"] if ch["side"] == "BUY" else book["asks"]
                                d[ch["price"]] = float(ch["size"])
                        # Update best bid/ask
                        bids_live = [(float(p), sz) for p, sz in book["bids"].items() if sz > 0]
                        asks_live = [(float(p), sz) for p, sz in book["asks"].items() if sz > 0]
                        if bids_live:
                            bb = max(bids_live, key=lambda x: x[0])[0]
                            if side == "Up": s["up_bid"] = bb
                            else: s["dn_bid"] = bb
                        if asks_live:
                            ba = min(asks_live, key=lambda x: x[0])[0]
                            if side == "Up": s["up_ask"] = ba
                            else: s["dn_ask"] = ba
                        # Record in window if active
                        w = s.get("current_window")
                        if w:
                            if now - w["trigger_ts"] > 2.0:
                                # Snapshot final book state
                                w["book_after"] = {
                                    "up_bid": s["up_bid"], "up_ask": s["up_ask"],
                                    "dn_bid": s["dn_bid"], "dn_ask": s["dn_ask"],
                                }
                                windows.append(w)
                                s["current_window"] = None
                            else:
                                if etype == "price_change":
                                    for ch in item.get("changes", []):
                                        w["events"].append({
                                            "dt": round(now - w["trigger_ts"], 3),
                                            "etype": "price_change",
                                            "side": side,
                                            "ch_side": ch["side"],       # BUY or SELL
                                            "price": ch["price"],
                                            "size": float(ch["size"]),
                                            "is_cancel": float(ch["size"]) == 0.0,
                                        })
                                elif etype == "book":
                                    w["events"].append({
                                        "dt": round(now - w["trigger_ts"], 3),
                                        "etype": "book",
                                        "side": side,
                                    })
        except Exception as e:
            print(f"[{label}] {e}", flush=True)
            await asyncio.sleep(2)

async def writer():
    while not stop_flag:
        await asyncio.sleep(60)
        try:
            with open(RESULTS, "w") as f:
                json.dump({
                    "updated": datetime.now(timezone.utc).isoformat(),
                    "window_count": len(windows),
                    "windows": windows,
                }, f)
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] saved {len(windows)} windows", flush=True)
        except Exception as e:
            print(f"[writer] {e}", flush=True)

async def main():
    global stop_flag
    for cfg in ASSETS:
        state[cfg["label"]] = {**cfg, "candle_open": None, "candle_ts": 0, "cb_price": 0,
                               "up_token": None, "dn_token": None, "token_to_side": {},
                               "current_window": None}
    tasks = [asyncio.create_task(refresh_markets()),
             asyncio.create_task(cb_ws()),
             asyncio.create_task(writer())]
    for cfg in ASSETS:
        tasks.append(asyncio.create_task(poly_ws(cfg["label"])))

    # Stop after 30 minutes
    try:
        await asyncio.sleep(1800)
    finally:
        stop_flag = True
        await asyncio.sleep(3)
        # Final write
        with open(RESULTS, "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "window_count": len(windows),
                "windows": windows,
            }, f)
        print(f"[DONE] {len(windows)} windows captured", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
