import asyncio
import websockets
import requests
import sqlite3
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────
ORDER_SIZE    = 10.0     # $ per limit order
LIMIT_OFFSET  = 0.05    # place limit this far below current mid
ORDER_INTERVAL = 30     # place new limit orders every 30 seconds
LOG_FILE      = "paper_trades_v6.db"

ASSETS = ["btc", "eth"]
TIMEFRAMES = {"5m": 300, "15m": 900}

# ── Shared state ──────────────────────────────────────────────────
# candle_positions[(asset, tf, candle_id)] = {
#   'up_fills': [(price, shares, ts), ...],
#   'down_fills': [(price, shares, ts), ...],
#   'pending_up': float or None,   # current limit price
#   'pending_down': float or None,
#   'last_order_ts': float,
# }
candle_positions = {}
lock = asyncio.Lock()
stats = {"total_pnl": 0.0, "wins": 0, "losses": 0, "both_leg": 0, "single_leg": 0}

# ── DB ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            asset TEXT,
            timeframe TEXT,
            candle_id INTEGER,
            resolved TEXT,
            n_up_fills INTEGER,
            n_down_fills INTEGER,
            avg_up_price REAL,
            avg_down_price REAL,
            combined_avg REAL,
            total_up_cost REAL,
            total_down_cost REAL,
            payout REAL,
            pnl REAL,
            leg_type TEXT
        )
    """)
    conn.commit()
    conn.close()

def log_candle(asset, tf, candle_id, resolved, up_fills, down_fills, pnl, leg_type):
    avg_up = sum(p for p,s,t in up_fills) / len(up_fills) if up_fills else 0
    avg_down = sum(p for p,s,t in down_fills) / len(down_fills) if down_fills else 0
    combined = avg_up + avg_down
    up_cost = sum(p*s for p,s,t in up_fills)
    down_cost = sum(p*s for p,s,t in down_fills)
    n_up = sum(s for p,s,t in up_fills)
    n_down = sum(s for p,s,t in down_fills)
    payout = max(n_up, n_down) * 1.0

    conn = sqlite3.connect(LOG_FILE)
    conn.execute("""
        INSERT INTO candles (timestamp, asset, timeframe, candle_id, resolved,
            n_up_fills, n_down_fills, avg_up_price, avg_down_price, combined_avg,
            total_up_cost, total_down_cost, payout, pnl, leg_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.now(timezone.utc).isoformat(), asset, tf, candle_id, resolved,
          len(up_fills), len(down_fills), avg_up, avg_down, combined,
          up_cost, down_cost, payout, pnl, leg_type))
    conn.commit()
    conn.close()

# ── Polymarket helpers ────────────────────────────────────────────
def get_slug(asset, tf):
    now = int(time.time())
    interval = TIMEFRAMES[tf]
    bucket = (now // interval) * interval
    return f"{asset}-updown-{tf}-{bucket}"

def fetch_tokens(slug):
    try:
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        resp = requests.get(url, timeout=8).json()
        if not resp: return None, None, None
        mkt = resp[0]
        tokens = json.loads(mkt.get("clobTokenIds", "[]"))
        if len(tokens) < 2: return None, None, None
        outcomes = json.loads(mkt.get("outcomes", '["Up","Down"]'))
        up_idx = outcomes.index("Up") if "Up" in outcomes else 0
        dn_idx = 1 - up_idx
        return tokens[up_idx], tokens[dn_idx], mkt.get("question", "")
    except:
        return None, None, None

# ── Candle resolution ─────────────────────────────────────────────
async def resolve_candle(asset, tf, candle_id, resolved):
    async with lock:
        key = (asset, tf, candle_id)
        if key not in candle_positions:
            return
        pos = candle_positions.pop(key)
        up_fills = pos['up_fills']
        down_fills = pos['down_fills']

        if not up_fills and not down_fills:
            return

        # Calculate PnL
        if up_fills and down_fills:
            # Both legs — guaranteed profit if combined < 1.00
            n_shares = min(
                sum(s for p,s,t in up_fills),
                sum(s for p,s,t in down_fills)
            )
            avg_up = sum(p*s for p,s,t in up_fills) / sum(s for p,s,t in up_fills)
            avg_down = sum(p*s for p,s,t in down_fills) / sum(s for p,s,t in down_fills)
            combined = avg_up + avg_down
            total_cost = (avg_up + avg_down) * n_shares
            payout = n_shares * 1.0
            pnl = payout - total_cost
            leg_type = "BOTH"
            stats["both_leg"] += 1

        elif up_fills:
            # Single leg Up — odds-based exit
            avg_up = sum(p*s for p,s,t in up_fills) / sum(s for p,s,t in up_fills)
            n_shares = sum(s for p,s,t in up_fills)
            cost = avg_up * n_shares
            # In live trading we'd exit when opposite side crosses 0.90
            # Here we just take the resolution outcome
            pnl = (n_shares * 1.0 - cost) if resolved == 'Up' else -cost
            down_fills = []
            leg_type = "SINGLE_UP"
            stats["single_leg"] += 1

        else:
            # Single leg Down — odds-based exit
            avg_down = sum(p*s for p,s,t in down_fills) / sum(s for p,s,t in down_fills)
            n_shares = sum(s for p,s,t in down_fills)
            cost = avg_down * n_shares
            pnl = (n_shares * 1.0 - cost) if resolved == 'Down' else -cost
            up_fills = []
            leg_type = "SINGLE_DOWN"
            stats["single_leg"] += 1

        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        log_candle(asset, tf, candle_id, resolved, up_fills, down_fills, pnl, leg_type)

        avg_up_p = sum(p*s for p,s,t in up_fills)/sum(s for p,s,t in up_fills) if up_fills else 0
        avg_dn_p = sum(p*s for p,s,t in down_fills)/sum(s for p,s,t in down_fills) if down_fills else 0
        combined_str = f"{avg_up_p+avg_dn_p:.3f}" if up_fills and down_fills else "N/A"
        icon = "✅" if pnl > 0 else "❌"
        print(f"  {icon} [{asset.upper()} {tf}] {leg_type} | resolved={resolved} | "
              f"up_fills={len(up_fills)} dn_fills={len(down_fills)} | "
              f"combined={combined_str} | pnl=${pnl:+.2f} | total=${stats['total_pnl']:+.2f}")

# ── CLOB stream ───────────────────────────────────────────────────
async def stream_clob(asset, tf_key, token_up, token_down, candle_id, stop_event):
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    books = {token_up: {"bids": {}, "asks": {}}, token_down: {"bids": {}, "asks": {}}}
    interval = TIMEFRAMES[tf_key]

    # Init candle position
    async with lock:
        key = (asset, tf_key, candle_id)
        if key not in candle_positions:
            candle_positions[key] = {
                'up_fills': [], 'down_fills': [],
                'pending_up': None, 'pending_down': None,
                'last_order_ts': 0,
                'up_low': 1.0, 'down_low': 1.0,
            }

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": [token_up, token_down], "markets": []
                }))
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        events = json.loads(msg)
                        if not isinstance(events, list):
                            events = [events]

                        for event in events:
                            etype = event.get("event_type", "")
                            asset_id = event.get("asset_id", "")
                            if asset_id not in books:
                                continue
                            book = books[asset_id]

                            if etype == "book":
                                book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids", [])}
                                book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks", [])}
                            elif etype == "price_change":
                                for ch in event.get("changes", []):
                                    s, p, sz = ch["side"], ch["price"], float(ch["size"])
                                    if s == "BUY":
                                        if sz == 0: book["bids"].pop(p, None)
                                        else: book["bids"][p] = sz
                                    elif s == "SELL":
                                        if sz == 0: book["asks"].pop(p, None)
                                        else: book["asks"][p] = sz

                            bids = [float(p) for p, s in book["bids"].items() if s > 0]
                            asks = [float(p) for p, s in book["asks"].items() if s > 0]
                            if not bids or not asks:
                                continue

                            best_bid = max(bids)
                            best_ask = min(asks)
                            mid = (best_bid + best_ask) / 2
                            outcome = "Up" if asset_id == token_up else "Down"
                            now = time.time()

                            async with lock:
                                key = (asset, tf_key, candle_id)
                                if key not in candle_positions:
                                    continue
                                pos = candle_positions[key]

                                # Track lows for resolution detection
                                if outcome == "Up":
                                    if mid <= 0.10:
                                        pos['up_low'] = mid
                                else:
                                    if mid <= 0.10:
                                        pos['down_low'] = mid

                                # Check if candle is nearly resolved — skip orders
                                if mid >= 0.92 or mid <= 0.08:
                                    # Check resolution
                                    if mid >= 0.92:
                                        resolved = outcome
                                    else:
                                        resolved = "Up" if outcome == "Down" else "Down"
                                    # Will be resolved by candle end
                                    continue

                                # Place new limit orders every ORDER_INTERVAL seconds
                                if now - pos['last_order_ts'] >= ORDER_INTERVAL:
                                    limit_price = round(mid - LIMIT_OFFSET, 3)
                                    if limit_price > 0.05:
                                        if outcome == "Up":
                                            pos['pending_up'] = limit_price
                                        else:
                                            pos['pending_down'] = limit_price
                                        pos['last_order_ts'] = now

                                # Check if pending limit got filled
                                # Fill happens when ask drops to our limit price
                                if outcome == "Up" and pos['pending_up']:
                                    if best_ask <= pos['pending_up']:
                                        fill_price = pos['pending_up']
                                        shares = ORDER_SIZE / fill_price
                                        pos['up_fills'].append((fill_price, shares, now))
                                        pos['pending_up'] = None
                                        print(f"  📗 [{asset.upper()} {tf_key}] UP fill @ {fill_price:.3f} | "
                                              f"shares={shares:.1f} | fills={len(pos['up_fills'])}up/{len(pos['down_fills'])}dn")

                                if outcome == "Down" and pos['pending_down']:
                                    if best_ask <= pos['pending_down']:
                                        fill_price = pos['pending_down']
                                        shares = ORDER_SIZE / fill_price
                                        pos['down_fills'].append((fill_price, shares, now))
                                        pos['pending_down'] = None
                                        print(f"  📕 [{asset.upper()} {tf_key}] DN fill @ {fill_price:.3f} | "
                                              f"shares={shares:.1f} | fills={len(pos['up_fills'])}up/{len(pos['down_fills'])}dn")

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
    last_candle_id = None

    while True:
        slug = get_slug(asset, tf_key)
        if slug != last_slug:
            interval = TIMEFRAMES[tf_key]
            candle_id = int(time.time()) // interval
            token_up, token_down, question = fetch_tokens(slug)

            if token_up and token_down:
                # Resolve previous candle
                if last_candle_id and last_candle_id != candle_id:
                    prev_key = (asset, tf_key, last_candle_id)
                    resolved = 'Unknown'
                    async with lock:
                        if prev_key in candle_positions:
                            pos = candle_positions[prev_key]
                            up_low = pos.get('up_low', 0.5)
                            down_low = pos.get('down_low', 0.5)
                            if up_low <= 0.10:
                                resolved = 'Down'
                            elif down_low <= 0.10:
                                resolved = 'Up'
                    await resolve_candle(asset, tf_key, last_candle_id, resolved)

                if ws_task and not ws_task.done():
                    stop_event.set()
                    ws_task.cancel()
                    try:
                        await ws_task
                    except:
                        pass

                stop_event = asyncio.Event()
                ws_task = asyncio.create_task(
                    stream_clob(asset, tf_key, token_up, token_down, candle_id, stop_event)
                )
                last_slug = slug
                last_candle_id = candle_id
                print(f"\n[{asset.upper()} {tf_key}] New candle: {question}")

        await asyncio.sleep(1)

# ── Stats printer ─────────────────────────────────────────────────
async def print_stats():
    while True:
        await asyncio.sleep(300)
        conn = sqlite3.connect(LOG_FILE)
        rows = conn.execute("""
            SELECT leg_type, COUNT(*), AVG(combined_avg), SUM(pnl),
                   AVG(n_up_fills), AVG(n_down_fills)
            FROM candles GROUP BY leg_type
        """).fetchall()
        total = conn.execute("SELECT COUNT(*), SUM(pnl) FROM candles").fetchone()
        conn.close()

        print(f"\n{'='*65}")
        print(f"  PAPER TRADER V6 — Limit Order Market Making")
        print(f"  Order size: ${ORDER_SIZE} | Limit offset: -{LIMIT_OFFSET} | Interval: {ORDER_INTERVAL}s")
        print(f"  Total PnL: ${stats['total_pnl']:+.2f} | Candles: {total[0]} | W/L: {stats['wins']}/{stats['losses']}")
        print(f"  Both-leg: {stats['both_leg']} | Single-leg: {stats['single_leg']}")
        print(f"\n  {'Type':<15} {'N':>5} {'Avg Comb':>10} {'Total PnL':>12} {'Avg Up Fills':>13} {'Avg Dn Fills':>13}")
        print(f"  {'-'*70}")
        for leg_type, n, avg_comb, total_pnl, avg_up, avg_dn in rows:
            comb_str = f"{avg_comb:.3f}" if avg_comb and avg_comb < 2 else "N/A"
            print(f"  {leg_type:<15} {n:>5} {comb_str:>10} {total_pnl:>+12.2f} {avg_up:>13.1f} {avg_dn:>13.1f}")
        print(f"{'='*65}\n")

# ── Main ──────────────────────────────────────────────────────────
async def main():
    init_db()
    print(f"\n PAPER TRADER V6 — Limit Order Market Making")
    print(f"  Order: ${ORDER_SIZE} | Offset: -{LIMIT_OFFSET} below mid | Every {ORDER_INTERVAL}s")
    print(f"  Assets: BTC, ETH | Timeframes: 5M, 15M | Unlimited positions\n")

    tasks = []
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            tasks.append(manage_market(asset, tf))
    tasks.append(print_stats())
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"[CRASH] {e} — restarting in 5s...")
            time.sleep(5)
