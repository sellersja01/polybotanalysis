"""
Polymarket CLOB client — persistent session, REST + WebSocket
"""
import json
import aiohttp
import websockets

from config import POLY_CLOB_URL, POLY_WS_URL


class PolymarketClient:
    """
    High-speed Polymarket client.
    Accepts an external aiohttp.ClientSession for connection reuse.
    """

    def __init__(self, session: aiohttp.ClientSession = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
                timeout=aiohttp.ClientTimeout(total=5, connect=2),
            )
            self._owns_session = True
        return self._session

    async def get_markets(self, next_cursor: str = "") -> dict:
        s = await self._get_session()
        params = {"limit": 500}
        if next_cursor:
            params["next_cursor"] = next_cursor
        async with s.get(f"{POLY_CLOB_URL}/markets", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def get_market(self, condition_id: str) -> dict:
        s = await self._get_session()
        async with s.get(f"{POLY_CLOB_URL}/markets/{condition_id}") as r:
            r.raise_for_status()
            return await r.json()

    async def place_order(self, token_id: str, price: float, size: int,
                          side: str = "BUY") -> dict:
        """
        Place a market order on Polymarket CLOB.
        NOTE: Real implementation requires EIP-712 signing with a wallet key.
        This is a placeholder — will be wired up when connecting real accounts.
        """
        s = await self._get_session()
        body = {
            "tokenID":  token_id,
            "price":    str(price),
            "size":     size,
            "side":     side,
            "feeRateBps": 0,
            "nonce":    0,
            "taker":    "0x0000000000000000000000000000000000000000",
            "maker":    "0x0000000000000000000000000000000000000000",
            "expiration": "0",
            "signatureType": 0,
            "signature": "0x",
        }
        async with s.post(f"{POLY_CLOB_URL}/order", json=body) as r:
            return await r.json()

    async def subscribe_prices(self, asset_ids: list, callback):
        async with websockets.connect(POLY_WS_URL) as ws:
            await ws.send(json.dumps({
                "assets_ids": asset_ids,
                "type": "market",
            }))
            async for raw in ws:
                msg = json.loads(raw)
                if isinstance(msg, list):
                    for item in msg:
                        await callback(item)
                elif isinstance(msg, dict):
                    await callback(msg)

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
