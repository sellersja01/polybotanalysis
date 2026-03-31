"""
Kalshi API client — persistent session, RSA-PSS auth, WebSocket
"""
import base64
import json
import time

import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from config import KALSHI_API_URL, KALSHI_WS_URL


def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_pss(private_key, method: str, path: str, ts_ms: int) -> str:
    """RSA-PSS with SHA-256 — required by Kalshi. Path must include /trade-api/v2 prefix."""
    full_path = "/trade-api/v2" + path if not path.startswith("/trade-api") else path
    msg = f"{ts_ms}{method}{full_path}".encode()
    sig = private_key.sign(
        msg,
        asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


class KalshiClient:
    """
    High-speed Kalshi client.
    Accepts an external aiohttp.ClientSession for connection reuse.
    """

    def __init__(self, key_id: str, key_path: str, session: aiohttp.ClientSession = None):
        self.key_id      = key_id
        self.private_key = load_private_key(key_path)
        self._session    = session
        self._owns_session = session is None

        # Pre-encode the order path (used on every trade)
        self._order_path = "/portfolio/orders"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
                timeout=aiohttp.ClientTimeout(total=5, connect=2),
            )
            self._owns_session = True
        return self._session

    def _headers(self, method: str, path: str) -> dict:
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign_pss(self.private_key, method, path, ts),
            "Content-Type": "application/json",
        }

    async def get(self, path: str, params: dict = None) -> dict:
        s = await self._get_session()
        async with s.get(KALSHI_API_URL + path,
                         headers=self._headers("GET", path),
                         params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def post(self, path: str, body: dict) -> dict:
        s = await self._get_session()
        async with s.post(KALSHI_API_URL + path,
                          headers=self._headers("POST", path),
                          json=body) as r:
            if r.status >= 400:
                text = await r.text()
                raise RuntimeError(f"Kalshi {r.status} on POST {path}: {text[:500]}")
            return await r.json()

    async def get_markets(self, series_ticker: str = None, status: str = "open") -> list:
        params = {"status": status, "limit": 200}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self.get("/markets", params)
        return data.get("markets", [])

    async def place_order(self, ticker: str, side: str, price_cents: int,
                          count: float, post_only: bool = True) -> dict:
        body = {
            "ticker":    ticker,
            "side":      side,
            "action":    "buy",
            "count":     int(round(float(count))),
            "yes_price": price_cents if side == "yes" else 100 - price_cents,
        }
        return await self.post(self._order_path, body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self.post(f"/portfolio/orders/{order_id}/cancel", {})

    def ws_headers(self) -> dict:
        """Auth headers for WebSocket connection."""
        path = "/trade-api/ws/v2"
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY":       self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sign_pss(self.private_key, "GET", path, ts),
        }

    async def subscribe_ticker(self, tickers: list, callback):
        headers = self.ws_headers()
        async with websockets.connect(KALSHI_WS_URL,
                                      additional_headers=headers) as ws:
            for i, ticker in enumerate(tickers):
                await ws.send(json.dumps({
                    "id": i + 1, "cmd": "subscribe",
                    "params": {"channels": ["ticker"], "market_ticker": ticker},
                }))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "ticker":
                    await callback(msg)

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
