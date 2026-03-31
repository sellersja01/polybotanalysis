"""
binance_feed.py — Real-time BTC price from Binance WebSocket
==============================================================
Public feed, no auth needed. Sub-50ms latency.
Tracks price + calculates rolling moves for edge detection.
"""
import asyncio
import json
import time
import websockets
import numpy as np
from collections import deque


BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_WS_BACKUP = "wss://stream.binance.us:9443/ws/btcusdt@trade"


class BinanceFeed:
    """
    Streams real-time BTC/USDT trades from Binance.
    Maintains a rolling price buffer for move detection.
    """

    def __init__(self, on_move=None, lookback_seconds=15, move_threshold_pct=0.05):
        self.price = 0.0
        self.last_update = 0.0
        self.on_move = on_move  # async callback(direction, move_pct, price, timestamp)

        self.lookback = lookback_seconds
        self.threshold = move_threshold_pct

        # Rolling buffer: (timestamp, price)
        self._buffer = deque(maxlen=5000)

        # Stats
        self.total_ticks = 0
        self.total_moves = 0

    def _detect_move(self, now: float, price: float):
        """Check if price moved >= threshold vs lookback_seconds ago."""
        if not self._buffer:
            return None, 0.0

        # Find price from lookback_seconds ago
        cutoff = now - self.lookback
        old_price = None
        for ts, p in self._buffer:
            if ts <= cutoff:
                old_price = p
            else:
                break

        if old_price is None or old_price == 0:
            return None, 0.0

        move_pct = (price - old_price) / old_price * 100

        if abs(move_pct) >= self.threshold:
            direction = "up" if move_pct > 0 else "down"
            return direction, move_pct

        return None, 0.0

    async def run(self):
        """Connect and stream forever with auto-reconnect."""
        while True:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                    print("[Binance] Connected to BTC/USDT trade stream")
                    async for raw in ws:
                        msg = json.loads(raw)
                        price = float(msg.get("p", 0))
                        ts = msg.get("T", 0) / 1000  # Binance sends ms

                        if price <= 0:
                            continue

                        self.price = price
                        self.last_update = time.time()
                        self.total_ticks += 1
                        self._buffer.append((time.time(), price))

                        # Detect significant moves
                        direction, move_pct = self._detect_move(time.time(), price)
                        if direction and self.on_move:
                            self.total_moves += 1
                            await self.on_move(direction, move_pct, price, time.time())

            except Exception as e:
                print(f"[Binance] WS error: {e} — reconnecting in 2s")
                await asyncio.sleep(2)

    def is_stale(self, max_age_s=5.0) -> bool:
        return (time.time() - self.last_update) > max_age_s if self.last_update > 0 else True
