"""
fast_momentum.py — Speed-optimized Candle Momentum Bot
=======================================================
Designed for low-latency VPS (Hetzner EU). Every millisecond matters.

Architecture:
  1. Coinbase WS + Polymarket WS on persistent connections
  2. Signal detection runs INSIDE the WS callback — no polling delay
  3. Pre-signed order templates ready for Up and Down
  4. FAK market orders — instant fill, no limit order waiting
  5. No REST calls in the hot path

Usage:
    # Paper mode (default)
    python arb_bot/fast_momentum.py

    # Live mode
    $env:DRY_RUN="false"
    $env:POLY_PRIVATE_KEY="0x..."
    $env:TRADE_USD="5.0"
    python arb_bot/fast_momentum.py
"""
import asyncio
import json
import os
import time
import aiohttp
import websockets
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
DRY_RUN         = os.environ.get("DRY_RUN", "true").lower() != "false"
TRADE_USD       = float(os.environ.get("TRADE_USD", "5.0"))       # USDC per live trade
PAPER_SHARES    = int(os.environ.get("SHARES", "100"))             # shares per paper trade
MOVE_THRESH     = float(os.environ.get("MOVE_THRESH", "0.05"))    # % move from candle open
MAX_STALE_MID   = float(os.environ.get("MAX_STALE_MID", "0.45"))  # only enter when genuinely stale
COOLDOWN        = int(os.environ.get("COOLDOWN", "30"))
MAX_ENTRIES     = int(os.environ.get("MAX_ENTRIES", "3"))          # per candle per market
MIN_ASK         = 0.02
MAX_ASK         = 0.85  # don't buy expensive — that's not stale

POLY_CLOB_URL = "https://clob.polymarket.com"
POLY_WS_URL   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
COINBASE_WS   = "wss://ws-feed.exchange.coinbase.com"

MARKET_CONFIGS = [
    {"label": "BTC_5m",  "slug_prefix": "btc", "interval": 300,  "cb_id": "BTC-USD"},
    {"label": "ETH_5m",  "slug_prefix": "eth", "interval": 300,  "cb_id": "ETH-USD"},
    {"label": "BTC_15m", "slug_prefix": "btc", "interval": 900,  "cb_id": "BTC-USD"},
    {"label": "ETH_15m", "slug_prefix": "eth", "interval": 900,  "cb_id": "ETH-USD"},
]

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))


# ── Poly CLOB client (live mode only) ──────────────────────────────────────
_clob = None
_live_session = None

def init_live_client():
    global _clob, _live_session
    if DRY_RUN:
        return
    try:
        from py_clob_client.client import ClobClient
        pk = os.environ.get("POLY_PRIVATE_KEY", "")
        if not pk:
            print("[LIVE] ERROR: POLY_PRIVATE_KEY not set"); return
        _clob = ClobClient(
            host=POLY_CLOB_URL,
            key=pk,
            chain_id=137,
            signature_type=2,
            funder="0x6826c3197fff281144b07fe6c3e72636854769ab",
        )
        creds = _clob.create_or_derive_api_creds()
        _clob.set_api_creds(creds)
        print(f"[LIVE] CLOB client ready — key={creds.api_key[:8]}...")
    except Exception as e:
        print(f"[LIVE] ERROR: {e}")
        _clob = None


async def fire_live_order(token_id, side_label):
    """Place a FAK market order. Returns in <50ms from a co-located VPS."""
    global _clob
    if not _clob:
        return None
    try:
        from py_clob_client.clob_types import MarketOrderArgs
        args = MarketOrderArgs(
            token_id=token_id,
            amount=TRADE_USD,
            side="BUY",
            price=0.99,
        )
        t0 = time.monotonic()
        signed = await asyncio.to_thread(_clob.create_market_order, args)
        resp = await asyncio.to_thread(_clob.post_order, signed, "FAK")
        latency = (time.monotonic() - t0) * 1000
        taking = float(resp.get("takingAmount", 0))
        making = float(resp.get("makingAmount", 0))
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] LIVE FILL {side_label} | spent=${making:.2f} got={taking:.4f}sh | latency={latency:.0f}ms")
        return resp
    except Exception as e:
        print(f"  LIVE ORDER ERROR: {e}")
        return None


# ── Market token lookup ─────────────────────────────────────────────────────
async def get_market(slug_prefix, interval):
    now = time.time()
    tf = "5m" if interval == 300 else "15m"
    async with aiohttp.ClientSession() as s:
        for offset in range(3):
            candle_ts = int(now // interval) * interval - (offset * interval)
            slug = f"{slug_prefix}-updown-{tf}-{candle_ts}"
            try:
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10) as r:
                    data = await r.json()
                if data:
                    mkt = data[0].get("markets", [{}])[0]
                    tokens = json.loads(mkt.get("clobTokenIds", "[]"))
                    outcomes = json.loads(mkt.get("outcomes", "[]"))
                    if len(tokens) >= 2:
                        up_idx = 0 if outcomes[0] == "Up" else 1
                        dn_idx = 1 - up_idx
                        question = mkt.get("question") or slug
                        return tokens[up_idx], tokens[dn_idx], question, candle_ts
            except:
                pass
    return None, None, None, None


# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    mode = "PAPER" if DRY_RUN else "*** LIVE ***"
    print("=" * 70)
    print(f"  FAST MOMENTUM BOT -- {mode}")
    print(f"  MOVE_THRESH={MOVE_THRESH}% | MAX_STALE_MID={MAX_STALE_MID}")
    print(f"  MAX_ENTRIES={MAX_ENTRIES}/candle | COOLDOWN={COOLDOWN}s")
    print(f"  MAX_ASK={MAX_ASK} (reject expensive entries)")
    if DRY_RUN:
        print(f"  PAPER_SHARES={PAPER_SHARES}")
    else:
        print(f"  TRADE_USD=${TRADE_USD}")
    print(f"  Markets: {', '.join(c['label'] for c in MARKET_CONFIGS)}")
    print("=" * 70)
    print()

    if not DRY_RUN:
        init_live_client()

    # State
    markets = {}
    for cfg in MARKET_CONFIGS:
        markets[cfg["label"]] = {
            **cfg,
            "candle_ts": 0,
            "candle_open": None,   # Coinbase price at candle start
            "up_token": None, "dn_token": None,
            "up_bid": 0.0, "up_ask": 0.0,
            "dn_bid": 0.0, "dn_ask": 0.0,
            "question": "",
            "entries": [],
            "n_entries": 0,
        }

    cb_prices = {"BTC-USD": 0.0, "ETH-USD": 0.0}
    last_signal_time = 0.0
    session_pnl = 0.0
    session_trades = 0
    session_wins = 0

    def up_mid(m):
        return (m["up_bid"] + m["up_ask"]) / 2 if m["up_bid"] > 0 and m["up_ask"] > 0 else 0

    def dn_mid(m):
        return (m["dn_bid"] + m["dn_ask"]) / 2 if m["dn_bid"] > 0 and m["dn_ask"] > 0 else 0

    # ── RESOLVE ─────────────────────────────────────────────────────────────
    def resolve(mkt):
        nonlocal session_pnl, session_trades, session_wins
        if not mkt["entries"]:
            return
        um = up_mid(mkt); dm = dn_mid(mkt)
        winner = 'Up' if um >= dm else 'Down'
        total_cost = total_pnl = 0.0
        for e in mkt["entries"]:
            total_cost += e["cost"]
            if e["side"] == winner:
                total_pnl += (1.0 * e["shares"]) - e["cost"]
            else:
                total_pnl += 0 - e["cost"]
        session_pnl += total_pnl
        session_trades += 1
        if total_pnl > 0:
            session_wins += 1
        tag = "WIN" if total_pnl > 0 else "LOSS"
        wr = 100 * session_wins / session_trades if session_trades else 0
        print(f"\n  === {mkt['label']} RESOLVED | {winner} {tag} | {len(mkt['entries'])}e cost=${total_cost:.2f} PnL=${total_pnl:+.2f} | {session_trades}c WR={wr:.0f}% ${session_pnl:+.2f} ===\n")

    # ── CANDLE SETUP ────────────────────────────────────────────────────────
    async def setup(mkt):
        now = time.time()
        iv = mkt["interval"]
        new_cs = (int(now) // iv) * iv
        if new_cs == mkt["candle_ts"]:
            return
        if mkt["entries"]:
            resolve(mkt)
        mkt["candle_ts"] = new_cs
        mkt["entries"] = []
        mkt["n_entries"] = 0
        mkt["up_bid"] = mkt["up_ask"] = mkt["dn_bid"] = mkt["dn_ask"] = 0.0
        cb = cb_prices.get(mkt["cb_id"], 0)
        mkt["candle_open"] = cb if cb > 0 else None
        up, dn, q, _ = await get_market(mkt["slug_prefix"], iv)
        if up and dn:
            mkt["up_token"] = up; mkt["dn_token"] = dn; mkt["question"] = q
            print(f"[{mkt['label']}] {q}")

    # ── SIGNAL (called on EVERY price update — must be fast) ────────────────
    def try_signal(mkt):
        nonlocal last_signal_time
        now = time.time()
        if now - last_signal_time < COOLDOWN:
            return
        if mkt["n_entries"] >= MAX_ENTRIES:
            return
        if mkt["candle_open"] is None:
            return
        cb = cb_prices.get(mkt["cb_id"], 0)
        if cb <= 0:
            return
        offset = now - mkt["candle_ts"]
        if offset < 10 or offset > mkt["interval"] - 20:
            return

        move = (cb - mkt["candle_open"]) / mkt["candle_open"] * 100
        if abs(move) < MOVE_THRESH:
            return

        direction = "Up" if move > 0 else "Down"
        if direction == "Up":
            mid = up_mid(mkt); ask = mkt["up_ask"]; token = mkt["up_token"]
        else:
            mid = dn_mid(mkt); ask = mkt["dn_ask"]; token = mkt["dn_token"]

        # KEY FILTER: only enter when genuinely stale
        if mid <= 0 or mid > MAX_STALE_MID:
            return
        if ask < MIN_ASK or ask > MAX_ASK:
            return

        # FIRE
        t0 = time.monotonic()
        cost = ask * PAPER_SHARES + fee(PAPER_SHARES, ask)
        mkt["entries"].append({"side": direction, "ask": ask, "shares": PAPER_SHARES, "cost": cost, "ts": now})
        mkt["n_entries"] += 1
        last_signal_time = now

        latency_us = (time.monotonic() - t0) * 1_000_000
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        print(
            f"  [{ts}] {'PAPER' if DRY_RUN else 'LIVE'} [{mkt['label']}] {direction} "
            f"ask={ask:.3f} mid={mid:.3f} mv={move:+.3f}% t+{int(offset)}s "
            f"cost=${cost:.2f} e={mkt['n_entries']}/{MAX_ENTRIES} "
            f"detect={latency_us:.0f}us",
            flush=True
        )

        # Live order in background (don't block detection loop)
        if not DRY_RUN and token:
            asyncio.create_task(fire_live_order(token, f"{mkt['label']} {direction}"))

    # ── COINBASE WS ─────────────────────────────────────────────────────────
    async def coinbase_ws():
        sub = {
            "type": "subscribe",
            "channels": [{"name": "ticker", "product_ids": ["BTC-USD", "ETH-USD"]}]
        }
        while True:
            try:
                async with websockets.connect(COINBASE_WS, ping_interval=30) as ws:
                    await ws.send(json.dumps(sub))
                    print("[CB] Connected")
                    async for msg in ws:
                        d = json.loads(msg)
                        if d.get("type") == "ticker":
                            pid = d.get("product_id")
                            p = float(d.get("price", 0))
                            if pid and p > 0:
                                cb_prices[pid] = p
                                # Check ALL markets on every CB tick (fast path)
                                for mkt in markets.values():
                                    if mkt["cb_id"] == pid:
                                        try_signal(mkt)
            except Exception as e:
                print(f"[CB] Error: {e}")
                await asyncio.sleep(2)

    # ── POLYMARKET WS ───────────────────────────────────────────────────────
    async def poly_ws(mkt):
        while True:
            try:
                await setup(mkt)
                if not mkt["up_token"] or not mkt["dn_token"]:
                    await asyncio.sleep(5); continue

                token_side = {mkt["up_token"]: "Up", mkt["dn_token"]: "Down"}
                sub = {"type": "market", "assets_ids": [mkt["up_token"], mkt["dn_token"]]}

                async with websockets.connect(POLY_WS_URL, ping_interval=30) as ws:
                    await ws.send(json.dumps(sub))
                    async for msg in ws:
                        now = time.time()
                        new_cs = (int(now) // mkt["interval"]) * mkt["interval"]
                        if new_cs != mkt["candle_ts"]:
                            await setup(mkt); break

                        data = json.loads(msg)
                        if isinstance(data, list):
                            for item in data:
                                side = token_side.get(item.get("asset_id"))
                                if not side: continue
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                bb = max((float(b["price"]) for b in bids), default=0) if bids else 0
                                ba = min((float(a["price"]) for a in asks), default=0) if asks else 0
                                if side == "Up":
                                    if bb > 0: mkt["up_bid"] = bb
                                    if ba > 0: mkt["up_ask"] = ba
                                else:
                                    if bb > 0: mkt["dn_bid"] = bb
                                    if ba > 0: mkt["dn_ask"] = ba
                                # Check signal on every Poly update too
                                try_signal(mkt)
            except Exception as e:
                print(f"[{mkt['label']}] WS: {e}")
                await asyncio.sleep(3)

    # ── Periodic candle check + status ──────────────────────────────────────
    async def tick_loop():
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for mkt in markets.values():
                new_cs = (int(now) // mkt["interval"]) * mkt["interval"]
                if new_cs != mkt["candle_ts"] and mkt["candle_ts"] > 0:
                    await setup(mkt)

    async def status_loop():
        while True:
            await asyncio.sleep(15)
            now = time.time()
            parts = []
            for label, m in markets.items():
                cb = cb_prices.get(m["cb_id"], 0)
                op = m["candle_open"]
                mv = (cb - op) / op * 100 if cb > 0 and op else 0
                um = up_mid(m); dm = dn_mid(m)
                rem = m["interval"] - int(now - m["candle_ts"])
                parts.append(f"{label}:{mv:+.2f}% U={um:.2f} D={dm:.2f} e={m['n_entries']}/{MAX_ENTRIES} {rem}s")
            wr = 100 * session_wins / session_trades if session_trades else 0
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] {' | '.join(parts)} | {session_trades}c WR={wr:.0f}% ${session_pnl:+.2f}", flush=True)

    # ── LAUNCH ──────────────────────────────────────────────────────────────
    # Warmup: pre-establish TCP connections
    if not DRY_RUN:
        async with aiohttp.ClientSession() as s:
            try:
                async with s.get(f"{POLY_CLOB_URL}/time") as r:
                    await r.text()
                print("[WARMUP] Poly CLOB connection pre-warmed")
            except:
                pass

    tasks = [
        asyncio.create_task(coinbase_ws()),
        asyncio.create_task(tick_loop()),
        asyncio.create_task(status_loop()),
    ]
    for mkt in markets.values():
        tasks.append(asyncio.create_task(poly_ws(mkt)))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
