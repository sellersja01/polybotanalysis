"""
market_mapper.py — Maps current Polymarket candles to Kalshi tickers
=====================================================================
Fetches the CURRENT open 15m candle for each asset from both platforms.
Saves to market_map.json. Called at startup and every candle rollover.
"""
import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp

from config import KALSHI_SERIES, ASSETS, KALSHI_KEY_ID, KALSHI_KEY_PATH, POLY_GAMMA_URL
from kalshi_client import KalshiClient


INTERVAL = 900  # 15 minutes


def current_candle_ts() -> int:
    """Unix timestamp of the START of the current 15m candle."""
    return (int(time.time()) // INTERVAL) * INTERVAL


async def fetch_poly_market(session: aiohttp.ClientSession, asset: str) -> dict | None:
    """Fetch current 15m market for an asset from Polymarket Gamma API."""
    slug_ts   = current_candle_ts()
    slug      = f"{asset.lower()}-updown-15m-{slug_ts}"
    url       = f"{POLY_GAMMA_URL}/events"
    try:
        async with session.get(url, params={"slug": slug}) as r:
            if r.status != 200:
                return None
            data = await r.json()
        events = data if isinstance(data, list) else data.get("events", [])
        if not events:
            return None
        event  = events[0]
        markets = event.get("markets", [])
        if not markets:
            return None
        m = markets[0]

        # Both fields are stored as JSON strings inside the JSON response
        token_ids = json.loads(m.get("clobTokenIds", "[]"))
        outcomes  = m.get("outcomes", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        if len(token_ids) < 2:
            return None

        # Determine which token index is Up vs Down
        up_idx = next(
            (i for i, o in enumerate(outcomes) if str(o).lower() == "up"), 0
        )
        dn_idx = 1 - up_idx

        return {
            "condition_id": m.get("conditionId") or m.get("condition_id"),
            "up_token":     token_ids[up_idx],
            "down_token":   token_ids[dn_idx],
            "question":     event.get("title", ""),
            "slug":         slug,
        }
    except Exception as e:
        print(f"[mapper] poly {asset} error: {e}", flush=True)
        return None


async def fetch_kalshi_ticker(kalshi: KalshiClient, asset: str,
                              min_open_ts: float = None) -> dict | None:
    """Fetch current open Kalshi ticker for an asset. Retries until new candle appears."""
    series = KALSHI_SERIES[asset]
    for _ in range(20):
        try:
            data = await kalshi.get("/markets", params={
                "status": "open",
                "series_ticker": series,
                "limit": 1,
            })
            markets = data.get("markets", [])
            if not markets:
                await asyncio.sleep(10)
                continue
            m = markets[0]
            if min_open_ts:
                ot_str = m.get("open_time", "")
                try:
                    ot = datetime.fromisoformat(
                        ot_str.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    ot = 0
                if ot < min_open_ts:
                    print(f"[mapper] waiting for new {asset} Kalshi market (got {m['ticker']})...",
                          flush=True)
                    await asyncio.sleep(10)
                    continue
            return {
                "ticker":   m["ticker"],
                "yes_is":   "up",   # KXBTC15M yes = Up (BTC went up)
            }
        except Exception as e:
            print(f"[mapper] kalshi {asset} error: {e}", flush=True)
            await asyncio.sleep(5)
    return None


async def build_map(session: aiohttp.ClientSession, kalshi: KalshiClient,
                    min_open_ts: float = None) -> list:
    """Build market map for the current candle. Returns list of pair dicts."""
    pairs = []

    results = await asyncio.gather(
        *[fetch_poly_market(session, asset) for asset in ASSETS],
        *[fetch_kalshi_ticker(kalshi, asset, min_open_ts) for asset in ASSETS],
        return_exceptions=True,
    )

    poly_results   = results[:len(ASSETS)]
    kalshi_results = results[len(ASSETS):]

    for i, asset in enumerate(ASSETS):
        pm = poly_results[i]
        km = kalshi_results[i]
        if isinstance(pm, Exception) or isinstance(km, Exception):
            print(f"[mapper] {asset} skipped (error)", flush=True)
            continue
        if not pm or not km:
            print(f"[mapper] {asset} skipped (no market found)", flush=True)
            continue
        pairs.append({
            "symbol":          asset,
            "timeframe":       "15m",
            "candle_ts":       current_candle_ts(),
            "poly_condition":  pm["condition_id"],
            "poly_up_token":   pm["up_token"],
            "poly_down_token": pm["down_token"],
            "kalshi_ticker":   km["ticker"],
            "kalshi_yes_is":   km["yes_is"],
        })
        print(f"[mapper] {asset}: poly={pm['condition_id'][:10]}... "
              f"kalshi={km['ticker']}", flush=True)

    return pairs
