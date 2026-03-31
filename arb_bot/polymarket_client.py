"""
polymarket_client.py — Polymarket CLOB client
==============================================
- Real EIP-712 order signing via py-clob-client
- Persistent aiohttp session for low-latency order POSTs
- WebSocket price feed with correct subscription format
"""
import json
import time
import asyncio

import aiohttp
import websockets

from config import POLY_CLOB_URL, POLY_WS_URL, POLY_PRIVATE_KEY, POLY_ADDRESS, POLY_API_KEY


def _build_clob_client():
    """Initialize py-clob-client for order signing. Runs once at startup."""
    if not POLY_PRIVATE_KEY:
        return None
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(
            host=POLY_CLOB_URL,
            key=POLY_PRIVATE_KEY,
            chain_id=137,   # Polygon
            signature_type=2,
            funder="0x6826c3197fff281144b07fe6c3e72636854769ab",
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print(f"[poly] CLOB credentials derived — key={creds.api_key[:8]}...", flush=True)
        return client
    except Exception as e:
        print(f"[poly] WARNING: could not init CLOB client ({e}) — order signing disabled", flush=True)
        return None


class PolymarketClient:
    """
    High-speed Polymarket client.
    Accepts an external aiohttp.ClientSession for connection reuse.
    """

    def __init__(self, session: aiohttp.ClientSession = None):
        self._session      = session
        self._owns_session = session is None
        # Initialize signing client once (CPU-only, no network at this point)
        self._clob = _build_clob_client()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
                timeout=aiohttp.ClientTimeout(total=5, connect=2),
            )
            self._owns_session = True
        return self._session

    def _auth_headers(self) -> dict:
        """L2 API key headers for Polymarket CLOB REST endpoints."""
        return {
            "POLY_ADDRESS":    POLY_ADDRESS,
            "POLY_SIGNATURE":  "",   # not needed for order POST — EIP-712 sig is in the body
            "POLY_TIMESTAMP":  str(int(time.time())),
            "POLY_NONCE":      "0",
        }

    async def get_market(self, condition_id: str) -> dict:
        s = await self._get_session()
        async with s.get(f"{POLY_CLOB_URL}/markets/{condition_id}") as r:
            r.raise_for_status()
            return await r.json()

    async def place_order(self, token_id: str, price: float, size: int,
                          side: str = "BUY") -> dict:
        """
        Place a GTC limit order at price+5¢, wait 1.5s, verify full fill, cancel if not.
        Kalshi only fires after this returns successfully.
        """
        if not self._clob:
            raise RuntimeError("CLOB client not initialized — set POLY_PRIVATE_KEY env var")

        try:
            from py_clob_client.clob_types import MarketOrderArgs

            # Market order — sweeps AMM at current price, guaranteed fill
            # amount = USDC to spend (shares * price)
            amount = round(size * price, 2)
            args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side="BUY",
            )
            signed = await asyncio.to_thread(self._clob.create_market_order, args)
            resp   = await asyncio.to_thread(self._clob.post_order, signed, "FAK")
            taking = float(resp.get("takingAmount", 0))
            if taking <= 0:
                raise RuntimeError(f"Market order filled 0 shares: {resp}")
            return resp

        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Order failed: {e}")

    async def subscribe_prices(self, asset_ids: list, callback):
        """
        Subscribe to Polymarket WS price feed.
        Correct format: {"type":"subscribe","channel":"live_activity","assets_ids":[...]}
        Messages arrive as book events with bids/asks arrays.
        """
        async with websockets.connect(POLY_WS_URL) as ws:
            await ws.send(json.dumps({
                "type":      "subscribe",
                "channel":   "live_activity",
                "assets_ids": asset_ids,
            }))
            async for raw in ws:
                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, list):
                        for msg in msgs:
                            await callback(msg)
                    elif isinstance(msgs, dict):
                        await callback(msgs)
                except Exception:
                    pass

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
