import asyncio
import websockets
import requests
import sqlite3
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────
STARTING_BALANCE = 2500.0
RISK_PCT         = 0.04
MAX_POSITIONS    = 12
CUT_THRESHOLD    = 0.05
MIN_ENTRY        = 0.15   # never enter if odds already below this
MAX_ENTRY        = 0.85   # never enter if odds already above this
LOG_FILE         = "paper_trades_v5.db"

ASSETS = {
    "btc": {"coinbase_id": "BTC-USD"},
    "eth": {"coinbase_id": "ETH-USD"},
}
TIMEFRAMES = {
    "5m":  {"interval": 300},
    "15m": {"interval": 900},
}

# ── Shared state ─────────────────────────────────────────────────
account        = {"balance": STARTING_BALANCE, "peak": STARTING_BALANCE}
open_positions = {}
candle_state   = {}
prices         = {}
lock           = asyncio.Lock()

# ── DB ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, asset TEXT, timeframe TEXT, candle_id INTEGER,
            strategy TEXT, outcome TEXT, entry_price REAL, exit_price REAL,
            bet_size REAL, shares REAL, pnl REAL, exit_reason TEXT, account_balance REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, balance REAL, open_positions INTEGER,
            total_trades INTEGER, total_pnl REAL
        )
    """)
    conn.commit()
    conn.close()

def log_trade(asset, tf, candle_id, strategy, outcome, entry, exit_p, bet, shares, pnl, reason):
    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        INSERT INTO trades (timestamp, asset, timeframe, candle_id, strategy, outcome,
            entry_price, exit_price, bet_size, shares, pnl, exit_reason, account_balance)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), asset, tf, candle_id, strategy,
          outcome, entry, exit_p, bet, shares, pnl, reason, account["balance"]))
    conn.commit()
    conn.close()

# ── Signal detection ──────────────────────────────────────────────
def get_signal(state, mid_up, ts):
    signals = []
    start     = state.get("start", ts)
    open_up   = state.get("open_up", mid_up)
    open_bias = open_up - 0.50
    elapsed   = ts - start
    prev_res  = state.get("prev_res")
    prev2_res = state.get("prev2_res")

    # ── Guard: never signal on nearly-resolved candles ─────────────
    if mid_up < MIN_ENTRY or mid_up > MAX_ENTRY:
        return signals

    if elapsed < 30:
        return signals

    mid30_up = state.get("mid30_up", open_up)
    mom30    = mid30_up - open_up

    mid60_up = state.get("mid60_up", mid30_up)
    mom60    = mid60_up - open_up

    # ── S1 Down — once per candle ──────────────────────────────────
    if (elapsed >= 30 and mom30 < -0.04 and mid_up < 0.45
            and not state.get("s1_dn_fired")):
        signals.append(("S1_Down", "Down"))
        state["s1_dn_fired"] = True

    # ── S1 Up — once per candle ────────────────────────────────────
    if (elapsed >= 30 and mom30 > 0.04 and mid_up < 0.45
            and not state.get("s1_up_fired")):
        signals.append(("S1_Up", "Up"))
        state["s1_up_fired"] = True

    # ── S2 Down — once per candle ──────────────────────────────────
    if (elapsed >= 30 and mom30 < -0.10
            and not state.get("s2_dn_fired")):
        signals.append(("S2_Down", "Down"))
        state["s2_dn_fired"] = True

    # ── S7 Down — once per candle ──────────────────────────────────
    if (elapsed >= 30 and mom30 < -0.04 and mid_up < 0.45
            and not state.get("s7_dn_fired")):
        signals.append(("S7_Down", "Down"))
        state["s7_dn_fired"] = True

    # ── New strats require 60s ─────────────────────────────────────
    if elapsed < 60:
        return signals

    # NS1 — Open bias + 60s momentum
    if open_bias > 0.03 and mom60 > 0.03 and not state.get("ns1_up_fired"):
        signals.append(("NS1_Bias_Mom_Up", "Up"))
        state["ns1_up_fired"] = True

    if open_bias < -0.03 and mom60 < -0.03 and not state.get("ns1_dn_fired"):
        signals.append(("NS1_Bias_Mom_Down", "Down"))
        state["ns1_dn_fired"] = True

    # NS2 — Strong opening bias >0.10
    if open_bias > 0.10 and not state.get("ns2_up_fired"):
        signals.append(("NS2_Strong_Bias_Up", "Up"))
        state["ns2_up_fired"] = True

    if open_bias < -0.10 and not state.get("ns2_dn_fired"):
        signals.append(("NS2_Strong_Bias_Down", "Down"))
        state["ns2_dn_fired"] = True

    # NS3 — Large 60s momentum >0.15
    if mom60 > 0.15 and not state.get("ns3_up_fired"):
        signals.append(("NS3_60s_Mom_Up", "Up"))
        state["ns3_up_fired"] = True

    if mom60 < -0.15 and not state.get("ns3_dn_fired"):
        signals.append(("NS3_60s_Mom_Down", "Down"))
        state["ns3_dn_fired"] = True

    # NS4 — Triple confirmation
    if open_bias > 0.03 and prev_res == "Up" and mom60 > 0.03 and not state.get("ns4_up_fired"):
        signals.append(("NS4_Triple_Up", "Up"))
        state["ns4_up_fired"] = True

    if open_bias < -0.03 and prev_res == "Down" and mom60 < -0.03 and not state.get("ns4_dn_fired"):
        signals.append(("NS4_Triple_Down", "Down"))
        state["ns4_dn_fired"] = True

    # NS5 — Streak reversal
    if prev_res == "Down" and prev2_res == "Down" and mom60 > 0.03 and not state.get("ns5_up_fired"):
        signals.append(("NS5_Reversal_Up", "Up"))
        state["ns5_up_fired"] = True

    if prev_res == "Up" and prev2_res == "Up" and mom60 < -0.03 and not state.get("ns5_dn_fired"):
        signals.append(("NS5_Reversal_Down", "Down"))
        state["ns5_dn_fired"] = True

    return signals

# ── Position management ───────────────────────────────────────────
async def try_enter(asset, tf, candle_id, strategy, side, ask, bid):
    async with lock:
        pos_key = (asset, tf, candle_id, strategy, side)
        if pos_key in open_positions: return
        if len(open_positions) >= MAX_POSITIONS: return
        if ask <= MIN_ENTRY or ask >= MAX_ENTRY: return  # guard here too
        if account["balance"] <= 0: return

        bet    = account["balance"] * RISK_PCT
        shares = bet / ask
        account["balance"] -= bet
        if account["balance"] > account["peak"]:
            account["peak"] = account["balance"]

        open_positions[pos_key] = {
            "entry": ask, "bet": bet, "shares": shares,
            "asset": asset, "tf": tf, "candle_id": candle_id,
            "strategy": strategy, "side": side,
        }
        print(f"  📈 [{strategy}] {asset.upper()} {tf} {side} @ {ask:.3f} | bet=${bet:.2f} | bal=${account['balance']:.2f}")

async def try_exit(pos_key, mid, bid, reason):
    async with lock:
        if pos_key not in open_positions: return
        pos    = open_positions.pop(pos_key)
        entry  = pos["entry"]
        shares = pos["shares"]
        bet    = pos["bet"]

        if reason == "win":
            pnl = (1.0 - entry) * shares
        elif reason in ("cut_loss", "loss"):
            pnl = (bid - entry) * shares if bid > 0 else -bet
        else:
            pnl = (bid - entry) * shares if bid > 0 else 0

        account["balance"] += bet + pnl
        if account["balance"] > account["peak"]:
            account["peak"] = account["balance"]

        icon = "✅" if pnl > 0 else "❌"
        print(f"  {icon} [{pos['strategy']}] {pos['asset'].upper()} {pos['tf']} {pos['side']} | {reason} | exit={mid:.3f} pnl=${pnl:+.2f} | bal=${account['balance']:.2f}")
        log_trade(pos["asset"], pos["tf"], pos["candle_id"], pos["strategy"],
                  pos["side"], entry, mid, bet, shares, pnl, reason)

# ── Polymarket helpers ────────────────────────────────────────────
def get_slug(asset, tf):
    now      = int(time.time())
    interval = TIMEFRAMES[tf]["interval"]
    bucket   = (now // interval) * interval
    return f"{asset}-updown-{tf}-{bucket}"

def fetch_tokens(slug):
    try:
        url  = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        resp = requests.get(url, timeout=8).json()
        if not resp: return None, None, None
        mkt  = resp[0]
        tokens = json.loads(mkt.get("clobTokenIds", "[]"))
        if len(tokens) < 2: return None, None, None
        outcomes = json.loads(mkt.get("outcomes", '["Up","Down"]'))
        up_idx   = outcomes.index("Up") if "Up" in outcomes else 0
        dn_idx   = 1 - up_idx
        return tokens[up_idx], tokens[dn_idx], mkt.get("question","")
    except:
        return None, None, None

# ── Price stream ─────────────────────────────────────────────────
async def stream_price(asset):
    coin_id = ASSETS[asset]["coinbase_id"]
    url     = "wss://advanced-trade-ws.coinbase.com"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                await ws.send(json.dumps({"type":"subscribe","product_ids":[coin_id],"channel":"ticker"}))
                while True:
                    data = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    for event in data.get("events", []):
                        for ticker in event.get("tickers", []):
                            p = float(ticker.get("price", 0))
                            if p > 0: prices[asset] = p
        except: await asyncio.sleep(2)

# ── CLOB stream ───────────────────────────────────────────────────
async def stream_clob(asset, tf_key, token_up, token_down, candle_id, stop_event):
    url   = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    books = {token_up: {"bids":{}, "asks":{}}, token_down: {"bids":{}, "asks":{}}}

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": [token_up, token_down], "markets": []
                }))
                while not stop_event.is_set():
                    try:
                        msg    = await asyncio.wait_for(ws.recv(), timeout=30)
                        events = json.loads(msg)
                        if not isinstance(events, list): events = [events]

                        for event in events:
                            etype    = event.get("event_type","")
                            asset_id = event.get("asset_id","")
                            if asset_id not in books: continue
                            book = books[asset_id]

                            if etype == "book":
                                book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids",[])}
                                book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks",[])}
                            elif etype == "price_change":
                                for ch in event.get("changes",[]):
                                    s, p, sz = ch["side"], ch["price"], float(ch["size"])
                                    if s == "BUY":
                                        if sz == 0: book["bids"].pop(p, None)
                                        else: book["bids"][p] = sz
                                    elif s == "SELL":
                                        if sz == 0: book["asks"].pop(p, None)
                                        else: book["asks"][p] = sz

                            bids = [float(p) for p,s in book["bids"].items() if s > 0]
                            asks = [float(p) for p,s in book["asks"].items() if s > 0]
                            if not bids or not asks: continue

                            best_bid = max(bids)
                            best_ask = min(asks)
                            mid      = (best_bid + best_ask) / 2
                            outcome  = "Up" if asset_id == token_up else "Down"
                            pos_key_prefix = (asset, tf_key, candle_id)

                            # Update candle state
                            state_key = (asset, tf_key, candle_id)
                            if state_key not in candle_state:
                                candle_state[state_key] = {
                                    "start": time.time(),
                                    "open_up": mid if outcome=="Up" else 1-mid,
                                    "prev_res": None, "prev2_res": None
                                }
                            state   = candle_state[state_key]
                            now     = time.time()
                            elapsed = now - state["start"]

                            if outcome == "Up":
                                if elapsed >= 28 and elapsed <= 32 and "mid30_up" not in state:
                                    state["mid30_up"] = mid
                                if elapsed >= 58 and elapsed <= 62 and "mid60_up" not in state:
                                    state["mid60_up"] = mid

                            # Signals — only on Up side update
                            if outcome == "Up":
                                signals = get_signal(state, mid, now)
                                for strat, side in signals:
                                    entry_ask = best_ask if side=="Up" else (1-best_bid)
                                    entry_bid = best_bid if side=="Up" else (1-best_ask)
                                    await try_enter(asset, tf_key, candle_id, strat, side, entry_ask, entry_bid)

                            # Exit checks
                            for pk in list(open_positions.keys()):
                                if pk[:3] != pos_key_prefix: continue
                                pos = open_positions[pk]
                                if pos["side"] != outcome: continue
                                if mid <= CUT_THRESHOLD:
                                    await try_exit(pk, mid, best_bid, "cut_loss")
                                elif mid >= 0.95:
                                    await try_exit(pk, mid, best_bid, "win")
                                elif mid <= 0.05:
                                    await try_exit(pk, mid, best_bid, "loss")

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
            token_up, token_down, question = fetch_tokens(slug)

            if token_up and token_down:
                if ws_task and not ws_task.done():
                    stop_event.set()
                    ws_task.cancel()
                    try: await ws_task
                    except: pass

                async with lock:
                    stale = [k for k in open_positions if k[0]==asset and k[1]==tf_key and k[2]!=candle_id]
                    for k in stale:
                        pos = open_positions.pop(k)
                        account["balance"] += pos["bet"]
                        print(f"  ⏰ Stale position closed: {k}")

                    prev_key  = (asset, tf_key, candle_id-1)
                    prev2_key = (asset, tf_key, candle_id-2)
                    prev_res  = candle_state.get(prev_key, {}).get("resolution")
                    prev2_res = candle_state.get(prev2_key, {}).get("resolution")

                stop_event = asyncio.Event()
                candle_state[(asset, tf_key, candle_id)] = {
                    "start": time.time(), "open_up": 0.5,
                    "prev_res": prev_res, "prev2_res": prev2_res
                }
                ws_task = asyncio.create_task(
                    stream_clob(asset, tf_key, token_up, token_down, candle_id, stop_event)
                )
                last_slug = slug
                print(f"\n[{asset.upper()} {tf_key}] New candle: {question}")

        await asyncio.sleep(1)

# ── Stats printer ─────────────────────────────────────────────────
async def print_stats():
    while True:
        await asyncio.sleep(300)
        conn  = sqlite3.connect(LOG_FILE)
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        wins  = conn.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0").fetchone()[0]
        pnl   = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0]
        strats = conn.execute("""
            SELECT strategy, COUNT(*) n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, SUM(pnl) pnl
            FROM trades GROUP BY strategy ORDER BY pnl DESC
        """).fetchall()
        conn.close()

        wr = 100*wins/total if total > 0 else 0
        print(f"\n{'='*60}")
        print(f"  PAPER TRADER V5 STATS")
        print(f"  Balance: ${account['balance']:,.2f} | Peak: ${account['peak']:,.2f}")
        print(f"  Total P&L: ${pnl:+,.2f} | Trades: {total} | WR: {wr:.1f}%")
        print(f"  Open positions: {len(open_positions)}")
        print(f"\n  {'Strategy':<28} {'N':>5} {'WR':>7} {'PnL':>10}")
        print(f"  {'-'*52}")
        for s, n, w, p in strats:
            print(f"  {s:<28} {n:>5} {100*w//n:>6}% {p:>+10.2f}")
        print(f"{'='*60}\n")

# ── Main ──────────────────────────────────────────────────────────
async def main():
    init_db()
    print(f"\n PAPER TRADER V5 — Fixed")
    print(f"  Balance: ${STARTING_BALANCE:,.2f} | Risk: {RISK_PCT*100:.0f}% | Max positions: {MAX_POSITIONS}")
    print(f"  Entry filter: {MIN_ENTRY} < odds < {MAX_ENTRY} | Each strat fires ONCE per candle")
    print(f"  Assets: BTC, ETH | Timeframes: 5M, 15M")
    print(f"  Strategies: S1/S2/S7 + NS1/NS2/NS3/NS4/NS5\n")

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
