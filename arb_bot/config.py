"""
config.py — All constants and fee functions
"""
import os
from datetime import datetime, timezone

# ── API endpoints ─────────────────────────────────────────────────────────────
KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL  = "wss://api.elections.kalshi.com/trade-api/ws/v2"
POLY_CLOB_URL  = "https://clob.polymarket.com"
POLY_WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Credentials (from env) ────────────────────────────────────────────────────
KALSHI_KEY_ID   = os.environ.get("KALSHI_KEY_ID", "")
KALSHI_KEY_PATH = os.environ.get("KALSHI_KEY_PATH", "kalshi_key.pem")

# ── Trading config ────────────────────────────────────────────────────────────
DRY_RUN          = os.environ.get("DRY_RUN", "true").lower() != "false"
SHARES_PER_TRADE = int(os.environ.get("SHARES_PER_TRADE", "100"))
MIN_PROFIT_CENTS = 0.5   # min net profit per share (cents) to fire
DEDUP_COOLDOWN   = 0.5   # seconds between fires on same pair+direction
STALE_PRICE_MS   = 2000  # reject arbs if either price older than this

# ── Fee functions ─────────────────────────────────────────────────────────────
FEE_CHANGE_DATE = datetime(2026, 3, 30, tzinfo=timezone.utc)

def poly_taker_fee_current(price: float) -> float:
    """fee per $1 contract: price * 0.25 * (p*(1-p))^2"""
    return price * 0.25 * (price * (1 - price)) ** 2

def poly_taker_fee_new(price: float) -> float:
    """fee per $1 contract after March 30: price * 0.072 * (p*(1-p))^1"""
    return price * 0.072 * (price * (1 - price))

def poly_taker_fee(price: float) -> float:
    """Auto-selects current or new fee based on date."""
    if datetime.now(timezone.utc) >= FEE_CHANGE_DATE:
        return poly_taker_fee_new(price)
    return poly_taker_fee_current(price)

def kalshi_taker_fee(price: float) -> float:
    return 0.07 * price * (1 - price)

def kalshi_maker_fee(price: float) -> float:
    return 0.0
