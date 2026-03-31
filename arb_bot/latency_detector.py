"""
latency_detector.py — Detects Polymarket lag behind Binance price moves
========================================================================
When Binance BTC moves but Polymarket odds haven't repriced yet,
fires an arb signal to buy the correct side at stale odds.
"""
import time

from config import poly_taker_fee, DEDUP_COOLDOWN


class LatencyDetector:
    """
    Compares live Binance BTC price moves against current Polymarket odds.
    Fires when Poly odds are stale relative to what Binance price implies.
    """

    def __init__(self, on_signal=None, min_edge_pct=3.0):
        """
        on_signal: async callback(signal_dict) fired when arb detected
        min_edge_pct: minimum implied probability edge to fire (percentage points)
        """
        self.on_signal = on_signal
        self.min_edge = min_edge_pct / 100  # convert to decimal

        # Current Polymarket state
        self.poly_up_mid = 0.0
        self.poly_up_ask = 0.0
        self.poly_dn_mid = 0.0
        self.poly_dn_ask = 0.0
        self.poly_ts = 0.0  # last poly update timestamp

        # Current market info
        self.condition_id = None
        self.up_token = None
        self.dn_token = None
        self.question = None

        # Dedup
        self._last_fire = 0.0
        self._last_direction = None

        # Stats
        self.total_checks = 0
        self.total_signals = 0

    def update_poly(self, up_mid: float, up_ask: float,
                    dn_mid: float, dn_ask: float):
        """Called on every Polymarket price update."""
        self.poly_up_mid = up_mid
        self.poly_up_ask = up_ask
        self.poly_dn_mid = dn_mid
        self.poly_dn_ask = dn_ask
        self.poly_ts = time.time()

    def set_market(self, condition_id: str, up_token: str, dn_token: str,
                   question: str = ""):
        """Set current active market (called on candle rollover)."""
        self.condition_id = condition_id
        self.up_token = up_token
        self.dn_token = dn_token
        self.question = question

    async def on_binance_move(self, direction: str, move_pct: float,
                               btc_price: float, btc_ts: float):
        """
        Called by BinanceFeed when a significant BTC move is detected.
        Checks if Polymarket is lagging and fires signal if so.
        """
        self.total_checks += 1
        now = time.time()

        # Need valid poly data
        if self.poly_up_ask <= 0 or self.poly_dn_ask <= 0:
            return
        if self.condition_id is None:
            return

        # Poly data must be reasonably fresh (within 10s)
        poly_age = now - self.poly_ts
        if poly_age > 10:
            return

        # What SHOULD Poly odds be given the BTC move?
        # If BTC went up, Up probability should be higher
        # The implied edge is how far Poly is from "correct"
        if direction == "up":
            # BTC went up → Up should win → Up mid should be high
            # If Poly Up mid is still near 0.50, there's an edge
            # Buy Up at current ask
            trade_side = "up"
            entry_price = self.poly_up_ask
            current_mid = self.poly_up_mid
        else:
            # BTC went down → Down should win → Down mid should be high
            # Buy Down at current ask
            trade_side = "down"
            entry_price = self.poly_dn_ask
            current_mid = self.poly_dn_mid

        # Edge estimate: the move implies the correct side should be worth more
        # A 0.05% BTC move in 15s typically shifts the 5m candle outcome
        # probability by roughly 5-15 percentage points
        # Conservative: use move_pct * 50 as implied probability shift
        implied_shift = abs(move_pct) * 50  # e.g., 0.1% move → 5pp shift
        implied_shift = min(implied_shift, 0.40)  # cap at 40pp

        # The edge is: what we think it should be vs what Poly is showing
        # If mid is 0.50 and it should be 0.55+, edge = 0.05+
        implied_fair = current_mid + implied_shift
        edge = implied_shift  # simplified: the full shift is our edge

        if edge < self.min_edge:
            return

        # Dedup: don't fire same direction within cooldown
        if (direction == self._last_direction and
                now - self._last_fire < DEDUP_COOLDOWN):
            return

        # Calculate expected profit
        fee = poly_taker_fee(entry_price)
        # If we hold to resolution and we're right, payout = $1.00
        # Expected profit = 1.0 - entry_price - fee (if we're right)
        # But we might exit early at repriced odds instead
        # Conservative: assume we sell after 2c reprice
        conservative_exit = current_mid + 0.02
        profit_hold = 1.0 - entry_price - fee  # hold to resolution
        profit_exit = conservative_exit - entry_price - fee  # exit after reprice

        self._last_fire = now
        self._last_direction = direction
        self.total_signals += 1

        signal = {
            "type": "latency_arb",
            "direction": direction,
            "trade_side": trade_side,
            "btc_move_pct": move_pct,
            "btc_price": btc_price,
            "btc_ts": btc_ts,
            "poly_mid": current_mid,
            "poly_ask": entry_price,
            "implied_edge": edge,
            "fee": fee,
            "profit_if_hold": profit_hold,
            "profit_if_exit": profit_exit,
            "poly_age_ms": poly_age * 1000,
            "detect_ts": now,
            "condition_id": self.condition_id,
            "token_id": self.up_token if trade_side == "up" else self.dn_token,
            "question": self.question,
        }

        if self.on_signal:
            await self.on_signal(signal)
