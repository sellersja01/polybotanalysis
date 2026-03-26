"""
market_mapper.py
================
Fetches open markets from both Polymarket and Kalshi, finds matching
BTC/ETH/SOL/XRP 15m (and hourly/daily) candles, and saves the mapping.

Run once at startup (or periodically) to refresh the map.

Output: arb_bot/market_map.json
[
  {
    "symbol":          "BTC",
    "timeframe":       "15m",
    "window_start_et": "2026-03-25 09:00",   // ET time
    "poly_condition":  "0xabc...",
    "poly_up_token":   "0xdef...",
    "poly_down_token": "0x123...",
    "kalshi_ticker":   "KXBTCU-26MAR25-1005",
    "kalshi_yes_is":   "up",                  // 'yes' token = Up on this market
  },
  ...
]
"""

import asyncio
import json
import re
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from polymarket_client import PolymarketClient

# ── Symbol config ──────────────────────────────────────────────────────────────
SYMBOLS = ["BTC", "ETH", "SOL", "XRP"]

POLY_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
}

KALSHI_SERIES = {
    "BTC": ["KXBTCU", "KXBTCD", "KXBTC"],
    "ETH": ["KXETHU", "KXETHD", "KXETH"],
    "SOL": ["KXSOLU", "KXSOLD", "KXSOL"],
    "XRP": ["KXXRPU", "KXXRPD", "KXXRP"],
}

TIMEFRAMES = ["15m", "1h", "1d"]


# ── Polymarket helpers ─────────────────────────────────────────────────────────
def poly_detect_symbol(question: str) -> str | None:
    q = question.lower()
    for sym, kws in POLY_KEYWORDS.items():
        if any(k in q for k in kws):
            return sym
    return None


def poly_detect_timeframe(question: str) -> str | None:
    q = question.lower()
    # "up or down" + time range like "9:00pm-9:15pm" → 15m
    # look for "hour" keyword or single hour window
    if re.search(r'\d+:\d+\s*(am|pm)[^\d]*-[^\d]*\d+:\d+\s*(am|pm)', q):
        # find the two times and compute diff
        times = re.findall(r'(\d+):(\d+)\s*(am|pm)', q)
        if len(times) >= 2:
            def to_min(h, m, ampm):
                h = int(h) % 12 + (12 if ampm == 'pm' else 0)
                return h * 60 + int(m)
            t1 = to_min(*times[0])
            t2 = to_min(*times[1])
            diff = abs(t2 - t1)
            if diff == 15:  return "15m"
            if diff == 60:  return "1h"
    if "daily" in q or "day" in q:
        return "1d"
    return None


# ── Kalshi helpers ─────────────────────────────────────────────────────────────
def kalshi_detect_symbol(ticker: str) -> str | None:
    t = ticker.upper()
    for sym, prefixes in KALSHI_SERIES.items():
        if any(t.startswith(p) for p in prefixes):
            return sym
    return None


# ── Main mapper ────────────────────────────────────────────────────────────────
async def build_map(key_id: str, key_path: str) -> list:
    poly   = PolymarketClient()
    kalshi = KalshiClient(key_id, key_path)

    print("Fetching Polymarket markets...")
    poly_markets = []
    cursor = ""
    while True:
        page = await poly.get_markets(cursor)
        batch = page.get("data", [])
        poly_markets.extend(batch)
        cursor = page.get("next_cursor", "")
        if not cursor or not batch:
            break
    print(f"  {len(poly_markets)} Polymarket markets fetched")

    print("Fetching Kalshi markets...")
    kalshi_markets = []
    for sym, prefixes in KALSHI_SERIES.items():
        for prefix in prefixes:
            try:
                batch = await kalshi.get_markets(series_ticker=prefix)
                kalshi_markets.extend(batch)
            except Exception:
                pass
    print(f"  {len(kalshi_markets)} Kalshi markets fetched")

    # Index Kalshi by symbol
    kalshi_by_sym = {sym: [] for sym in SYMBOLS}
    for m in kalshi_markets:
        sym = kalshi_detect_symbol(m.get("ticker", ""))
        if sym:
            kalshi_by_sym[sym].append(m)

    # Build map: for each Polymarket market find a Kalshi match
    mapping = []
    for pm in poly_markets:
        q    = pm.get("question", "")
        sym  = poly_detect_symbol(q)
        tf   = poly_detect_timeframe(q)
        if not sym or not tf:
            continue
        if "up or down" not in q.lower():
            continue

        tokens = pm.get("tokens", [])
        up_tok   = next((t["token_id"] for t in tokens if t.get("outcome","").lower() == "up"),   None)
        down_tok = next((t["token_id"] for t in tokens if t.get("outcome","").lower() == "down"), None)
        if not up_tok or not down_tok:
            continue

        # Find a Kalshi market with the same symbol + timeframe (loose match for now)
        for km in kalshi_by_sym[sym]:
            mapping.append({
                "symbol":         sym,
                "timeframe":      tf,
                "poly_question":  q,
                "poly_condition": pm.get("condition_id"),
                "poly_up_token":  up_tok,
                "poly_down_token": down_tok,
                "poly_up_bid":    None,
                "poly_up_ask":    None,
                "kalshi_ticker":  km["ticker"],
                "kalshi_yes_bid": None,
                "kalshi_yes_ask": None,
                "kalshi_yes_is":  "up",  # will be confirmed at runtime
            })
            break  # one Kalshi match per Poly market

    print(f"  {len(mapping)} matched pairs found")
    with open("market_map.json", "w") as f:
        json.dump(mapping, f, indent=2)
    print("  Saved → market_map.json")
    return mapping


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python market_mapper.py <kalshi_key_id> <kalshi_key_path>")
        sys.exit(1)
    asyncio.run(build_map(sys.argv[1], sys.argv[2]))
