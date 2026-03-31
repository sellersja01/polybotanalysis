"""
test_latency_live.py — Live terminal test: watch Binance vs Polymarket lag
===========================================================================
No auth needed, no trading. Just connects to both public WebSockets
and prints when Polymarket is lagging behind Binance.

Usage: python test_latency_live.py
"""
import asyncio
import json
import time
import aiohttp
import websockets
from collections import deque
from datetime import datetime

# ── Fetch current BTC market from Polymarket ──────────────────────────────────
async def get_current_btc_market():
    """Find the current open BTC Up/Down 5m market on Polymarket using slug."""
    print("Fetching current BTC 5m market from Polymarket...", flush=True)
    now = time.time()
    async with aiohttp.ClientSession() as s:
        # Try current and recent 5m candle slugs
        for offset in range(5):
            candle_ts = int(now // 300) * 300 - (offset * 300)
            slug = f"btc-updown-5m-{candle_ts}"
            async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}") as r:
                data = await r.json()
            if data:
                event = data[0]
                for mkt in event.get("markets", []):
                    tokens = json.loads(mkt.get("clobTokenIds", "[]"))
                    outcomes = json.loads(mkt.get("outcomes", "[]"))
                    if len(tokens) >= 2 and len(outcomes) >= 2:
                        up_token = tokens[0] if outcomes[0] == "Up" else tokens[1]
                        dn_token = tokens[1] if outcomes[0] == "Up" else tokens[0]
                        cid = mkt.get("conditionId", "")
                        question = mkt.get("question") or event.get("title", slug)
                        print(f"  Found: {slug}")
                        return {
                            "question": question,
                            "condition_id": cid,
                            "up_token": up_token,
                            "dn_token": dn_token,
                        }
    return None


# ── Main live test ────────────────────────────────────────────────────────────
async def main():
    print("=" * 65)
    print("  LIVE LATENCY TEST — Binance vs Polymarket")
    print("  No auth, no trading. Just watching.")
    print("=" * 65)

    market = await get_current_btc_market()
    if not market:
        print("\nERROR: Could not find current BTC Up/Down market on Polymarket")
        print("Markets may be between candles. Try again in a minute.")
        return

    print(f"\n  Market: {market['question']}")
    print(f"  Up token: {market['up_token'][:20]}...")
    print(f"  Dn token: {market['dn_token'][:20]}...")

    # State
    btc_price = 0.0
    btc_buffer = deque(maxlen=3000)  # (time, price)
    poly_up_mid = 0.0
    poly_dn_mid = 0.0
    poly_up_ask = 0.0
    poly_dn_ask = 0.0
    poly_ts = 0.0
    signal_count = 0
    btc_tick_count = 0
    poly_tick_count = 0
    last_signal_time = 0

    LOOKBACK = 15  # seconds
    MOVE_THRESH = 0.05  # percent
    MIN_EDGE = 0.02  # 2 cents

    async def binance_feed():
        nonlocal btc_price, btc_tick_count
        url = "wss://stream.binance.com:9443/ws/btcusdt@trade"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    print("\n[Binance] Connected", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        p = float(msg.get("p", 0))
                        if p > 0:
                            btc_price = p
                            btc_buffer.append((time.time(), p))
                            btc_tick_count += 1

                            # Check for move
                            now = time.time()
                            cutoff = now - LOOKBACK
                            old_p = None
                            for ts, pr in btc_buffer:
                                if ts <= cutoff:
                                    old_p = pr
                                else:
                                    break
                            if old_p and old_p > 0:
                                move = (p - old_p) / old_p * 100
                                if abs(move) >= MOVE_THRESH:
                                    await check_lag(move, p, now)
            except Exception as e:
                print(f"[Binance] Error: {e} — reconnecting", flush=True)
                await asyncio.sleep(2)

    async def check_lag(move_pct, price, now):
        nonlocal signal_count, last_signal_time

        if poly_up_mid <= 0 or poly_dn_mid <= 0:
            return
        if now - last_signal_time < 5:  # 5s cooldown
            return

        direction = "UP" if move_pct > 0 else "DOWN"

        # If BTC went up, Up should be winning. Check if Poly still shows ~50/50
        if direction == "UP":
            # Up should be high, but if mid is still < 0.55 it's stale
            stale = poly_up_mid < 0.55
            entry = poly_up_ask
            side = "Up"
        else:
            stale = poly_dn_mid < 0.55
            entry = poly_dn_ask
            side = "Down"

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        if stale and abs(move_pct) >= MOVE_THRESH:
            signal_count += 1
            last_signal_time = now
            poly_age = (now - poly_ts) * 1000
            print(
                f"\n  >>> [{ts}] *** LAG DETECTED *** "
                f"BTC {direction} {move_pct:+.3f}% @ ${price:,.0f} | "
                f"Poly {side} mid={poly_up_mid if direction=='UP' else poly_dn_mid:.3f} "
                f"ask={entry:.3f} | "
                f"STALE — would buy {side} | "
                f"poly_age={poly_age:.0f}ms | "
                f"signal #{signal_count}",
                flush=True
            )
        # Always print status on moves (but less frequently)
        elif btc_tick_count % 500 == 0:
            print(
                f"  [{ts}] BTC {direction} {move_pct:+.3f}% @ ${price:,.0f} | "
                f"Poly Up={poly_up_mid:.3f} Dn={poly_dn_mid:.3f} | "
                f"no lag",
                flush=True
            )

    async def poly_feed():
        nonlocal poly_up_mid, poly_dn_mid, poly_up_ask, poly_dn_ask, poly_ts, poly_tick_count
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        asset_ids = [market["up_token"], market["dn_token"]]
        token_side = {market["up_token"]: "up", market["dn_token"]: "down"}

        while True:
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": asset_ids,
                        "type": "market",
                    }))
                    print("[Poly] Connected + subscribed", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        items = msg if isinstance(msg, list) else [msg]
                        for item in items:
                            aid = item.get("asset_id")

                            # Full book snapshot (initial + periodic)
                            if aid and aid in token_side and "bids" in item:
                                side = token_side[aid]
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                best_bid = max((float(b["price"]) for b in bids), default=0) if bids else 0
                                best_ask = min((float(a["price"]) for a in asks), default=0) if asks else 0
                                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
                                if side == "up":
                                    poly_up_mid = mid
                                    poly_up_ask = best_ask
                                else:
                                    poly_dn_mid = mid
                                    poly_dn_ask = best_ask
                                poly_ts = time.time()
                                poly_tick_count += 1
                                continue

                            # Price change updates
                            changes = item.get("price_changes", [])
                            for ch in changes:
                                ch_aid = ch.get("asset_id")
                                if ch_aid not in token_side:
                                    continue
                                side = token_side[ch_aid]
                                price = float(ch.get("price", 0))
                                ch_side = ch.get("side", "")
                                if ch_side == "BUY":  # bid update
                                    if side == "up":
                                        poly_up_mid = (price + poly_up_ask) / 2 if poly_up_ask else price
                                    else:
                                        poly_dn_mid = (price + poly_dn_ask) / 2 if poly_dn_ask else price
                                elif ch_side == "SELL":  # ask update
                                    if side == "up":
                                        poly_up_ask = price
                                        poly_up_mid = (poly_up_mid + price) / 2 if poly_up_mid else price
                                    else:
                                        poly_dn_ask = price
                                        poly_dn_mid = (poly_dn_mid + price) / 2 if poly_dn_mid else price
                                poly_ts = time.time()
                                poly_tick_count += 1
            except Exception as e:
                print(f"[Poly] Error: {e} — reconnecting", flush=True)
                await asyncio.sleep(2)

    async def status_printer():
        while True:
            await asyncio.sleep(15)
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"  [{ts}] btc=${btc_price:,.0f} | "
                f"poly up={poly_up_mid:.3f} dn={poly_dn_mid:.3f} | "
                f"btc_ticks={btc_tick_count:,} poly_ticks={poly_tick_count:,} | "
                f"signals={signal_count}",
                flush=True
            )

    print("\nStarting feeds...\n", flush=True)
    await asyncio.gather(
        binance_feed(),
        poly_feed(),
        status_printer(),
    )


if __name__ == "__main__":
    asyncio.run(main())
