"""
arb_collector.py
================
Collects live bid/ask prices from BOTH Polymarket and Kalshi for
BTC/ETH/SOL/XRP 15m up/down markets simultaneously.

Every time either platform updates, writes a snapshot row with the
latest known prices from BOTH platforms side-by-side.

Also records candle outcomes (Up/Down) when markets resolve.

Run on VPS:
    python3 -u arb_collector.py > arb_collector.log 2>&1 &

Output DB: arb_collector.db
"""

import asyncio
import base64
import json
import sqlite3
import time
import random
import os
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils
import aiohttp
import websockets

# ── Config ────────────────────────────────────────────────────────────────────
KALSHI_KEY_ID   = os.environ.get("KALSHI_KEY_ID",   "d307ccc8-df96-4210-8d42-8d70c75fe71f")
KALSHI_KEY_PATH = os.environ.get("KALSHI_KEY_PATH",  "/home/opc/kalshi_key.pem")
ASSETS          = ["btc", "eth", "sol", "xrp"]
INTERVAL        = 900  # 15 minutes
DB_PATH         = "/home/opc/arb_collector.db"

KALSHI_API   = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS    = "wss://api.elections.kalshi.com/trade-api/ws/v2"
POLY_WS      = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API    = "https://gamma-api.polymarket.com/events"

KALSHI_SERIES = {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M", "xrp": "KXXRP15M"}

# ── Database setup ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            ts          REAL,
            asset       TEXT,
            candle_id   TEXT,
            trigger     TEXT,
            p_up_bid    REAL,
            p_up_ask    REAL,
            p_dn_bid    REAL,
            p_dn_ask    REAL,
            k_up_bid    REAL,
            k_up_ask    REAL,
            k_dn_bid    REAL,
            k_dn_ask    REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            candle_id   TEXT,
            asset       TEXT,
            outcome     TEXT,
            resolved_ts REAL,
            PRIMARY KEY (candle_id, asset)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_asset_ts ON snapshots(asset, ts)")
    conn.commit()
    conn.close()

def write_snapshot(asset, candle_id, trigger, prices):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        time.time(), asset, candle_id, trigger,
        prices.get("p_up_bid"), prices.get("p_up_ask"),
        prices.get("p_dn_bid"), prices.get("p_dn_ask"),
        prices.get("k_up_bid"), prices.get("k_up_ask"),
        prices.get("k_dn_bid"), prices.get("k_dn_ask"),
    ))
    conn.commit()
    conn.close()

def write_outcome(candle_id, asset, outcome):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?)
    """, (candle_id, asset, outcome, time.time()))
    conn.commit()
    conn.close()

# ── Kalshi auth ───────────────────────────────────────────────────────────────
def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def kalshi_sign(pk, method, path, ts):
    msg = f"{ts}{method}{path}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def kalshi_headers(pk, key_id, method, path):
    ts = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": kalshi_sign(pk, method, path, ts),
    }

# ── Market lookup ─────────────────────────────────────────────────────────────
def get_candle_id(asset):
    ts = (int(time.time()) // INTERVAL) * INTERVAL
    return f"{asset}_15m_{ts}"

def get_poly_slug(asset):
    ts = (int(time.time()) // INTERVAL) * INTERVAL
    return f"{asset}-updown-15m-{ts}"

async def fetch_poly_tokens(session, asset):
    slug = get_poly_slug(asset)
    async with session.get(GAMMA_API, params={"slug": slug}) as r:
        data = await r.json()
    if not data:
        return None, None, None
    market = data[0]["markets"][0]
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    question  = market.get("question", "")
    market_id = str(market.get("id", ""))
    if len(token_ids) < 2:
        return None, None, None
    return token_ids[0], token_ids[1], question

async def fetch_kalshi_ticker(session, pk, key_id, asset, min_open_ts=None):
    """Fetch current open ticker. If min_open_ts given, retry until open_time >= that ts."""
    series = KALSHI_SERIES[asset]
    path   = "/markets"
    for attempt in range(20):
        h = kalshi_headers(pk, key_id, "GET", path)
        async with session.get(KALSHI_API + path, headers=h,
                               params={"status": "open", "series_ticker": series, "limit": 1}) as r:
            data = await r.json()
        markets = data.get("markets", [])
        if not markets:
            await asyncio.sleep(10)
            continue
        m = markets[0]
        if min_open_ts:
            open_time_str = m.get("open_time", "")
            # open_time is ISO8601 UTC e.g. "2026-03-26T05:00:00Z"
            from datetime import datetime, timezone
            try:
                ot = datetime.fromisoformat(open_time_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                ot = 0
            if ot < min_open_ts:
                print(f"[kalshi] waiting for new {asset} market (got {m['ticker']})...", flush=True)
                await asyncio.sleep(10)
                continue
        return m["ticker"]
    return None

# ── Shared state ──────────────────────────────────────────────────────────────
# state[asset] = {candle_id, p_up_bid, p_up_ask, p_dn_bid, p_dn_ask,
#                 k_up_bid, k_up_ask, k_dn_bid, k_dn_ask,
#                 poly_up_token, poly_dn_token, kalshi_ticker}
state = {a: {} for a in ASSETS}

def snapshot_prices(asset):
    return {k: state[asset].get(k) for k in
            ["p_up_bid","p_up_ask","p_dn_bid","p_dn_ask",
             "k_up_bid","k_up_ask","k_dn_bid","k_dn_ask"]}

# ── Polymarket WebSocket ──────────────────────────────────────────────────────
async def run_poly_feed(session):
    """Single WS connection for all 4 assets."""
    while True:
        try:
            # Refresh token IDs for current candle
            token_to_asset_side = {}
            all_tokens = []
            for asset in ASSETS:
                up, dn, q = await fetch_poly_tokens(session, asset)
                if not up:
                    print(f"[poly] no market for {asset}", flush=True)
                    continue
                state[asset]["poly_up_token"] = up
                state[asset]["poly_dn_token"]  = dn
                state[asset]["candle_id"]      = get_candle_id(asset)
                token_to_asset_side[up] = (asset, "up")
                token_to_asset_side[dn] = (asset, "dn")
                all_tokens += [up, dn]
                print(f"[poly] {asset} 15m: {q}", flush=True)

            books = {tid: {"bids": {}, "asks": {}} for tid in all_tokens}

            async with websockets.connect(POLY_WS, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": all_tokens, "markets": []
                }))
                print("[poly] WS connected", flush=True)

                candle_end = ((int(time.time()) // INTERVAL) + 1) * INTERVAL

                while True:
                    # Check if candle rolled over
                    if time.time() >= candle_end + 5:
                        print("[poly] candle rollover — reconnecting", flush=True)
                        break

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue

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
                        elif etype == "price_change":
                            for ch in event.get("changes", []):
                                p, sz = ch["price"], float(ch["size"])
                                d = book["bids"] if ch["side"] == "BUY" else book["asks"]
                                if sz == 0: d.pop(p, None)
                                else:       d[p] = sz
                        else:
                            continue

                        bids = [float(p) for p, s in book["bids"].items() if s > 0]
                        asks = [float(p) for p, s in book["asks"].items() if s > 0]
                        if not bids or not asks:
                            continue

                        best_bid = max(bids)
                        best_ask = min(asks)
                        asset, side = token_to_asset_side[asset_id]
                        state[asset][f"p_{side}_bid"] = best_bid
                        state[asset][f"p_{side}_ask"] = best_ask

                        cid = state[asset].get("candle_id", get_candle_id(asset))
                        write_snapshot(asset, cid, "polymarket", snapshot_prices(asset))

        except Exception as e:
            print(f"[poly] error: {e} — reconnecting in 5s", flush=True)
            await asyncio.sleep(5)

# ── Kalshi WebSocket ──────────────────────────────────────────────────────────
async def run_kalshi_feed(session, pk, key_id):
    next_candle_ts = None  # after rollover, wait for markets with open_time >= this
    while True:
        try:
            # Fetch current tickers
            tickers = {}
            for asset in ASSETS:
                ticker = await fetch_kalshi_ticker(session, pk, key_id, asset,
                                                   min_open_ts=next_candle_ts)
                if ticker:
                    tickers[ticker] = asset
                    state[asset]["kalshi_ticker"] = ticker
                    print(f"[kalshi] {asset} ticker: {ticker}", flush=True)

            if not tickers:
                await asyncio.sleep(10)
                continue

            # Auth headers for WS
            ws_path = "/trade-api/ws/v2"
            ts  = int(time.time() * 1000)
            sig = kalshi_sign(pk, "GET", ws_path, ts)
            ws_headers = {
                "KALSHI-ACCESS-KEY":       key_id,
                "KALSHI-ACCESS-TIMESTAMP": str(ts),
                "KALSHI-ACCESS-SIGNATURE": sig,
            }

            async with websockets.connect(KALSHI_WS, additional_headers=ws_headers,
                                          ping_interval=20, ping_timeout=10) as ws:
                for i, ticker in enumerate(tickers):
                    await ws.send(json.dumps({
                        "id": i + 1, "cmd": "subscribe",
                        "params": {"channels": ["ticker"], "market_ticker": ticker}
                    }))
                print("[kalshi] WS connected", flush=True)

                candle_end = ((int(time.time()) // INTERVAL) + 1) * INTERVAL
                prev_prices = {t: None for t in tickers}

                while True:
                    if time.time() >= candle_end + 5:
                        print("[kalshi] candle rollover — reconnecting", flush=True)
                        next_candle_ts = candle_end  # require new market open_time >= candle_end
                        break

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue

                    data = json.loads(msg)
                    if data.get("type") != "ticker":
                        continue

                    d      = data.get("msg", {})
                    ticker = d.get("market_ticker")
                    if ticker not in tickers:
                        continue

                    asset    = tickers[ticker]
                    yes_bid  = d.get("yes_bid_dollars")
                    yes_ask  = d.get("yes_ask_dollars")

                    if None in (yes_bid, yes_ask):
                        continue

                    yes_bid = float(yes_bid)
                    yes_ask = float(yes_ask)
                    # no prices not in WS message — derive from yes
                    no_bid  = round(1.0 - yes_ask, 4)
                    no_ask  = round(1.0 - yes_bid, 4)

                    # Detect outcome from near-resolved prices
                    if yes_bid >= 0.99:
                        write_outcome(get_candle_id(asset), asset, "Up")
                    elif no_bid >= 0.99:
                        write_outcome(get_candle_id(asset), asset, "Down")

                    state[asset]["k_up_bid"] = yes_bid
                    state[asset]["k_up_ask"] = yes_ask
                    state[asset]["k_dn_bid"] = no_bid
                    state[asset]["k_dn_ask"] = no_ask

                    cid = state[asset].get("candle_id", get_candle_id(asset))
                    write_snapshot(asset, cid, "kalshi", snapshot_prices(asset))

        except Exception as e:
            print(f"[kalshi] error: {e} — reconnecting in 5s", flush=True)
            await asyncio.sleep(5)

# ── Status printer ────────────────────────────────────────────────────────────
async def print_status():
    while True:
        await asyncio.sleep(60)
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        outcomes = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        conn.close()
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] snapshots={n}  outcomes={outcomes}", flush=True)
        for asset in ASSETS:
            s = state[asset]
            p_up = s.get("p_up_ask","?"); k_up = s.get("k_up_ask","?")
            p_dn = s.get("p_dn_ask","?"); k_dn = s.get("k_dn_ask","?")
            if p_up != "?" and k_up != "?":
                gap = round((float(p_up) + float(k_dn) - 1.0) * 100, 2)
                print(f"  {asset.upper()} | poly_up={p_up:.3f} kalshi_up={k_up:.3f} | gap={gap:+.2f}¢", flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("Starting arb_collector.py", flush=True)
    print(f"Kalshi key: {KALSHI_KEY_ID}", flush=True)

    if not os.path.exists(KALSHI_KEY_PATH):
        print(f"ERROR: Kalshi key not found at {KALSHI_KEY_PATH}", flush=True)
        return

    init_db()
    pk = load_key(KALSHI_KEY_PATH)
    print("DB initialized, key loaded", flush=True)

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            run_poly_feed(session),
            run_kalshi_feed(session, pk, KALSHI_KEY_ID),
            print_status(),
        )

if __name__ == "__main__":
    asyncio.run(main())
