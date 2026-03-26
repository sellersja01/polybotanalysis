"""
arb_detector.py
===============
Holds live prices for all mapped markets and fires an arb signal
whenever a risk-free opportunity exists after fees.
"""

POLY_FEE_COEFF  = 0.25   # Polymarket taker: price * coeff * (p*(1-p))^2
KALSHI_MAKER_FEE = 0.0   # Kalshi maker: 0%
KALSHI_TAKER_FEE = 0.07  # Kalshi taker: 0.07 * p * (1-p)
MIN_PROFIT_CENTS = 0.5   # Minimum profit per share to fire (in cents)


def poly_taker_fee(price: float) -> float:
    return price * POLY_FEE_COEFF * (price * (1 - price)) ** 2


def kalshi_taker_fee(price: float) -> float:
    return KALSHI_TAKER_FEE * price * (1 - price)


def check_arb(poly_up_ask: float, poly_down_ask: float,
              kalshi_yes_ask: float, kalshi_no_ask: float,
              kalshi_yes_is_up: bool = True) -> dict | None:
    """
    Check all 4 arb directions:
      1. Buy Up  on Poly  + Buy No  on Kalshi (if yes=Up)
      2. Buy Down on Poly + Buy Yes on Kalshi (if yes=Up)
      3-4. Same with Kalshi as maker (post_only limit at ask)

    Returns the best opportunity dict, or None.
    """
    if kalshi_yes_is_up:
        k_up_ask   = kalshi_yes_ask
        k_down_ask = kalshi_no_ask
    else:
        k_up_ask   = kalshi_no_ask
        k_down_ask = kalshi_yes_ask

    results = []

    for (poly_ask, k_ask, label_poly, label_kalshi) in [
        (poly_up_ask,   k_down_ask, "poly_up",   "kalshi_down"),
        (poly_down_ask, k_up_ask,   "poly_down", "kalshi_up"),
    ]:
        # ── Scenario A: Poly taker + Kalshi maker ─────────────────────────────
        # Kalshi maker = limit order at ask price (0% fee)
        cost  = poly_ask + k_ask
        fees  = poly_taker_fee(poly_ask)   # Kalshi maker = 0
        profit = 1.0 - cost - fees
        if profit > MIN_PROFIT_CENTS / 100:
            results.append({
                "profit":       profit,
                "poly_side":    label_poly,
                "kalshi_side":  label_kalshi,
                "poly_ask":     poly_ask,
                "kalshi_ask":   k_ask,
                "poly_mode":    "taker",
                "kalshi_mode":  "maker",
                "fees":         fees,
                "roi_pct":      profit / cost * 100,
            })

        # ── Scenario B: Kalshi taker + Poly taker (both taker, higher bar) ────
        fees2  = poly_taker_fee(poly_ask) + kalshi_taker_fee(k_ask)
        profit2 = 1.0 - cost - fees2
        if profit2 > MIN_PROFIT_CENTS / 100:
            results.append({
                "profit":       profit2,
                "poly_side":    label_poly,
                "kalshi_side":  label_kalshi,
                "poly_ask":     poly_ask,
                "kalshi_ask":   k_ask,
                "poly_mode":    "taker",
                "kalshi_mode":  "taker",
                "fees":         fees2,
                "roi_pct":      profit2 / cost * 100,
            })

    if not results:
        return None
    return max(results, key=lambda x: x["profit"])


class ArbState:
    """Tracks live prices for all pairs and detects arb."""

    def __init__(self, market_map: list, on_arb):
        """
        market_map: list from market_mapper.build_map()
        on_arb: async callback(opportunity_dict)
        """
        self.pairs   = {m["poly_condition"]: m for m in market_map}
        self.on_arb  = on_arb
        # prices indexed by poly_condition
        self.poly_prices   = {}   # cond_id → {up_bid, up_ask, down_bid, down_ask}
        self.kalshi_prices = {}   # kalshi_ticker → {yes_bid, yes_ask, no_bid, no_ask}

    def update_poly(self, condition_id: str, up_bid: float, up_ask: float,
                    down_bid: float, down_ask: float):
        self.poly_prices[condition_id] = {
            "up_bid": up_bid, "up_ask": up_ask,
            "down_bid": down_bid, "down_ask": down_ask,
        }
        self._check(condition_id)

    def update_kalshi(self, ticker: str, yes_bid: float, yes_ask: float,
                      no_bid: float, no_ask: float):
        self.kalshi_prices[ticker] = {
            "yes_bid": yes_bid, "yes_ask": yes_ask,
            "no_bid": no_bid,  "no_ask": no_ask,
        }
        # find matching condition_id
        for cond, pair in self.pairs.items():
            if pair["kalshi_ticker"] == ticker:
                self._check(cond)
                break

    def _check(self, condition_id: str):
        pair = self.pairs.get(condition_id)
        if not pair:
            return
        pp = self.poly_prices.get(condition_id)
        kp = self.kalshi_prices.get(pair["kalshi_ticker"])
        if not pp or not kp:
            return

        opp = check_arb(
            pp["up_ask"], pp["down_ask"],
            kp["yes_ask"], kp["no_ask"],
            kalshi_yes_is_up=(pair["kalshi_yes_is"] == "up"),
        )
        if opp:
            opp["pair"]     = pair
            opp["poly_up_bid"]  = pp["up_bid"]
            opp["poly_dn_bid"]  = pp["down_bid"]
            asyncio.ensure_future(self.on_arb(opp))


import asyncio  # noqa: E402  (needed for ensure_future above)
