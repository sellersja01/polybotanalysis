"""
main.py — High-speed cross-platform arb bot
=============================================
Architecture for sub-100ms execution:
  1. Persistent aiohttp sessions — TLS handshake once at startup
  2. Parallel leg execution — both orders fire via asyncio.gather()
  3. O(1) arb detector — reverse-indexed for instant lookups
  4. Dedup — won't re-fire same pair+direction within cooldown window
  5. Candle rollover — auto re-subscribes to new tickers every 15m
  6. Nanosecond latency tracking on every trade

Usage:
    export POLY_PRIVATE_KEY=0xYOUR_PRIVATE_KEY
    export DRY_RUN=true          # paper mode (default)
    export DRY_RUN=false         # live trading
    export SHARES_PER_TRADE=100
    python main.py
"""
import asyncio
import json
import time
import signal

import aiohttp
import websockets

from config import (
    KALSHI_KEY_ID, KALSHI_KEY_PATH,
    POLY_PRIVATE_KEY, POLY_ADDRESS,
    DRY_RUN, SHARES_PER_TRADE, CANDLE_INTERVAL,
)
from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from arb_detector import ArbState
from executor import Executor
from market_mapper import build_map, current_candle_ts


# ── Connection warmup ─────────────────────────────────────────────────────────
async def warmup(session: aiohttp.ClientSession, url: str, label: str):
    t0 = time.perf_counter_ns()
    try:
        async with session.get(url) as r:
            await r.read()
        ms = (time.perf_counter_ns() - t0) / 1e6
        print(f"  [{label}] warm in {ms:.0f}ms", flush=True)
    except Exception as e:
        print(f"  [{label}] warmup failed: {e}", flush=True)


# ── Kalshi WebSocket feed ─────────────────────────────────────────────────────
async def run_kalshi_feed(kalshi: KalshiClient, state: ArbState,
                          get_tickers, stop_event: asyncio.Event):
    """
    Persistent Kalshi WS feed. Auto-reconnects.
    Calls get_tickers() each reconnect to get the current ticker list.
    """
    while not stop_event.is_set():
        tickers = get_tickers()
        if not tickers:
            await asyncio.sleep(2)
            continue
        try:
            headers = kalshi.ws_headers()
            from config import KALSHI_WS_URL
            async with websockets.connect(
                KALSHI_WS_URL, additional_headers=headers,
                ping_interval=20, ping_timeout=10
            ) as ws:
                for i, ticker in enumerate(tickers):
                    await ws.send(json.dumps({
                        "id": i + 1, "cmd": "subscribe",
                        "params": {"channels": ["ticker"], "market_ticker": ticker},
                    }))
                print(f"[kalshi] WS connected — {len(tickers)} tickers", flush=True)

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                        if msg.get("type") != "ticker":
                            continue
                        d      = msg.get("msg", {})
                        ticker = d.get("market_ticker")
                        if not ticker:
                            continue

                        # WS sends yes_bid_dollars / yes_ask_dollars
                        yes_bid = d.get("yes_bid_dollars")
                        yes_ask = d.get("yes_ask_dollars")
                        if yes_bid is None or yes_ask is None:
                            continue

                        yes_bid = float(yes_bid)
                        yes_ask = float(yes_ask)
                        no_bid  = round(1.0 - yes_ask, 4)
                        no_ask  = round(1.0 - yes_bid, 4)

                        print(f"[kalshi] {ticker}: yes_bid={yes_bid:.3f} yes_ask={yes_ask:.3f}", flush=True)
                        state.update_kalshi(ticker, yes_bid, yes_ask, no_bid, no_ask)
                    except Exception:
                        pass

        except Exception as e:
            if not stop_event.is_set():
                print(f"[kalshi] WS error: {e} — reconnecting in 3s", flush=True)
                await asyncio.sleep(3)


# ── Polymarket WebSocket feed ─────────────────────────────────────────────────
async def run_poly_feed(state: ArbState, get_pairs, stop_event: asyncio.Event):
    """
    Persistent Polymarket WS feed. Auto-reconnects.
    Calls get_pairs() each reconnect to get current token IDs.
    """
    from config import POLY_WS_URL

    while not stop_event.is_set():
        pairs = get_pairs()
        if not pairs:
            await asyncio.sleep(2)
            continue

        # Build token → (condition_id, side) index
        token_map = {}
        asset_ids = []
        for p in pairs:
            if p.get("poly_up_token"):
                token_map[p["poly_up_token"]]   = (p["poly_condition"], "up")
                token_map[p["poly_down_token"]]  = (p["poly_condition"], "down")
                asset_ids.append(p["poly_up_token"])
                asset_ids.append(p["poly_down_token"])

        if not asset_ids:
            await asyncio.sleep(2)
            continue

        price_buf = {}

        try:
            async with websockets.connect(POLY_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": asset_ids, "markets": [],
                }))
                print(f"[poly] WS connected — {len(pairs)} pairs", flush=True)

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        msgs = json.loads(raw)
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        for msg in msgs:
                            asset_id = msg.get("asset_id")
                            if asset_id not in token_map:
                                continue

                            cond, side = token_map[asset_id]

                            bids = msg.get("bids", [])
                            asks = msg.get("asks", [])
                            if bids and asks:
                                best_bid = float(bids[0]["price"])
                                best_ask = float(asks[0]["price"])
                            else:
                                best_bid = float(msg.get("best_bid") or msg.get("bid") or 0)
                                best_ask = float(msg.get("best_ask") or msg.get("ask") or 0)

                            if not best_bid and not best_ask:
                                continue

                            if cond not in price_buf:
                                price_buf[cond] = {}
                            price_buf[cond][f"{side}_bid"] = best_bid
                            price_buf[cond][f"{side}_ask"] = best_ask

                            buf = price_buf[cond]
                            if all(k in buf for k in ("up_bid","up_ask","down_bid","down_ask")):
                                state.update_poly(
                                    cond,
                                    buf["up_bid"], buf["up_ask"],
                                    buf["down_bid"], buf["down_ask"],
                                )
                    except Exception:
                        pass

        except Exception as e:
            if not stop_event.is_set():
                print(f"[poly] WS error: {e} — reconnecting in 3s", flush=True)
                await asyncio.sleep(3)


# ── Polymarket RTDS Chainlink feed (captures BTC price at each candle open) ───
async def run_rtds_feed(state_container: dict, stop_event: asyncio.Event):
    RTDS_URL = "wss://ws-live-data.polymarket.com"
    sub = json.dumps({
        "action": "subscribe",
        "subscriptions": [{
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": json.dumps({"symbol": "btc/usd"})
        }]
    })
    state_container.setdefault("candle_ref_prices", {})

    while not stop_event.is_set():
        try:
            async with websockets.connect(RTDS_URL, ping_interval=20) as ws:
                await ws.send(sub)
                async for raw in ws:
                    if stop_event.is_set():
                        break
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                        points = msg.get("payload", {}).get("data", [])
                        for pt in points:
                            ts_s = pt["timestamp"] / 1000
                            price = float(pt["value"])
                            candle_ts = (int(ts_s) // CANDLE_INTERVAL) * CANDLE_INTERVAL
                            ref = state_container["candle_ref_prices"]
                            if candle_ts not in ref:
                                ref[candle_ts] = price
                                print(f"[rtds] candle {candle_ts} open: ${price:,.2f}", flush=True)
                    except Exception:
                        pass
        except Exception as e:
            if not stop_event.is_set():
                print(f"[rtds] error: {e} — reconnecting in 3s", flush=True)
                await asyncio.sleep(3)


# ── Candle rollover manager ───────────────────────────────────────────────────
async def candle_manager(kalshi_session: aiohttp.ClientSession,
                         poly_session: aiohttp.ClientSession,
                         kalshi: KalshiClient,
                         state_container: dict,
                         executor: Executor,
                         stop_event: asyncio.Event):
    """
    Every 15 minutes, fetches new market map and updates state.
    Feeds (Kalshi WS, Poly WS) read from state_container["pairs"] dynamically.
    """
    while not stop_event.is_set():
        # Wait until the next candle boundary + 15s buffer for Kalshi to publish
        now       = time.time()
        candle_end = ((int(now) // CANDLE_INTERVAL) + 1) * CANDLE_INTERVAL
        wait_secs  = candle_end - now + 15
        print(f"[candle] next rollover in {wait_secs:.0f}s", flush=True)
        await asyncio.sleep(wait_secs)

        if stop_event.is_set():
            break

        print("[candle] rolling over — fetching new markets...", flush=True)
        try:
            # Wait up to 5s for RTDS to capture the new candle's open price
            poly_ref = None
            for _ in range(10):
                poly_ref = state_container.get("candle_ref_prices", {}).get(candle_end)
                if poly_ref:
                    break
                await asyncio.sleep(0.5)
            new_pairs = await build_map(
                poly_session, kalshi,
                min_open_ts=candle_end,
                poly_ref_price=poly_ref,
            )
            if new_pairs:
                state_container["pairs"] = new_pairs
                kalshi_tickers = list({p["kalshi_ticker"] for p in new_pairs})
                state_container["tickers"] = kalshi_tickers

                # Rebuild ArbState with new pairs
                new_state = ArbState(new_pairs, state_container["on_arb"])
                state_container["arb_state"] = new_state

                print(f"[candle] rollover complete — {len(new_pairs)} pairs", flush=True)
            else:
                print("[candle] WARNING: no pairs found after rollover", flush=True)
        except Exception as e:
            print(f"[candle] rollover error: {e}", flush=True)


# ── Polymarket Gamma REST poller (fallback price source) ──────────────────────
async def run_poly_gamma_poller(session: aiohttp.ClientSession, state_container: dict,
                                proxy_state, stop_event: asyncio.Event):
    """
    Polls Polymarket Gamma API every 2s for outcomePrices (AMM mid prices).
    Validates that prices are for the CURRENT candle and sum to ~1.0.
    """
    from config import POLY_GAMMA_URL
    POLL_SECS = 2
    INTERVAL  = 900

    while not stop_event.is_set():
        await asyncio.sleep(POLL_SECS)
        pairs = state_container.get("pairs", [])
        if not pairs:
            continue

        candle_ts = (int(time.time()) // INTERVAL) * INTERVAL

        for pair in pairs:
            asset = pair.get("symbol", "")
            cond  = pair.get("poly_condition")
            try:
                slug = f"{asset.lower()}-updown-15m-{candle_ts}"
                async with session.get(f"{POLY_GAMMA_URL}/events",
                                       params={"slug": slug}) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()

                events = data if isinstance(data, list) else data.get("events", [])
                if not events:
                    continue
                market = events[0].get("markets", [None])[0]
                if not market:
                    continue

                outcomes       = market.get("outcomes", "[]")
                outcome_prices = market.get("outcomePrices", "[]")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                if len(outcome_prices) < 2:
                    continue

                up_idx = next(
                    (i for i, o in enumerate(outcomes) if str(o).lower() == "up"), 0
                )
                up_mid = float(outcome_prices[up_idx])
                dn_mid = float(outcome_prices[1 - up_idx])

                # Sanity checks — reject bad/stale data
                total = up_mid + dn_mid
                if total < 0.90 or total > 1.10:
                    print(f"[poly] {asset}: bad prices up={up_mid:.3f} dn={dn_mid:.3f} sum={total:.3f} — skipping", flush=True)
                    continue
                if up_mid <= 0.02 or up_mid >= 0.98:
                    continue  # settled, skip

                # Gamma gives mid price — estimate ask as mid+0.01 (1¢ spread)
                proxy_state.update_poly(cond, up_mid - 0.01, up_mid + 0.01, dn_mid - 0.01, dn_mid + 0.01)
                print(f"[poly] {asset}: up={up_mid:.3f} dn={dn_mid:.3f}", flush=True)

            except Exception as e:
                print(f"[poly] {asset} error: {e}", flush=True)


# ── Stats printer ─────────────────────────────────────────────────────────────
async def stats_printer(executor: Executor, state_container: dict,
                        stop_event: asyncio.Event):
    while not stop_event.is_set():
        await asyncio.sleep(60)
        pairs = state_container.get("pairs", [])
        arb_st = state_container.get("arb_state")
        mode   = "DRY" if DRY_RUN else "LIVE"
        print(f"[{mode}] {executor.stats_summary()} | pairs={len(pairs)}", flush=True)

        if arb_st:
            for cond, pp in list(arb_st.poly_prices.items()):
                kp = arb_st.kalshi_prices.get(cond)
                if not kp:
                    continue
                pair = arb_st.pairs.get(cond, {})
                sym  = pair.get("symbol", "?")
                gap  = round((pp["up_ask"] + kp["dn_ask"]) - 1.0, 4)
                gap2 = round((pp["dn_ask"] + kp["up_ask"]) - 1.0, 4)
                best_gap = min(gap, gap2)  # negative = arb opportunity
                print(
                    f"  {sym}: poly_up={pp['up_ask']:.3f} kals_up={kp['up_ask']:.3f} "
                    f"gap={best_gap:+.3f}",
                    flush=True
                )


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    if not POLY_PRIVATE_KEY and not DRY_RUN:
        print("ERROR: Set POLY_PRIVATE_KEY env var to your wallet private key")
        print("       (0x... format from MetaMask → Account Details → Show private key)")
        return

    mode = "DRY RUN (no real orders)" if DRY_RUN else "*** LIVE TRADING ***"
    print(f"{'='*60}", flush=True)
    print(f"  ARB BOT — {mode}", flush=True)
    print(f"  Shares/trade: {SHARES_PER_TRADE}", flush=True)
    print(f"  Wallet: {POLY_ADDRESS}", flush=True)
    print(f"{'='*60}\n", flush=True)

    stop_event = asyncio.Event()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except Exception:
            pass

    # ── Persistent sessions ───────────────────────────────────────────────────
    print("Creating persistent sessions...", flush=True)
    kalshi_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
        timeout=aiohttp.ClientTimeout(total=5, connect=2),
    )
    poly_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
        timeout=aiohttp.ClientTimeout(total=5, connect=2),
    )

    # ── Clients ───────────────────────────────────────────────────────────────
    kalshi = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH, session=kalshi_session)
    poly   = PolymarketClient(session=poly_session)

    # ── Warmup ────────────────────────────────────────────────────────────────
    print("Warming up connections...", flush=True)
    await asyncio.gather(
        warmup(kalshi_session,
               "https://api.elections.kalshi.com/trade-api/v2/exchange/status",
               "kalshi"),
        warmup(poly_session,
               "https://clob.polymarket.com/time",
               "poly"),
    )

    # ── Initial market map ────────────────────────────────────────────────────
    print("\nFetching current market map...", flush=True)
    pairs = await build_map(poly_session, kalshi)
    if not pairs:
        print("ERROR: No market pairs found. Exiting.")
        await kalshi_session.close()
        await poly_session.close()
        return

    # ── Shared state container (updated on each candle rollover) ──────────────
    state_container = {
        "pairs":    pairs,
        "tickers":  list({p["kalshi_ticker"] for p in pairs}),
        "arb_state": None,
        "on_arb":    None,
    }

    # ── Executor ──────────────────────────────────────────────────────────────
    executor = Executor(kalshi, poly, db_path="arb_trades.db")

    async def on_arb(opp: dict):
        arb_st = state_container.get("arb_state")
        if arb_st and opp.get("condition_id") in arb_st.pairs:
            await executor.execute(opp)

    state_container["on_arb"] = on_arb

    arb_state = ArbState(pairs, on_arb)
    state_container["arb_state"] = arb_state

    # Feed functions read from state_container dynamically
    def get_tickers():
        return state_container.get("tickers", [])

    def get_pairs():
        return state_container.get("pairs", [])

    # ProxyState routes WS updates to the current arb_state (survives candle rollovers)
    # Calls ArbState class methods directly to avoid recursion from instance patching
    class ProxyState:
        def update_kalshi(self, *args, **kwargs):
            st = state_container.get("arb_state")
            if st:
                ArbState.update_kalshi(st, *args, **kwargs)
        def update_poly(self, *args, **kwargs):
            st = state_container.get("arb_state")
            if st:
                ArbState.update_poly(st, *args, **kwargs)

    proxy_state = ProxyState()

    print(f"\nMonitoring {len(pairs)} pairs, mode={mode}", flush=True)
    print("Connecting to WebSockets...\n", flush=True)

    try:
        await asyncio.gather(
            run_kalshi_feed(kalshi, proxy_state, get_tickers, stop_event),
            run_poly_feed(proxy_state, get_pairs, stop_event),
            run_poly_gamma_poller(poly_session, state_container, proxy_state, stop_event),
            run_rtds_feed(state_container, stop_event),
            candle_manager(kalshi_session, poly_session, kalshi,
                           state_container, executor, stop_event),
            stats_printer(executor, state_container, stop_event),
        )
    finally:
        print("\nShutting down...", flush=True)
        executor.close()
        await kalshi_session.close()
        await poly_session.close()


if __name__ == "__main__":
    asyncio.run(main())
