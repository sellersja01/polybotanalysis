"""
Polymarket CLOB client — REST + WebSocket price feed
"""
import asyncio
import json
import aiohttp
import websockets

CLOB_REST = "https://clob.polymarket.com"
CLOB_WS   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketClient:
    async def get_markets(self, next_cursor: str = "") -> dict:
        url = f"{CLOB_REST}/markets"
        params = {"limit": 500}
        if next_cursor:
            params["next_cursor"] = next_cursor
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params) as r:
                r.raise_for_status()
                return await r.json()

    async def get_market(self, condition_id: str) -> dict:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{CLOB_REST}/markets/{condition_id}") as r:
                r.raise_for_status()
                return await r.json()

    async def subscribe_prices(self, asset_ids: list, callback):
        """
        Stream real-time best bid/ask for a list of token (asset) IDs.
        asset_ids: list of Polymarket token IDs (Up or Down token)
        callback: async fn(asset_id, best_bid, best_ask)
        """
        async with websockets.connect(CLOB_WS) as ws:
            await ws.send(json.dumps({
                "assets_ids": asset_ids,
                "type":       "market",
            }))
            async for raw in ws:
                msg = json.loads(raw)
                # Polymarket sends list of price updates
                if isinstance(msg, list):
                    for item in msg:
                        await callback(item)
                elif isinstance(msg, dict):
                    await callback(msg)
