"""
Kalshi API client — auth + REST + WebSocket
"""
import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path

import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


KALSHI_WS_URL  = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"


def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(private_key, method: str, path: str, ts_ms: int) -> str:
    msg = f"{ts_ms}{method}{path}".encode()
    sig = private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def auth_headers(key_id: str, private_key, method: str, path: str) -> dict:
    ts = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, method, path, ts),
        "Content-Type": "application/json",
    }


class KalshiClient:
    def __init__(self, key_id: str, key_path: str):
        self.key_id      = key_id
        self.private_key = load_private_key(key_path)

    def _headers(self, method: str, path: str) -> dict:
        return auth_headers(self.key_id, self.private_key, method, path)

    async def get(self, path: str, params: dict = None) -> dict:
        url = KALSHI_API_URL + path
        headers = self._headers("GET", path)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, params=params) as r:
                r.raise_for_status()
                return await r.json()

    async def post(self, path: str, body: dict) -> dict:
        url = KALSHI_API_URL + path
        headers = self._headers("POST", path)
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json=body) as r:
                r.raise_for_status()
                return await r.json()

    async def get_markets(self, series_ticker: str = None, status: str = "open") -> list:
        """Fetch open markets, optionally filtered by series (e.g. 'KXBTC')."""
        params = {"status": status, "limit": 200}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self.get("/markets", params)
        return data.get("markets", [])

    async def place_order(self, ticker: str, side: str, price_cents: int,
                          count: int, post_only: bool = True) -> dict:
        """
        side: 'yes' or 'no'
        price_cents: integer 1-99 (cents)
        post_only: True = maker (0% fee), False = taker
        """
        body = {
            "ticker":       ticker,
            "side":         side,
            "action":       "buy",
            "count":        count,
            "yes_price":    price_cents if side == "yes" else 100 - price_cents,
            "time_in_force": "immediate_or_cancel",
            "post_only":    post_only,
        }
        return await self.post("/portfolio/orders", body)

    async def subscribe_ticker(self, tickers: list, callback):
        """Stream real-time bid/ask for a list of market tickers."""
        path   = "/trade-api/ws/v2"
        ts     = int(time.time() * 1000)
        sig    = sign(self.private_key, "GET", path, ts)
        headers = {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sig,
        }
        async with websockets.connect(KALSHI_WS_URL, extra_headers=headers) as ws:
            # Subscribe to ticker channel for each market
            for i, ticker in enumerate(tickers):
                await ws.send(json.dumps({
                    "id":  i + 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels":      ["ticker"],
                        "market_ticker": ticker,
                    }
                }))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "ticker":
                    await callback(msg)
