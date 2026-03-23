import asyncio
import websockets
import requests
import sqlite3
import json
import time
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────
STARTING_BALANCE = 2500.0
BASE_RISK_PCT     = 0.04       # 4% of account per trade
MAX_SIMULTANEOUS  = 10         # max open positions at once
CUT_THRESHOLD     = 0.10       # exit if mid drops to this
MIN_ENTRY         = 0.30       # only enter if odds above this
MAX_ENTRY         = 0.60       # only enter if odds below this
BTC_MOVE_THRESH   = 30.0       # min BTC $ move to trigger signal

ASSETS = {
    "btc": {"coinbase_id": "BTC-USD"},
    "eth": {"coinbase_id": "ETH-USD"},
    "sol": {"coinbase_id": "SOL-USD"},
    "xrp": {"coinbase_id": "XRP-USD"},
}
TIMEFRAMES = {
    "5m":  {"interval": 300},
    "15m": {"interval": 900},
}
HOUR_SLUG_NAMES = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "xrp": "xrp"}

LOG_FILE = "paper_trades.db"

# ── Shared state ─────────────────────────────────────────────────
account = {"balance": STARTING_BALANCE, "peak": STARTING_BALANCE}
open_positions = {}   # key: (asset, tf, candle_id, outcome) -> position dict
prices = {}           # asset -> latest price
candle_open_price = defaultdict(dict)  # (asset,tf) -> {candle_id: open_price}
price_change_2m = defaultdict(dict)    # (asset,tf) -> {candle_id: change}
candle_start_time = defaultdict(dict)  # (asset,tf) -> {candle_id: unix_time}
lock = asyncio.Lock()

# ── DB setup ─────────────────────────────────────────────────────
def init_log_db():
    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            asset TEXT,
            timeframe TEXT,
            candle_id INTEGER,
            outcome TEXT,
            signal TEXT,
            entry_price REAL,
            exit_price REAL,
            entry_ask REAL,
            entry_bid REAL,
            entry_spread REAL,
            shares REAL,
            bet_size REAL,
            pnl REAL,
            pnl_pct REAL,
            exit_reason TEXT,
            account_balance REAL,
            duration_seconds REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            unix_time REAL,
            balance REAL,
            open_positions INTEGER,
            total_trades INTEGER
        )
    """)
    conn.commit()
    conn.close()

def log_trade(asset, tf, candle_id, outcome, signal, entry_price, exit_price,
              entry_ask, entry_bid, entry_spread, shares, bet_size, pnl, exit_reason, entry_unix):
    pnl_pct = (pnl / bet_size) * 100 if bet_size > 0 else 0
    duration = time.time() - entry_unix
    ts = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        INSERT INTO trades (timestamp, asset, timeframe, candle_id, outcome, signal,
            entry_price, exit_price, entry_ask, entry_bid, entry_spread,
            shares, bet_size, pnl, pnl_pct, exit_reason, account_balance, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ts, asset, tf, candle_id, outcome, signal,
          entry_price, exit_price, entry_ask, entry_bid, entry_spread,
          shares, bet_size, pnl, pnl_pct, exit_reason, account["balance"], duration))
    conn.commit()
    conn.close()

def log_snapshot():
    conn = sqlite3.connect(LOG_FILE)
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.execute("INSERT INTO account_snapshots (timestamp, unix_time, balance, open_positions, total_trades) VALUES (?, ?, ?, ?, ?)",
                 (datetime.now(timezone.utc).isoformat(), time.time(), account["balance"], len(open_positions), total))
    conn.commit()
    conn.close()

# ── Signal logic ─────────────────────────────────────────────────
def get_signal(asset, tf, candle_id, outcome, mid, ask, bid):
    """Returns signal name or None."""
    key = (asset, tf)
    cid = candle_id

    # Need open price for this candle
    if cid not in candle_open_price[key]:
        return None

    # Must be at least 60 seconds into the candle before entering
    if cid not in candle_start_time[key]:
        return None
    elapsed = time.time() - candle_start_time[key][cid]
    if elapsed < 60:
        return None

    open_p = candle_open_price[key].get(cid, 0)
    move_2m = price_change_2m[key].get(cid, 0)
    spread = ask - bid

    # Scale threshold per asset
    avg_price = prices.get(asset, 0)
    if avg_price == 0:
        return None
    scale = avg_price / 80000
    thresh = max(0.05, BTC_MOVE_THRESH * scale)

    # Only trade cheap side
    if not (MIN_ENTRY <= mid <= MAX_ENTRY):
        return None

    # DIRECTION LOCK — only trade in the direction price is actually moving
    # If move_2m is positive, only allow Up trades. If negative, only Down.
    # If near zero, no trade.
    if abs(move_2m) < thresh * 0.3:
        return None  # price not moving enough, skip

    # Enforce direction match — outcome must align with price move
    if move_2m > 0 and outcome == "Down":
        return None  # price going up, don't bet Down
    if move_2m < 0 and outcome == "Up":
        return None  # price going down, don't bet Up

    # Also block if opposite direction already has an open position this candle
    opposite = "Down" if outcome == "Up" else "Up"
    opposite_key = (asset, tf, cid, opposite)
    if opposite_key in open_positions:
        return None  # already in opposite direction, skip

    # S1: Early momentum
    if outcome == "Down" and move_2m < -thresh:
        return "S1_momentum_down"
    if outcome == "Up" and move_2m > thresh:
        return "S1_momentum_up"

    # S2: Strong momentum
    if outcome == "Down" and move_2m < -thresh * 1.5:
        return "S2_strong_down"
    if outcome == "Up" and move_2m > thresh * 1.5:
        return "S2_strong_up"

    # S3: Price moved but odds still cheap
    if outcome == "Down" and move_2m < -thresh * 0.7 and mid > 0.45:
        return "S3_cheap_down"
    if outcome == "Up" and move_2m > thresh * 0.7 and mid < 0.55:
        return "S3_cheap_up"

    return None

# ── Entry / Exit ──────────────────────────────────────────────────
async def try_enter(asset, tf, candle_id, outcome, mid, ask, bid, signal):
    async with lock:
        pos_key = (asset, tf, candle_id, outcome)
        if pos_key in open_positions:
            return
        if len(open_positions) >= MAX_SIMULTANEOUS:
            return

        bet = account["balance"] * BASE_RISK_PCT
        bet = max(10.0, min(bet, account["balance"] * 0.10))  # floor $10, cap 10%
        shares = bet / ask  # buy at ask

        open_positions[pos_key] = {
            "asset": asset, "tf": tf, "candle_id": candle_id, "outcome": outcome,
            "signal": signal, "entry_price": ask, "entry_ask": ask,
            "entry_bid": bid, "entry_spread": ask - bid,
            "shares": shares, "bet_size": bet, "entry_unix": time.time(),
        }

        print(f"\n🟢 ENTER  {asset.upper()} {tf} {outcome} | signal={signal} | ask={ask:.3f} | bet=${bet:.2f} | shares={shares:.1f} | balance=${account['balance']:.2f}")

async def try_exit(pos_key, mid, bid, reason):
    async with lock:
        if pos_key not in open_positions:
            return
        pos = open_positions.pop(pos_key)

        exit_price = bid  # sell at bid
        pnl = (exit_price - pos["entry_price"]) * pos["shares"]
        account["balance"] += pnl
        if account["balance"] > account["peak"]:
            account["peak"] = account["balance"]

        log_trade(
            pos["asset"], pos["tf"], pos["candle_id"], pos["outcome"], pos["signal"],
            pos["entry_price"], exit_price, pos["entry_ask"], pos["entry_bid"],
            pos["entry_spread"], pos["shares"], pos["bet_size"], pnl, reason, pos["entry_unix"]
        )

        emoji = "✅" if pnl > 0 else "❌"
        print(f"\n{emoji} EXIT   {pos['asset'].upper()} {pos['tf']} {pos['outcome']} | reason={reason} | exit={exit_price:.3f} | pnl=${pnl:+.2f} | balance=${account['balance']:.2f}")

# ── Slug / token helpers ──────────────────────────────────────────
def get_slug(asset, tf_key):
    interval = TIMEFRAMES[tf_key]["interval"]
    now = int(time.time())
    ts = (now // interval) * interval
    return f"{asset}-updown-{tf_key}-{ts}"

def fetch_tokens(slug):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10)
        data = r.json()
        if data:
            market = data[0]["markets"][0]
            tokens = json.loads(market.get("clobTokenIds", "[]"))
            if len(tokens) >= 2:
                return tokens[0], tokens[1], market.get("question", ""), str(market.get("id", ""))
    except:
        pass
    return None, None, "", ""

# ── Price stream ──────────────────────────────────────────────────
async def stream_price(asset):
    coinbase_id = ASSETS[asset]["coinbase_id"]
    while True:
        try:
            async with websockets.connect("wss://advanced-trade-ws.coinbase.com") as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": [coinbase_id], "channel": "ticker"}))
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("channel") == "ticker":
                        for event in data.get("events", []):
                            for ticker in event.get("tickers", []):
                                price = float(ticker.get("price", 0))
                                if price > 0:
                                    prices[asset] = price
        except Exception as e:
            await asyncio.sleep(2)

# ── CLOB stream ───────────────────────────────────────────────────
async def stream_clob(asset, tf_key, token_up, token_down, market_id, question, candle_id, stop_event):
    label = f"[{asset.upper()} {tf_key}]"
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    books = {
        token_up:   {"bids": {}, "asks": {}},
        token_down: {"bids": {}, "asks": {}},
    }
    key = (asset, tf_key)

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"auth": {}, "type": "subscribe", "assets_ids": [token_up, token_down], "markets": []}))
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        events = json.loads(msg)
                        if not isinstance(events, list):
                            events = [events]

                        for event in events:
                            event_type = event.get("event_type", "")
                            asset_id = event.get("asset_id", "")
                            if asset_id not in books:
                                continue
                            book = books[asset_id]

                            if event_type == "book":
                                book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids", [])}
                                book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks", [])}
                            elif event_type == "price_change":
                                for change in event.get("changes", []):
                                    side = change["side"]
                                    price = change["price"]
                                    size = float(change["size"])
                                    if side == "BUY":
                                        if size == 0: book["bids"].pop(price, None)
                                        else: book["bids"][price] = size
                                    elif side == "SELL":
                                        if size == 0: book["asks"].pop(price, None)
                                        else: book["asks"][price] = size

                            bids = [float(p) for p, s in book["bids"].items() if s > 0]
                            asks = [float(p) for p, s in book["asks"].items() if s > 0]
                            if not bids or not asks:
                                continue

                            best_bid = max(bids)
                            best_ask = min(asks)
                            mid = (best_bid + best_ask) / 2
                            outcome = "Up" if asset_id == token_up else "Down"
                            pos_key = (asset, tf_key, candle_id, outcome)

                            # Track candle open price
                            if candle_id not in candle_open_price[key]:
                                if asset in prices:
                                    candle_open_price[key][candle_id] = prices[asset]
                                    candle_start_time[key][candle_id] = time.time()

                            # Track price change — lock in between 60-120s
                            if candle_id in candle_start_time[key]:
                                elapsed = time.time() - candle_start_time[key][candle_id]
                                if 60 <= elapsed <= 120 and asset in prices:
                                    open_p = candle_open_price[key].get(candle_id, prices[asset])
                                    price_change_2m[key][candle_id] = prices[asset] - open_p

                            # Check exit for open position
                            if pos_key in open_positions:
                                if mid <= CUT_THRESHOLD:
                                    await try_exit(pos_key, mid, best_bid, "cut_loss")
                                elif mid >= 0.95:
                                    await try_exit(pos_key, mid, best_bid, "resolution_win")
                                elif mid <= 0.05:
                                    await try_exit(pos_key, mid, best_bid, "resolution_loss")
                            else:
                                # Check entry signal
                                signal = get_signal(asset, tf_key, candle_id, outcome, mid, best_ask, best_bid)
                                if signal:
                                    await try_enter(asset, tf_key, candle_id, outcome, mid, best_ask, best_bid, signal)

                    except asyncio.TimeoutError:
                        await ws.ping()
        except Exception as e:
            if not stop_event.is_set():
                await asyncio.sleep(2)

# ── Market manager ────────────────────────────────────────────────
async def manage_market(asset, tf_key):
    last_slug = None
    ws_task = None
    stop_event = asyncio.Event()

    while True:
        slug = get_slug(asset, tf_key)
        if slug != last_slug:
            interval = TIMEFRAMES[tf_key]["interval"]
            candle_id = int(time.time()) // interval

            token_up, token_down, question, market_id = fetch_tokens(slug)
            if token_up and token_down:
                if ws_task and not ws_task.done():
                    stop_event.set()
                    ws_task.cancel()
                    try: await ws_task
                    except: pass

                # Close any open positions from old candle
                async with lock:
                    old_keys = [k for k in open_positions if k[0] == asset and k[1] == tf_key and k[2] != candle_id]
                    for k in old_keys:
                        pos = open_positions.pop(k)
                        print(f"\n⏰ CANDLE END {asset.upper()} {tf_key} {k[3]} — closing at mid")

                stop_event = asyncio.Event()
                ws_task = asyncio.create_task(
                    stream_clob(asset, tf_key, token_up, token_down, market_id, question, candle_id, stop_event)
                )
                last_slug = slug
                print(f"\n[{asset.upper()} {tf_key}] New candle: {question}")
        await asyncio.sleep(1)

# ── Stats printer ─────────────────────────────────────────────────
async def print_stats():
    while True:
        await asyncio.sleep(60)
        log_snapshot()
        conn = sqlite3.connect(LOG_FILE)
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        wins  = conn.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0").fetchone()[0]
        total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
        conn.close()
        wr = (wins/total*100) if total > 0 else 0
        print(f"\n{'='*55}")
        print(f"  📊 PAPER TRADING STATS")
        print(f"  Balance:      ${account['balance']:,.2f}  (started $2,500)")
        print(f"  Peak:         ${account['peak']:,.2f}")
        print(f"  Total P&L:    ${total_pnl:+,.2f}")
        print(f"  Trades:       {total} | WR: {wr:.1f}%")
        print(f"  Open pos:     {len(open_positions)}")
        print(f"{'='*55}\n")

# ── Main ──────────────────────────────────────────────────────────
async def main():
    init_log_db()
    print(f"\n🚀 PAPER TRADER — Starting balance: ${STARTING_BALANCE:,.2f}")
    print(f"   Risk per trade: {BASE_RISK_PCT*100:.0f}% | Max positions: {MAX_SIMULTANEOUS}")
    print(f"   Entry zone: {MIN_ENTRY:.0%}–{MAX_ENTRY:.0%} | Cut at: {CUT_THRESHOLD:.0%}\n")

    tasks = []
    for asset in ASSETS:
        tasks.append(stream_price(asset))
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            tasks.append(manage_market(asset, tf))
    tasks.append(print_stats())

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
