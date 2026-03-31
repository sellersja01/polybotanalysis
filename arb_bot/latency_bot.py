"""
latency_bot.py — Latency arbitrage bot: Binance → Polymarket
==============================================================
Monitors Binance BTC price in real-time. When BTC moves and Polymarket
odds haven't repriced yet, buys the correct side at stale odds.

Usage:
    export KALSHI_KEY_ID=...      (not needed for latency arb but kept for compat)
    export KALSHI_KEY_PATH=...
    export DRY_RUN=true
    python latency_bot.py
"""
import asyncio
import json
import os
import time
import sqlite3
from datetime import datetime, timezone

import aiohttp

from config import DRY_RUN, SHARES_PER_TRADE, POLY_CLOB_URL, POLY_WS_URL
from config import poly_taker_fee
from polymarket_client import PolymarketClient
from binance_feed import BinanceFeed
from latency_detector import LatencyDetector


# ── Config ────────────────────────────────────────────────────────────────────
MIN_EDGE_PCT = float(os.environ.get("MIN_EDGE_PCT", "3.0"))  # min edge in pp
LOOKBACK_S   = int(os.environ.get("LOOKBACK_S", "15"))        # BTC move lookback
MOVE_THRESH  = float(os.environ.get("MOVE_THRESH", "0.05"))   # min BTC move %
RISK_MAX_PCT = float(os.environ.get("RISK_MAX_PCT", "8.0"))   # max % of portfolio per trade
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "0.20"))  # -20%
KILL_SWITCH_PCT  = float(os.environ.get("KILL_SWITCH_PCT", "0.40"))   # -40%


class LatencyBot:
    """Main bot that wires everything together."""

    def __init__(self):
        self.dry_run = DRY_RUN
        self.shares = SHARES_PER_TRADE

        # Components
        self.binance = BinanceFeed(
            lookback_seconds=LOOKBACK_S,
            move_threshold_pct=MOVE_THRESH,
        )
        self.detector = LatencyDetector(
            on_signal=self.on_signal,
            min_edge_pct=MIN_EDGE_PCT,
        )
        self.poly_client = None
        self.poly_session = None

        # Wire binance moves to detector
        self.binance.on_move = self.detector.on_binance_move

        # Stats
        self.total_signals = 0
        self.total_trades = 0
        self.total_profit = 0.0
        self.daily_start_balance = 0.0
        self.killed = False
        self.latencies = []

        # Trade log
        self._db = sqlite3.connect("latency_trades.db")
        self._db.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, dry_run INTEGER,
            direction TEXT, trade_side TEXT,
            btc_move_pct REAL, btc_price REAL,
            poly_mid REAL, poly_ask REAL,
            implied_edge REAL, fee REAL,
            profit_if_hold REAL, profit_if_exit REAL,
            poly_age_ms REAL,
            latency_detect_ms REAL, latency_exec_ms REAL,
            condition_id TEXT, question TEXT,
            result TEXT, error TEXT
        )""")
        self._db.commit()

    async def on_signal(self, signal: dict):
        """Called when detector finds a latency arb opportunity."""
        if self.killed:
            return

        self.total_signals += 1
        t_start = time.perf_counter_ns()

        detect_latency = (time.time() - signal["detect_ts"]) * 1000

        ts_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        mode = "DRY" if self.dry_run else "LIVE"

        print(
            f"[{ts_str}] SIGNAL {signal['direction'].upper()} | "
            f"BTC {signal['btc_move_pct']:+.3f}% @ ${signal['btc_price']:,.0f} | "
            f"Poly {signal['trade_side']} mid={signal['poly_mid']:.3f} ask={signal['poly_ask']:.3f} | "
            f"edge={signal['implied_edge']*100:.1f}pp | "
            f"hold=${signal['profit_if_hold']*100:.1f} exit=${signal['profit_if_exit']*100:.1f} | "
            f"lag={signal['poly_age_ms']:.0f}ms | {mode}"
        )

        exec_latency = 0.0
        result = "dry_run"
        error = None

        if not self.dry_run:
            # Execute: buy the correct side on Polymarket
            t_exec = time.perf_counter_ns()
            try:
                r = await self.poly_client.place_order(
                    signal["token_id"],
                    signal["poly_ask"],
                    self.shares,
                )
                exec_latency = (time.perf_counter_ns() - t_exec) / 1e6
                result = str(r)[:200]
                print(f"  EXEC: {exec_latency:.1f}ms | {result}")
            except Exception as e:
                exec_latency = (time.perf_counter_ns() - t_exec) / 1e6
                error = str(e)
                result = "error"
                print(f"  ERROR: {exec_latency:.1f}ms | {e}")

        total_latency = (time.perf_counter_ns() - t_start) / 1e6
        self.latencies.append(total_latency)

        # Track PnL (simulated for DRY_RUN)
        sim_profit = signal["profit_if_exit"] * self.shares
        self.total_profit += sim_profit
        self.total_trades += 1

        # Risk checks
        # (simplified — in live mode would track actual balance)

        # Log to DB
        try:
            self._db.execute("""INSERT INTO trades (
                ts, dry_run, direction, trade_side,
                btc_move_pct, btc_price, poly_mid, poly_ask,
                implied_edge, fee, profit_if_hold, profit_if_exit,
                poly_age_ms, latency_detect_ms, latency_exec_ms,
                condition_id, question, result, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                time.time(), 1 if self.dry_run else 0,
                signal["direction"], signal["trade_side"],
                signal["btc_move_pct"], signal["btc_price"],
                signal["poly_mid"], signal["poly_ask"],
                signal["implied_edge"], signal["fee"],
                signal["profit_if_hold"], signal["profit_if_exit"],
                signal["poly_age_ms"],
                detect_latency, exec_latency,
                signal["condition_id"], signal["question"],
                result, error,
            ))
            self._db.commit()
        except Exception as e:
            print(f"  DB error: {e}")

    async def run_poly_feed(self):
        """Stream Polymarket Up/Down odds for the current candle."""
        # For now, subscribe to BTC 5m markets
        # In production, this would auto-roll to the current candle
        print("[Poly] Connecting to price feed...")

        # We need the current market's token IDs
        # This would come from market_mapper in production
        # For now, accept them via env or market_map.json
        if os.path.exists("market_map.json"):
            with open("market_map.json") as f:
                market_map = json.load(f)
            # Find BTC 15m or 5m pairs
            btc_pairs = [p for p in market_map
                         if p.get("symbol") == "BTC"]
            if btc_pairs:
                pair = btc_pairs[0]
                self.detector.set_market(
                    pair["poly_condition"],
                    pair["poly_up_token"],
                    pair["poly_down_token"],
                    pair.get("poly_question", ""),
                )
                asset_ids = [pair["poly_up_token"], pair["poly_down_token"]]
            else:
                print("[Poly] No BTC pairs in market_map.json")
                return
        else:
            print("[Poly] No market_map.json — run market_mapper.py first")
            return

        token_sides = {
            asset_ids[0]: "up",
            asset_ids[1]: "down",
        }

        price_buf = {}

        async def handle(msg):
            asset_id = msg.get("asset_id")
            if asset_id not in token_sides:
                return
            side = token_sides[asset_id]
            bid = float(msg.get("best_bid") or 0)
            ask = float(msg.get("best_ask") or 0)
            mid = (bid + ask) / 2 if bid and ask else 0

            price_buf[f"{side}_mid"] = mid
            price_buf[f"{side}_ask"] = ask

            if all(k in price_buf for k in ("up_mid", "up_ask", "down_mid", "down_ask")):
                self.detector.update_poly(
                    price_buf["up_mid"], price_buf["up_ask"],
                    price_buf["down_mid"], price_buf["down_ask"],
                )

        while True:
            try:
                await self.poly_client.subscribe_prices(asset_ids, handle)
            except Exception as e:
                print(f"[Poly] WS error: {e} — reconnecting in 2s")
                await asyncio.sleep(2)

    async def stats_printer(self):
        """Print stats every 30 seconds."""
        while True:
            await asyncio.sleep(30)
            avg_lat = np.mean(self.latencies) if self.latencies else 0
            print(
                f"[stats] signals={self.total_signals} "
                f"trades={self.total_trades} "
                f"sim_profit=${self.total_profit:.2f} "
                f"btc=${self.binance.price:,.0f} "
                f"btc_ticks={self.binance.total_ticks:,} "
                f"moves={self.binance.total_moves} "
                f"avg_lat={avg_lat:.1f}ms "
                f"{'DRY' if self.dry_run else 'LIVE'}"
            )

    async def run(self):
        """Main entry point."""
        print(f"{'='*60}")
        print(f"  LATENCY ARB BOT — {'DRY RUN' if self.dry_run else '*** LIVE ***'}")
        print(f"  Shares/trade: {self.shares}")
        print(f"  Min edge: {MIN_EDGE_PCT}pp | Lookback: {LOOKBACK_S}s | Move thresh: {MOVE_THRESH}%")
        print(f"{'='*60}\n")

        # Create persistent session
        connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=60)
        self.poly_session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=5, connect=2),
        )
        self.poly_client = PolymarketClient(session=self.poly_session)

        # Warmup
        print("Warming up Polymarket connection...")
        try:
            async with self.poly_session.get(f"{POLY_CLOB_URL}/time") as r:
                await r.read()
                print(f"  Polymarket: warm")
        except Exception as e:
            print(f"  Polymarket warmup failed: {e}")

        print("Starting feeds...\n")

        try:
            await asyncio.gather(
                self.binance.run(),
                self.run_poly_feed(),
                self.stats_printer(),
            )
        finally:
            self._db.close()
            await self.poly_session.close()


# Need numpy for stats_printer
import numpy as np  # noqa: E402

if __name__ == "__main__":
    bot = LatencyBot()
    asyncio.run(bot.run())
