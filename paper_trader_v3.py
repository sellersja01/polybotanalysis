import asyncio
import websockets
import requests
import sqlite3
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────
STARTING_BALANCE = 2500.0
BASE_RISK_PCT     = 0.04       # 4% of account per trade
MAX_SIMULTANEOUS  = 10
CUT_THRESHOLD     = 0.10       # exit early if mid drops to this
MIN_ENTRY         = 0.15       # min odds to enter
MAX_ENTRY         = 0.70       # max odds to enter

ASSETS = {
    "btc": {"coinbase_id": "BTC-USD", "thresh_small": 27.76, "thresh_large": 46.26},
    "eth": {"coinbase_id": "ETH-USD", "thresh_small": 0.87,  "thresh_large": 1.45},
}
TIMEFRAMES = {
    "5m":  {"interval": 300},
    "15m": {"interval": 900},
}

LOG_FILE = "paper_trades_v3.db"

# ── Shared state ─────────────────────────────────────────────────
account           = {"balance": STARTING_BALANCE, "peak": STARTING_BALANCE}
open_positions    = {}
prices            = {}
candle_open_price = defaultdict(dict)
price_change_2m   = defaultdict(dict)
candle_start_time = defaultdict(dict)
prev_resolution   = defaultdict(dict)  # (asset,tf) -> {candle_id: "Up"/"Down"}
lock              = asyncio.Lock()

# ── DB ────────────────────────────────────────────────────────────
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
    pnl_pct  = (pnl / bet_size) * 100 if bet_size > 0 else 0
    duration = time.time() - entry_unix
    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        INSERT INTO trades (timestamp, asset, timeframe, candle_id, outcome, signal,
            entry_price, exit_price, entry_ask, entry_bid, entry_spread,
            shares, bet_size, pnl, pnl_pct, exit_reason, account_balance, duration_seconds)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), asset, tf, candle_id, outcome, signal,
          entry_price, exit_price, entry_ask, entry_bid, entry_spread,
          shares, bet_size, pnl, pnl_pct, exit_reason, account["balance"], duration))
    conn.commit()
    conn.close()

def log_snapshot():
    conn = sqlite3.connect(LOG_FILE)
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.execute("""
        INSERT INTO account_snapshots (timestamp, unix_time, balance, open_positions, total_trades)
        VALUES (?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), time.time(), account["balance"], len(open_positions), total))
    conn.commit()
    conn.close()

# ── Signal logic ─────────────────────────────────────────────────
def get_signal(asset, tf, candle_id, outcome, mid, ask, bid):
    key = (asset, tf)
    cid = candle_id

    if cid not in candle_open_price[key]:
        return None
    if cid not in candle_start_time[key]:
        return None

    elapsed = time.time() - candle_start_time[key][cid]
    if elapsed < 60:
        return None

    # don't enter in last 30s of candle
    interval = TIMEFRAMES[tf]["interval"]
    candle_start = cid * interval
    seconds_in = time.time() - candle_start
    if seconds_in > interval - 30:
        return None

    move_2m = price_change_2m[key].get(cid, 0)
    spread  = ask - bid
    avg_price = prices.get(asset, 0)
    if avg_price == 0:
        return None

    ts = ASSETS[asset]["thresh_small"]
    tl = ASSETS[asset]["thresh_large"]

    if not (MIN_ENTRY <= mid <= MAX_ENTRY):
        return None

    # block opposite direction if already in a position this candle
    opposite = "Down" if outcome == "Up" else "Up"
    if (asset, tf, cid, opposite) in open_positions:
        return None

    # ── DOWN strategies ──────────────────────────────────────────
    if outcome == "Down":
        # S1: early momentum down
        if move_2m < -ts:
            return "S1_DOWN"

        # S2: strong momentum down
        if move_2m < -tl:
            return "S2_DOWN"

        # S4: fade extreme open — Up odds > 75c, Down is cheap
        if mid < 0.25:
            return "S4_DOWN"

        # S6: mean revert — prev candle resolved Up, bet Down this candle
        prev_cid = cid - 1
        if prev_resolution[key].get(prev_cid) == "Up":
            return "S6_DOWN"

        # S7: tight spread + momentum down
        if spread < 0.015 and move_2m < -ts:
            return "S7_DOWN"

        # S8: big move but odds lagging (Up still > 40c)
        if move_2m < -tl and (1 - mid) > 0.40:
            return "S8_DOWN"

    # ── UP strategies (kept for completeness, lower priority) ────
    if outcome == "Up":
        # S1 Up
        if move_2m > ts:
            return "S1_UP"

    return None

# ── Entry / Exit ──────────────────────────────────────────────────
async def try_enter(asset, tf, candle_id, outcome, mid, ask, bid, signal):
    async with lock:
        pos_key = (asset, tf, candle_id, outcome)
        if pos_key in open_positions:
            return
        if len(open_positions) >= MAX_SIMULTANEOUS:
            return

        bet    = account["balance"] * BASE_RISK_PCT
        bet    = max(10.0, min(bet, account["balance"] * 0.10))
        shares = bet / ask
        spread = ask - bid

        account["balance"] -= bet
        account["peak"]     = max(account["peak"], account["balance"])

        open_positions[pos_key] = {
            "entry_price": ask,
            "entry_ask":   ask,
            "entry_bid":   bid,
            "entry_spread": spread,
            "shares":      shares,
            "bet_size":    bet,
            "signal":      signal,
            "entry_unix":  time.time(),
        }
        print(f"  [ENTER] {asset.upper()} {tf} {outcome} | {signal} | ask={ask:.3f} shares={shares:.1f} bet=${bet:.2f} | bal=${account['balance']:.2f}")

async def try_exit(pos_key, mid, bid, reason):
    async with lock:
        if pos_key not in open_positions:
            return
        pos    = open_positions.pop(pos_key)
        asset, tf, candle_id, outcome = pos_key
        exit_p = bid
        pnl    = (exit_p - pos["entry_price"]) * pos["shares"]
        account["balance"] += pos["bet_size"] + pnl
        account["peak"]     = max(account["peak"], account["balance"])

        # track resolution for S6
        key = (asset, tf)
        if reason in ("resolution_win", "resolution_loss"):
            won = reason == "resolution_win"
            resolved_outcome = outcome if won else ("Down" if outcome == "Up" else "Up")
            prev_resolution[key][candle_id] = resolved_outcome

        log_trade(asset, tf, candle_id, outcome, pos["signal"],
                  pos["entry_price"], exit_p, pos["entry_ask"], pos["entry_bid"],
                  pos["entry_spread"], pos["shares"], pos["bet_size"],
                  pnl, reason, pos["entry_unix"])

        icon = "✅" if pnl > 0 else "❌"
        print(f"  {icon} [EXIT] {asset.upper()} {tf} {outcome} | {reason} | exit={exit_p:.3f} pnl=${pnl:+.2f} | bal=${account['balance']:.2f}")

# ── Helpers ───────────────────────────────────────────────────────
def get_slug(asset, tf_key):
    interval = TIMEFRAMES[tf_key]["interval"]
    now = int(time.time())
    ts  = (now // interval) * interval
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
                    msg  = await ws.recv()
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
    url   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
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
                        msg    = await asyncio.wait_for(ws.recv(), timeout=30)
                        events = json.loads(msg)
                        if not isinstance(events, list):
                            events = [events]

                        for event in events:
                            event_type = event.get("event_type", "")
                            asset_id   = event.get("asset_id", "")
                            if asset_id not in books:
                                continue
                            book = books[asset_id]

                            if event_type == "book":
                                book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids", [])}
                                book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks", [])}
                            elif event_type == "price_change":
                                for change in event.get("changes", []):
                                    side  = change["side"]
                                    price = change["price"]
                                    size  = float(change["size"])
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
                            mid      = (best_bid + best_ask) / 2
                            outcome  = "Up" if asset_id == token_up else "Down"
                            pos_key  = (asset, tf_key, candle_id, outcome)

                            # track candle open price
                            if candle_id not in candle_open_price[key]:
                                if asset in prices:
                                    candle_open_price[key][candle_id] = prices[asset]
                                    candle_start_time[key][candle_id] = time.time()

                            # lock in price change between 60-120s
                            if candle_id in candle_start_time[key]:
                                elapsed = time.time() - candle_start_time[key][candle_id]
                                if 60 <= elapsed <= 120 and asset in prices:
                                    open_p = candle_open_price[key].get(candle_id, prices[asset])
                                    price_change_2m[key][candle_id] = prices[asset] - open_p

                            # exit check
                            if pos_key in open_positions:
                                if mid <= CUT_THRESHOLD:
                                    await try_exit(pos_key, mid, best_bid, "cut_loss")
                                elif mid >= 0.95:
                                    await try_exit(pos_key, mid, best_bid, "resolution_win")
                                elif mid <= 0.05:
                                    await try_exit(pos_key, mid, best_bid, "resolution_loss")
                            else:
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
    last_slug  = None
    ws_task    = None
    stop_event = asyncio.Event()

    while True:
        slug = get_slug(asset, tf_key)
        if slug != last_slug:
            interval  = TIMEFRAMES[tf_key]["interval"]
            candle_id = int(time.time()) // interval
            token_up, token_down, question, market_id = fetch_tokens(slug)
            if token_up and token_down:
                if ws_task and not ws_task.done():
                    stop_event.set()
                    ws_task.cancel()
                    try: await ws_task
                    except: pass

                # close stale positions
                async with lock:
                    old_keys = [k for k in open_positions if k[0] == asset and k[1] == tf_key and k[2] != candle_id]
                    for k in old_keys:
                        pos = open_positions.pop(k)
                        print(f"  ⏰ [CANDLE END] {asset.upper()} {tf_key} {k[3]} — closing stale position")

                stop_event = asyncio.Event()
                ws_task    = asyncio.create_task(
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
        conn  = sqlite3.connect(LOG_FILE)
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        wins  = conn.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0").fetchone()[0]
        total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
        strats = conn.execute("""
            SELECT signal, COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), SUM(pnl)
            FROM trades GROUP BY signal ORDER BY SUM(pnl) DESC
        """).fetchall()
        conn.close()

        wr = (wins / total * 100) if total > 0 else 0
        print(f"\n{'='*55}")
        print(f"  PAPER TRADING STATS")
        print(f"  Balance:   ${account['balance']:,.2f}  (started $2,500)")
        print(f"  Peak:      ${account['peak']:,.2f}")
        print(f"  Total P&L: ${total_pnl:+,.2f}")
        print(f"  Trades:    {total} | WR: {wr:.1f}%")
        print(f"  Open:      {len(open_positions)}")
        if strats:
            print(f"  --- By Strategy ---")
            for s, n, w, p in strats:
                swr = (w/n*100) if n > 0 else 0
                print(f"  {s:<14} n={n:3d} WR={swr:.1f}% pnl=${p:+.2f}")
        print(f"{'='*55}\n")

# ── Main ──────────────────────────────────────────────────────────
async def main():
    init_log_db()
    print(f"\n PAPER TRADER v3 — Starting balance: ${STARTING_BALANCE:,.2f}")
    print(f"  Risk: {BASE_RISK_PCT*100:.0f}% | Max positions: {MAX_SIMULTANEOUS}")
    print(f"  Strategies: S1_DOWN S2_DOWN S4_DOWN S6_DOWN S7_DOWN S8_DOWN S1_UP")
    print(f"  Assets: {list(ASSETS.keys())} | TFs: {list(TIMEFRAMES.keys())}\n")

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
