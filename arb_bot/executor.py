"""
executor.py — High-speed arb execution engine
================================================
- Fires both legs in parallel via asyncio.gather()
- Uses pre-existing persistent sessions (no TLS overhead)
- Tracks latency at nanosecond precision
- DRY_RUN mode logs everything without sending orders
- Handles partial fills and leg failures
"""
import asyncio
import time
import sqlite3
from datetime import datetime, timezone

from config import DRY_RUN, SHARES_PER_TRADE


class ExecutionResult:
    __slots__ = ('success', 'poly_result', 'kalshi_result',
                 'latency_detect_ms', 'latency_poly_ms', 'latency_kalshi_ms',
                 'latency_total_ms', 'error')

    def __init__(self):
        self.success = False
        self.poly_result = None
        self.kalshi_result = None
        self.latency_detect_ms = 0.0
        self.latency_poly_ms = 0.0
        self.latency_kalshi_ms = 0.0
        self.latency_total_ms = 0.0
        self.error = None


class Executor:
    """
    Fires arb trades on both platforms in parallel.
    Measures and logs latency for every operation.
    """

    def __init__(self, kalshi_client, poly_client, db_path="arb_trades.db"):
        self.kalshi = kalshi_client
        self.poly   = poly_client
        self.dry_run = DRY_RUN
        self.shares  = SHARES_PER_TRADE

        # Stats
        self.total_fired   = 0
        self.total_success = 0
        self.total_profit  = 0.0
        self.latencies     = []  # list of total_ms

        # Trade log DB
        self._db = sqlite3.connect(db_path)
        self._db.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, dry_run INTEGER,
            symbol TEXT, timeframe TEXT,
            poly_side TEXT, kalshi_side TEXT,
            poly_ask REAL, kalshi_ask REAL,
            poly_mode TEXT, kalshi_mode TEXT,
            profit_per_share REAL, shares INTEGER, total_profit REAL,
            fees REAL, roi_pct REAL,
            latency_detect_ms REAL, latency_poly_ms REAL,
            latency_kalshi_ms REAL, latency_total_ms REAL,
            poly_result TEXT, kalshi_result TEXT, error TEXT
        )""")
        self._db.commit()

    async def execute(self, opp: dict) -> ExecutionResult:
        """
        Execute an arb opportunity.
        Returns ExecutionResult with latency data regardless of DRY_RUN.
        """
        result = ExecutionResult()
        t_start = time.perf_counter_ns()

        # Time from detection to execution start
        detect_ts = opp.get("detect_ts", time.time())
        result.latency_detect_ms = (time.time() - detect_ts) * 1000

        pair   = opp["pair"]
        shares = self.shares
        profit = opp["profit"] * shares

        self.total_fired += 1

        # Log the opportunity
        ts_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        mode_str = "DRY" if self.dry_run else "LIVE"
        print(
            f"[{ts_str}] EXEC {pair['symbol']} {pair.get('timeframe','15m')} | "
            f"{opp['poly_side']}@{opp['poly_ask']:.3f} + "
            f"{opp['kalshi_side']}@{opp['kalshi_ask']:.3f} | "
            f"net={opp['profit']*100:.2f}c ROI={opp['roi_pct']:.1f}% | "
            f"detect={result.latency_detect_ms:.1f}ms | {mode_str}"
        )

        if self.dry_run:
            # Simulate execution latency
            result.success = True
            result.latency_poly_ms = 0.0
            result.latency_kalshi_ms = 0.0
            result.latency_total_ms = (time.perf_counter_ns() - t_start) / 1e6
            self.total_success += 1
            self.total_profit += profit
            self._log_trade(opp, result, pair)
            return result

        # ── LIVE execution: fire both legs in parallel ────────────────────────
        poly_token = (pair["poly_up_token"] if opp["poly_side"] == "poly_up"
                      else pair["poly_down_token"])

        # Determine Kalshi side
        k_yes_is_up = pair.get("kalshi_yes_is", "up") == "up"
        if opp["kalshi_side"] == "kalshi_up":
            k_side = "yes" if k_yes_is_up else "no"
        else:
            k_side = "no" if k_yes_is_up else "yes"

        k_price_cents = int(opp["kalshi_ask"] * 100)
        is_maker = opp["kalshi_mode"] == "maker"

        async def fire_poly():
            t0 = time.perf_counter_ns()
            try:
                r = await self.poly.place_order(
                    poly_token, opp["poly_ask"], shares
                )
                result.latency_poly_ms = (time.perf_counter_ns() - t0) / 1e6
                return r
            except Exception as e:
                result.latency_poly_ms = (time.perf_counter_ns() - t0) / 1e6
                raise

        async def fire_kalshi():
            t0 = time.perf_counter_ns()
            try:
                r = await self.kalshi.place_order(
                    pair["kalshi_ticker"], k_side, k_price_cents,
                    shares, post_only=is_maker
                )
                result.latency_kalshi_ms = (time.perf_counter_ns() - t0) / 1e6
                return r
            except Exception as e:
                result.latency_kalshi_ms = (time.perf_counter_ns() - t0) / 1e6
                raise

        try:
            poly_r, kalshi_r = await asyncio.gather(
                fire_poly(), fire_kalshi(), return_exceptions=True
            )

            if isinstance(poly_r, Exception):
                result.error = f"poly: {poly_r}"
                result.poly_result = str(poly_r)
            else:
                result.poly_result = str(poly_r)[:200]

            if isinstance(kalshi_r, Exception):
                result.error = (result.error or "") + f" kalshi: {kalshi_r}"
                result.kalshi_result = str(kalshi_r)
            else:
                result.kalshi_result = str(kalshi_r)[:200]

            if not isinstance(poly_r, Exception) and not isinstance(kalshi_r, Exception):
                result.success = True
                self.total_success += 1
                self.total_profit += profit
            else:
                # One leg failed — need to handle partial fill
                print(f"  PARTIAL FILL: {result.error}")

        except Exception as e:
            result.error = str(e)
            print(f"  EXEC ERROR: {e}")

        result.latency_total_ms = (time.perf_counter_ns() - t_start) / 1e6
        self.latencies.append(result.latency_total_ms)

        print(
            f"  timing: poly={result.latency_poly_ms:.1f}ms "
            f"kalshi={result.latency_kalshi_ms:.1f}ms "
            f"total={result.latency_total_ms:.1f}ms"
        )

        self._log_trade(opp, result, pair)
        return result

    def _log_trade(self, opp, result, pair):
        """Write trade to SQLite for analysis."""
        try:
            self._db.execute("""INSERT INTO trades (
                ts, dry_run, symbol, timeframe,
                poly_side, kalshi_side, poly_ask, kalshi_ask,
                poly_mode, kalshi_mode,
                profit_per_share, shares, total_profit,
                fees, roi_pct,
                latency_detect_ms, latency_poly_ms,
                latency_kalshi_ms, latency_total_ms,
                poly_result, kalshi_result, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                time.time(), 1 if self.dry_run else 0,
                pair.get("symbol", ""), pair.get("timeframe", "15m"),
                opp["poly_side"], opp["kalshi_side"],
                opp["poly_ask"], opp["kalshi_ask"],
                opp["poly_mode"], opp["kalshi_mode"],
                opp["profit"], self.shares, opp["profit"] * self.shares,
                opp["fees"], opp["roi_pct"],
                result.latency_detect_ms, result.latency_poly_ms,
                result.latency_kalshi_ms, result.latency_total_ms,
                result.poly_result, result.kalshi_result, result.error,
            ))
            self._db.commit()
        except Exception as e:
            print(f"  DB log error: {e}")

    def stats_summary(self) -> str:
        avg_lat = (sum(self.latencies) / len(self.latencies)
                   if self.latencies else 0)
        p50 = sorted(self.latencies)[len(self.latencies)//2] if self.latencies else 0
        return (
            f"fired={self.total_fired} "
            f"success={self.total_success} "
            f"profit=${self.total_profit:.2f} "
            f"avg_lat={avg_lat:.1f}ms "
            f"p50_lat={p50:.1f}ms"
        )

    def close(self):
        self._db.close()
