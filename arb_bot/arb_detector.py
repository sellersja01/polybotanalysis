"""
arb_detector.py — Detects cross-platform arb opportunities
============================================================
- Reverse ticker index for O(1) Kalshi lookups
- Dedup: won't re-fire on same pair+direction within cooldown
- Timestamps every price update to reject stale data
- Dual fee regime (auto-selects based on date)
"""
import asyncio
import time

from config import poly_taker_fee, kalshi_maker_fee, kalshi_taker_fee
from config import MIN_PROFIT_CENTS, DEDUP_COOLDOWN, STALE_PRICE_MS


def check_arb(poly_up_ask: float, poly_down_ask: float,
              k_up_ask: float, k_down_ask: float) -> dict | None:
    """
    Check both arb directions with both fee scenarios.
    Returns best opportunity or None.
    """
    results = []

    for poly_ask, k_ask, p_side, k_side in [
        (poly_up_ask,   k_down_ask, "poly_up",   "kalshi_down"),
        (poly_down_ask, k_up_ask,   "poly_down", "kalshi_up"),
    ]:
        if poly_ask <= 0 or k_ask <= 0 or poly_ask >= 1 or k_ask >= 1:
            continue

        cost = poly_ask + k_ask

        # Scenario A: Poly taker + Kalshi maker (0% fee)
        fees_a  = poly_taker_fee(poly_ask)
        net_a   = 1.0 - cost - fees_a
        if net_a > MIN_PROFIT_CENTS / 100:
            results.append({
                "profit":      net_a,
                "poly_side":   p_side,
                "kalshi_side": k_side,
                "poly_ask":    poly_ask,
                "kalshi_ask":  k_ask,
                "poly_mode":   "taker",
                "kalshi_mode": "maker",
                "fees":        fees_a,
                "cost":        cost,
                "roi_pct":     net_a / cost * 100,
            })

        # Scenario B: both taker
        fees_b = poly_taker_fee(poly_ask) + kalshi_taker_fee(k_ask)
        net_b  = 1.0 - cost - fees_b
        if net_b > MIN_PROFIT_CENTS / 100:
            results.append({
                "profit":      net_b,
                "poly_side":   p_side,
                "kalshi_side": k_side,
                "poly_ask":    poly_ask,
                "kalshi_ask":  k_ask,
                "poly_mode":   "taker",
                "kalshi_mode": "taker",
                "fees":        fees_b,
                "cost":        cost,
                "roi_pct":     net_b / cost * 100,
            })

    if not results:
        return None
    return max(results, key=lambda x: x["profit"])


class ArbState:
    """Tracks live prices for all pairs and fires arb signals."""

    def __init__(self, market_map: list, on_arb):
        self.pairs  = {m["poly_condition"]: m for m in market_map}
        self.on_arb = on_arb

        # Reverse index: kalshi_ticker -> poly_condition (O(1) lookup)
        self._kalshi_to_cond = {
            m["kalshi_ticker"]: m["poly_condition"] for m in market_map
        }

        # Live prices with timestamps
        self.poly_prices   = {}   # cond_id -> {up_bid, up_ask, dn_bid, dn_ask, ts}
        self.kalshi_prices = {}   # cond_id -> {up_bid, up_ask, dn_bid, dn_ask, ts}

        # Dedup: (cond_id, direction) -> last_fire_time
        self._last_fire = {}

    def update_poly(self, condition_id: str, up_bid: float, up_ask: float,
                    down_bid: float, down_ask: float):
        self.poly_prices[condition_id] = {
            "up_bid": up_bid, "up_ask": up_ask,
            "dn_bid": down_bid, "dn_ask": down_ask,
            "ts": time.time(),
        }
        self._check(condition_id)

    def update_kalshi(self, ticker: str, yes_bid: float, yes_ask: float,
                      no_bid: float, no_ask: float):
        cond = self._kalshi_to_cond.get(ticker)
        if cond is None:
            return

        pair = self.pairs[cond]
        if pair["kalshi_yes_is"] == "up":
            up_bid, up_ask = yes_bid, yes_ask
            dn_bid, dn_ask = no_bid, no_ask
        else:
            up_bid, up_ask = no_bid, no_ask
            dn_bid, dn_ask = yes_bid, yes_ask

        self.kalshi_prices[cond] = {
            "up_bid": up_bid, "up_ask": up_ask,
            "dn_bid": dn_bid, "dn_ask": dn_ask,
            "ts": time.time(),
        }
        self._check(cond)

    def _check(self, condition_id: str):
        pair = self.pairs.get(condition_id)
        pp   = self.poly_prices.get(condition_id)
        kp   = self.kalshi_prices.get(condition_id)
        if not pair or not pp or not kp:
            return

        # Reject stale prices
        now = time.time()
        if (now - pp["ts"]) * 1000 > STALE_PRICE_MS:
            return
        if (now - kp["ts"]) * 1000 > STALE_PRICE_MS:
            return

        opp = check_arb(
            pp["up_ask"], pp["dn_ask"],
            kp["up_ask"], kp["dn_ask"],
        )
        if not opp:
            return

        # Dedup: don't fire same pair+direction within cooldown
        fire_key = (condition_id, opp["poly_side"])
        last = self._last_fire.get(fire_key, 0)
        if now - last < DEDUP_COOLDOWN:
            return

        self._last_fire[fire_key] = now

        opp["pair"]        = pair
        opp["condition_id"] = condition_id
        opp["poly_up_bid"]  = pp["up_bid"]
        opp["poly_dn_bid"]  = pp["dn_bid"]
        opp["detect_ts"]    = now

        asyncio.ensure_future(self.on_arb(opp))
