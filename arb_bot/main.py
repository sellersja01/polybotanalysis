"""
main.py — High-speed cross-platform arb bot
=============================================
Architecture for sub-50ms execution:
  1. Persistent aiohttp sessions (skip TLS on every trade)
  2. Parallel leg execution via asyncio.gather()
  3. Reverse-indexed arb detector (O(1) lookups)
  4. Dedup to prevent re-firing same opportunity
  5. Nanosecond latency tracking on every operation

Usage:
    export KALSHI_KEY_ID=your_key_id
    export KALSHI_KEY_PATH=/path/to/key.pem
    export DRY_RUN=true
    python main.py
"""
import asyncio
import json
import os
import time

import aiohttp

from config import KALSHI_KEY_ID, KALSHI_KEY_PATH, DRY_RUN, SHARES_PER_TRADE
from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from arb_detector import ArbState
from executor import Executor


# ── Warm up connections ──────────────────────────────────────────────────────
async def warmup_session(session: aiohttp.ClientSession, url: str, label: str):
    """Send a throwaway GET to establish TCP+TLS. Subsequent calls reuse it."""
    t0 = time.perf_counter_ns()
    try:
        async with session.get(url) as r:
            await r.read()
        ms = (time.perf_counter_ns() - t0) / 1e6
        print(f"  {label}: {ms:.0f}ms (connection warm)")
    except Exception as e:
        print(f"  {label}: warmup failed ({e})")


# ── Price feed: Kalshi ───────────────────────────────────────────────────────
async def run_kalshi_feed(client: KalshiClient, state: ArbState, tickers: list):
    async def handle(msg):
        d = msg.get("msg", {})
        ticker = d.get("market_ticker")
        if not ticker:
            return
        # Kalshi WS only sends yes_bid/yes_ask — derive no prices
        yes_bid = d.get("yes_bid")
        yes_ask = d.get("yes_ask")
        if yes_bid is None or yes_ask is None:
            return
        yes_bid /= 100
        yes_ask /= 100
        no_bid = 1.0 - yes_ask
        no_ask = 1.0 - yes_bid
        state.update_kalshi(ticker, yes_bid, yes_ask, no_bid, no_ask)

    while True:
        try:
            await client.subscribe_ticker(tickers, handle)
        except Exception as e:
            print(f"Kalshi WS error: {e} — reconnecting in 2s")
            await asyncio.sleep(2)


# ── Price feed: Polymarket ───────────────────────────────────────────────────
async def run_poly_feed(client: PolymarketClient, state: ArbState, pairs: list):
    token_map = {}
    asset_ids = []
    for p in pairs:
        token_map[p["poly_up_token"]]   = (p["poly_condition"], "up")
        token_map[p["poly_down_token"]] = (p["poly_condition"], "down")
        asset_ids.append(p["poly_up_token"])
        asset_ids.append(p["poly_down_token"])

    price_buf = {}

    async def handle(msg):
        asset_id = msg.get("asset_id")
        if asset_id not in token_map:
            return
        cond, side = token_map[asset_id]
        bid = float(msg.get("best_bid") or 0)
        ask = float(msg.get("best_ask") or 0)
        if cond not in price_buf:
            price_buf[cond] = {}
        price_buf[cond][f"{side}_bid"] = bid
        price_buf[cond][f"{side}_ask"] = ask
        buf = price_buf[cond]
        if all(k in buf for k in ("up_bid", "up_ask", "down_bid", "down_ask")):
            state.update_poly(cond, buf["up_bid"], buf["up_ask"],
                              buf["down_bid"], buf["down_ask"])

    while True:
        try:
            await client.subscribe_prices(asset_ids, handle)
        except Exception as e:
            print(f"Polymarket WS error: {e} — reconnecting in 2s")
            await asyncio.sleep(2)


# ── Stats printer ────────────────────────────────────────────────────────────
async def stats_printer(executor: Executor):
    while True:
        await asyncio.sleep(30)
        print(f"[stats] {executor.stats_summary()}")


# ── Entry point ──────────────────────────────────────────────────────────────
async def main():
    if not KALSHI_KEY_ID or not os.path.exists(KALSHI_KEY_PATH):
        print("ERROR: Set KALSHI_KEY_ID and KALSHI_KEY_PATH env vars")
        return

    mode = "DRY RUN" if DRY_RUN else "*** LIVE TRADING ***"
    print(f"{'='*60}")
    print(f"  ARB BOT — {mode}")
    print(f"  Shares/trade: {SHARES_PER_TRADE}")
    print(f"{'='*60}")

    # ── Create persistent sessions ────────────────────────────────────────────
    print("\nCreating persistent sessions...")
    connector_k = aiohttp.TCPConnector(limit=10, keepalive_timeout=60)
    connector_p = aiohttp.TCPConnector(limit=10, keepalive_timeout=60)

    kalshi_session = aiohttp.ClientSession(
        connector=connector_k,
        timeout=aiohttp.ClientTimeout(total=5, connect=2),
    )
    poly_session = aiohttp.ClientSession(
        connector=connector_p,
        timeout=aiohttp.ClientTimeout(total=5, connect=2),
    )

    # ── Create clients with shared sessions ───────────────────────────────────
    kalshi = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH, session=kalshi_session)
    poly   = PolymarketClient(session=poly_session)

    # ── Warm up connections (establish TCP+TLS once) ──────────────────────────
    print("Warming up connections...")
    await asyncio.gather(
        warmup_session(kalshi_session,
                       "https://api.elections.kalshi.com/trade-api/v2/exchange/status",
                       "Kalshi"),
        warmup_session(poly_session,
                       "https://clob.polymarket.com/time",
                       "Polymarket"),
    )

    # ── Load market map ───────────────────────────────────────────────────────
    print("\nLoading market map...")
    if os.path.exists("market_map.json"):
        with open("market_map.json") as f:
            market_map = json.load(f)
        print(f"  Loaded {len(market_map)} pairs from market_map.json")
    else:
        from market_mapper import build_map
        market_map = await build_map(KALSHI_KEY_ID, KALSHI_KEY_PATH)

    if not market_map:
        print("No matched market pairs. Run market_mapper.py first.")
        await kalshi_session.close()
        await poly_session.close()
        return

    # ── Create executor ───────────────────────────────────────────────────────
    executor = Executor(kalshi, poly)

    async def on_arb(opp: dict):
        await executor.execute(opp)

    state = ArbState(market_map, on_arb)
    kalshi_tickers = list({p["kalshi_ticker"] for p in market_map})

    print(f"\nMonitoring {len(market_map)} pairs, {len(kalshi_tickers)} Kalshi tickers")
    print("Connecting to WebSockets...\n")

    try:
        await asyncio.gather(
            run_kalshi_feed(kalshi, state, kalshi_tickers),
            run_poly_feed(poly, state, market_map),
            stats_printer(executor),
        )
    finally:
        executor.close()
        await kalshi_session.close()
        await poly_session.close()


if __name__ == "__main__":
    asyncio.run(main())
