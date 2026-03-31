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

from config import DRY_RUN, SHARES_PER_TRADE, MAX_TRADES_PER_CANDLE, CANDLE_INTERVAL, TRADE_WINDOW


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

        # Per-candle trade counter: asset -> (candle_ts, trade_count)
        self._candle_trades = {}

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

    def _candle_ts(self) -> int:
        return (int(time.time()) // TRADE_WINDOW) * TRADE_WINDOW

    def _check_candle_limit(self, symbol: str) -> bool:
        """Returns True if we're allowed to trade this candle."""
        candle_ts = self._candle_ts()
        prev_ts, count = self._candle_trades.get(symbol, (0, 0))
        if prev_ts != candle_ts:
            # New candle — reset counter
            self._candle_trades[symbol] = (candle_ts, 0)
            count = 0
        return count < MAX_TRADES_PER_CANDLE

    def _increment_candle_count(self, symbol: str):
        candle_ts = self._candle_ts()
        _, count = self._candle_trades.get(symbol, (0, 0))
        self._candle_trades[symbol] = (candle_ts, count + 1)

    async def execute(self, opp: dict) -> ExecutionResult:
        """
        Execute an arb opportunity.
        Returns ExecutionResult with latency data regardless of DRY_RUN.
        """
        result = ExecutionResult()
        t_start = time.perf_counter_ns()

        pair   = opp["pair"]
        symbol = pair.get("symbol", "?")

        # Check per-candle trade limit
        if not self._check_candle_limit(symbol):
            return result  # silently skip — already traded this candle

        # ref_price_gap check disabled — re-enable once validated
        # ref_gap = pair.get("ref_price_gap")
        # if ref_gap is not None and ref_gap > 6.0:
        #     return result

        # Time from detection to execution start
        detect_ts = opp.get("detect_ts", time.time())
        result.latency_detect_ms = (time.time() - detect_ts) * 1000

        shares = self.shares
        profit = opp["profit"] * shares

        self.total_fired += 1
        self._increment_candle_count(symbol)

        # Determine sides cleanly for display
        poly_side_label   = "UP  " if "up"   in opp["poly_side"]   else "DOWN"
        kalshi_side_label = "UP  " if "up"   in opp["kalshi_side"] else "DOWN"
        mode_str = "DRY RUN" if self.dry_run else "LIVE"
        ts_str   = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        print(f"\n{'='*55}", flush=True)
        print(f"  *** TRADE FIRED — {symbol} 15m | {mode_str} ***", flush=True)
        print(f"  Time       : {ts_str}", flush=True)
        print(f"  Polymarket : {poly_side_label}  @ {opp['poly_ask']:.4f} (${opp['poly_ask']*shares:.2f})", flush=True)
        print(f"  Kalshi     : {kalshi_side_label}  @ {opp['kalshi_ask']:.4f} (${opp['kalshi_ask']*shares:.2f})", flush=True)
        print(f"  Total cost : ${(opp['poly_ask']+opp['kalshi_ask'])*shares:.2f}", flush=True)
        print(f"  Net profit : {opp['profit']*100:.2f}¢  ROI={opp['roi_pct']:.1f}%", flush=True)
        print(f"  Detect lag : {result.latency_detect_ms:.1f}ms", flush=True)
        print(f"{'='*55}\n", flush=True)

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

        # Add 10¢ buffer so limit order crosses the spread and fills immediately
        k_price_cents = min(int(opp["kalshi_ask"] * 100) + 10, 99)
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
            # Fire Polymarket first — must fully fill before Kalshi fires
            poly_r = await fire_poly()
            if isinstance(poly_r, Exception):
                result.error = f"poly: {poly_r}"
                result.poly_result = str(poly_r)
                print(f"  POLY FAILED — skipping Kalshi to avoid unhedged position: {poly_r}")
                result.latency_total_ms = (time.perf_counter_ns() - t_start) / 1e6
                self.latencies.append(result.latency_total_ms)
                self._log_trade(opp, result, pair)
                return result

            poly_resp = poly_r if isinstance(poly_r, dict) else {}
            if not (poly_resp.get("success") or poly_resp.get("status") == "matched"):
                result.error = f"poly not filled: {str(poly_r)[:100]}"
                result.poly_result = str(poly_r)[:200]
                print(f"  POLY NOT FILLED — skipping Kalshi: {result.error}")
                result.latency_total_ms = (time.perf_counter_ns() - t_start) / 1e6
                self.latencies.append(result.latency_total_ms)
                self._log_trade(opp, result, pair)
                return result

            result.poly_result = str(poly_r)[:200]

            # Match Kalshi to Poly fill — round to 1 decimal place
            poly_filled = float(poly_resp.get("takingAmount", shares))
            kalshi_shares = max(1, round(poly_filled))  # Kalshi API requires integer
            if kalshi_shares != shares:
                print(f"  Poly filled {poly_filled:.4f} shares — Kalshi buying {kalshi_shares}")

            # Poly confirmed filled — now fire Kalshi with matched share count
            t0 = time.perf_counter_ns()
            kalshi_r = await self.kalshi.place_order(
                pair["kalshi_ticker"], k_side, k_price_cents,
                kalshi_shares, post_only=is_maker
            )
            result.latency_kalshi_ms = (time.perf_counter_ns() - t0) / 1e6

            if isinstance(kalshi_r, Exception):
                result.error = f"kalshi: {kalshi_r}"
                result.kalshi_result = str(kalshi_r)
                print(f"  KALSHI FAILED after poly filled — unhedged! {kalshi_r}")
            else:
                result.kalshi_result = str(kalshi_r)[:200]
                result.success = True
                self.total_success += 1
                self.total_profit += profit

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
