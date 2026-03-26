"""
main.py — Cross-platform arb bot (Polymarket ↔ Kalshi)
========================================================
Phase 1: Monitor only (DRY_RUN=True) — logs opportunities without trading
Phase 2: Set DRY_RUN=False to execute live trades

Usage:
    python main.py

Config via environment variables:
    KALSHI_KEY_ID     — Kalshi API key ID
    KALSHI_KEY_PATH   — Path to Kalshi RSA private key .pem file
    DRY_RUN           — "true" (default) or "false"
    MIN_PROFIT_CENTS  — Minimum profit per share to trade (default: 0.5)
"""

import asyncio
import json
import os
import time
from datetime import datetime

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient
from market_mapper import build_map
from arb_detector import ArbState

# ── Config ────────────────────────────────────────────────────────────────────
KALSHI_KEY_ID   = os.environ.get("KALSHI_KEY_ID",   "")
KALSHI_KEY_PATH = os.environ.get("KALSHI_KEY_PATH",  "kalshi_key.pem")
DRY_RUN         = os.environ.get("DRY_RUN", "true").lower() != "false"
SHARES_PER_TRADE = int(os.environ.get("SHARES_PER_TRADE", "100"))

total_opps  = 0
total_profit = 0.0


# ── Arb callback ──────────────────────────────────────────────────────────────
async def on_arb(opp: dict):
    global total_opps, total_profit
    total_opps  += 1
    total_profit += opp["profit"] * SHARES_PER_TRADE

    ts    = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    pair  = opp["pair"]
    print(
        f"[{ts}] ARB  {pair['symbol']} {pair['timeframe']} | "
        f"poly {opp['poly_side']} @ {opp['poly_ask']:.3f}  "
        f"kalshi {opp['kalshi_side']} @ {opp['kalshi_ask']:.3f}  "
        f"profit={opp['profit']*100:.2f}¢  ROI={opp['roi_pct']:.1f}%  "
        f"({'DRY' if DRY_RUN else 'LIVE'})"
    )

    if DRY_RUN:
        return

    # ── Execute both legs concurrently ────────────────────────────────────────
    kalshi = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
    poly   = PolymarketClient()

    poly_token = (pair["poly_up_token"] if opp["poly_side"] == "poly_up"
                  else pair["poly_down_token"])

    # Kalshi: use maker (post_only) for 0% fee
    k_side = "yes" if opp["kalshi_side"].endswith("up") and pair["kalshi_yes_is"] == "up" else "no"
    k_price_cents = int(opp["kalshi_ask"] * 100)

    try:
        poly_task   = asyncio.create_task(
            poly.place_order(poly_token, opp["poly_ask"], SHARES_PER_TRADE)
        )
        kalshi_task = asyncio.create_task(
            kalshi.place_order(pair["kalshi_ticker"], k_side, k_price_cents,
                               SHARES_PER_TRADE, post_only=True)
        )
        poly_result, kalshi_result = await asyncio.gather(
            poly_task, kalshi_task, return_exceptions=True
        )
        print(f"  poly={poly_result}  kalshi={kalshi_result}")
    except Exception as e:
        print(f"  ERROR executing: {e}")


# ── Price feed tasks ──────────────────────────────────────────────────────────
async def run_kalshi_feed(client: KalshiClient, state: ArbState, tickers: list):
    async def handle(msg):
        d = msg.get("msg", {})
        ticker = d.get("market_ticker")
        if not ticker:
            return
        yes_ask = d.get("yes_ask") / 100 if d.get("yes_ask") else None
        yes_bid = d.get("yes_bid") / 100 if d.get("yes_bid") else None
        no_ask  = d.get("no_ask")  / 100 if d.get("no_ask")  else None
        no_bid  = d.get("no_bid")  / 100 if d.get("no_bid")  else None
        if None not in (yes_ask, yes_bid, no_ask, no_bid):
            state.update_kalshi(ticker, yes_bid, yes_ask, no_bid, no_ask)

    while True:
        try:
            await client.subscribe_ticker(tickers, handle)
        except Exception as e:
            print(f"Kalshi WS error: {e} — reconnecting in 2s")
            await asyncio.sleep(2)


async def run_poly_feed(state: ArbState, pairs: list):
    """Subscribe to all Polymarket Up+Down tokens."""
    poly = PolymarketClient()

    # Build asset_id → (condition_id, side) map
    token_map = {}
    asset_ids = []
    for p in pairs:
        token_map[p["poly_up_token"]]   = (p["poly_condition"], "up")
        token_map[p["poly_down_token"]] = (p["poly_condition"], "down")
        asset_ids.append(p["poly_up_token"])
        asset_ids.append(p["poly_down_token"])

    # Track both sides per condition
    price_buf = {}  # condition_id → {up_bid, up_ask, down_bid, down_ask}

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
        if all(k in buf for k in ("up_bid","up_ask","down_bid","down_ask")):
            state.update_poly(cond, buf["up_bid"], buf["up_ask"],
                              buf["down_bid"], buf["down_ask"])

    while True:
        try:
            await poly.subscribe_prices(asset_ids, handle)
        except Exception as e:
            print(f"Polymarket WS error: {e} — reconnecting in 2s")
            await asyncio.sleep(2)


async def stats_printer():
    while True:
        await asyncio.sleep(60)
        print(f"[stats] opps={total_opps}  sim_profit=${total_profit:.2f}  mode={'DRY' if DRY_RUN else 'LIVE'}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    if not KALSHI_KEY_ID or not os.path.exists(KALSHI_KEY_PATH):
        print("ERROR: Set KALSHI_KEY_ID and KALSHI_KEY_PATH env vars")
        print("  export KALSHI_KEY_ID=your_key_id")
        print("  export KALSHI_KEY_PATH=/path/to/key.pem")
        return

    print(f"Mode: {'DRY RUN (monitoring only)' if DRY_RUN else '*** LIVE TRADING ***'}")
    print("Building market map...")

    # Load cached map if available (re-run market_mapper.py to refresh)
    if os.path.exists("market_map.json"):
        with open("market_map.json") as f:
            market_map = json.load(f)
        print(f"  Loaded {len(market_map)} pairs from market_map.json")
    else:
        kalshi = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
        market_map = await build_map(KALSHI_KEY_ID, KALSHI_KEY_PATH)

    if not market_map:
        print("No matched market pairs found. Run market_mapper.py first.")
        return

    state         = ArbState(market_map, on_arb)
    kalshi_client = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
    kalshi_tickers = list({p["kalshi_ticker"] for p in market_map})

    print(f"Monitoring {len(market_map)} pairs across {len(kalshi_tickers)} Kalshi markets")
    print("Connecting to WebSockets...\n")

    await asyncio.gather(
        run_kalshi_feed(kalshi_client, state, kalshi_tickers),
        run_poly_feed(state, market_map),
        stats_printer(),
    )


if __name__ == "__main__":
    asyncio.run(main())
