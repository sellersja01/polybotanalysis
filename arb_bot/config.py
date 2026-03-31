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
POLY_GAMMA_URL = "https://gamma-api.polymarket.com"

# ── Kalshi credentials (from env) ─────────────────────────────────────────────
KALSHI_KEY_ID   = os.environ.get("KALSHI_KEY_ID",   "d307ccc8-df96-4210-8d42-8d70c75fe71f")
KALSHI_KEY_PATH = os.environ.get("KALSHI_KEY_PATH", "/home/opc/kalshi_key.pem")

# ── Polymarket credentials (from env) ─────────────────────────────────────────
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "")   # wallet private key (0x...)
POLY_ADDRESS     = os.environ.get("POLY_ADDRESS",     "0x6826c3197fff281144b07fe6c3e72636854769ab")
POLY_API_KEY     = os.environ.get("POLY_API_KEY",     "019d323e-f149-794e-8c3b-6c1df3877250")

# ── Kalshi 15m series tickers ─────────────────────────────────────────────────
KALSHI_SERIES = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
}
ASSETS = ["BTC"]

# ── Trading config ────────────────────────────────────────────────────────────
DRY_RUN          = os.environ.get("DRY_RUN", "true").lower() != "false"  # default: DRY RUN
SHARES_PER_TRADE = int(os.environ.get("SHARES_PER_TRADE", "10"))  # higher = less relative rounding error on Kalshi
MAX_TRADES_PER_CANDLE = 3    # max 3 trades per TRADE_WINDOW
TRADE_WINDOW          = 900  # 15-minute trade window (aligns with candle)
MIN_PROFIT_CENTS = 2.0   # min net profit per $1 contract (cents) to fire
DEDUP_COOLDOWN   = 10.0  # seconds between fires on same pair+direction
STALE_PRICE_MS   = 12000  # reject arbs if either price older than this (ms)
CANDLE_INTERVAL  = 900   # 15 minutes in seconds

# ── Fee functions ─────────────────────────────────────────────────────────────
FEE_CHANGE_DATE = datetime(2026, 3, 30, tzinfo=timezone.utc)

def poly_taker_fee(price: float) -> float:
    """Fee per $1 contract — auto-selects formula based on date."""
    if datetime.now(timezone.utc) >= FEE_CHANGE_DATE:
        return price * 0.072 * (price * (1 - price))
    return price * 0.25 * (price * (1 - price)) ** 2

def kalshi_taker_fee(price: float) -> float:
    return 0.07 * price * (1 - price)

def kalshi_maker_fee(price: float) -> float:
    return 0.0
