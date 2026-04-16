"""
Paper Trader — Layered Entry Strategy (BTC 5m)

Rules:
  - Entry levels: [0.45, 0.40, 0.35, 0.30, 0.25]
  - When EITHER side's mid crosses a level for the first time this candle,
    buy BOTH sides at current ask (paper only)
  - Each level = SHARES_PER_LEVEL shares of each side
  - Exit loser when its mid drops to EXIT_MID (sell at bid = 2*mid - ask)
  - Hold winner to candle resolution ($1.00 payout)
  - New fee formula: 0.072 * price * (price*(1-price))
"""

import asyncio
import websockets
import requests
import json
import time
from datetime import datetime, timezone

# ── Config ───────────────────────────────────────────────────────────────────
ENTRY_LEVELS      = [0.45, 0.40, 0.35, 0.30, 0.25]
SHARES_PER_LEVEL  = 10
EXIT_MID          = 0.20
CANDLE_SECS       = 300   # 5m

# ── Fee ──────────────────────────────────────────────────────────────────────
def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))

# ── Polymarket helpers ────────────────────────────────────────────────────────
def current_slug():
    ts = (int(time.time()) // CANDLE_SECS) * CANDLE_SECS
    return f"btc-updown-5m-{ts}", ts

def fetch_tokens(slug):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10)
        data = r.json()
        if data:
            market = data[0]["markets"][0]
            tokens = json.loads(market.get("clobTokenIds", "[]"))
            if len(tokens) >= 2:
                return tokens[0], tokens[1], market.get("question", "")
    except Exception as e:
        print(f"[fetch_tokens] error: {e}")
    return None, None, ""

# ── State ─────────────────────────────────────────────────────────────────────
up_bid = up_ask = dn_bid = dn_ask = 0.0
prices_fresh = False   # True once we get a valid book snapshot for current candle
candle_start_ts = 0
token_up = token_down = None
question = ""

# Per-candle tracking
levels_triggered  = set()
prev_up_mid       = None   # last known mid — used to detect level crossovers
prev_dn_mid       = None
up_entries        = []   # list of (ask_paid, fee_paid, shares)
dn_entries        = []
up_exit_bid       = None
dn_exit_bid       = None
loser_sold        = False

# Session totals
total_candles = 0
total_wins    = 0
session_pnl   = 0.0

def up_mid():
    if up_bid and up_ask: return (up_bid + up_ask) / 2
    return up_ask or 0.0

def dn_mid():
    if dn_bid and dn_ask: return (dn_bid + dn_ask) / 2
    return dn_ask or 0.0

def reset_candle():
    global levels_triggered, up_entries, dn_entries
    global up_exit_bid, dn_exit_bid, loser_sold
    global up_bid, up_ask, dn_bid, dn_ask, prices_fresh
    global prev_up_mid, prev_dn_mid
    levels_triggered = set()
    up_entries       = []
    dn_entries       = []
    up_exit_bid      = None
    dn_exit_bid      = None
    loser_sold       = False
    # Reset prices so stale candle-end prices don't trigger entries
    up_bid = up_ask = dn_bid = dn_ask = 0.0
    prices_fresh = False
    prev_up_mid = None
    prev_dn_mid = None

def check_entries():
    """Fire entries only when a mid CROSSES through a level (was above, now below)."""
    global prices_fresh, prev_up_mid, prev_dn_mid
    um = up_mid()
    dm = dn_mid()
    if not um or not dm or not dn_ask or not up_ask: return

    # Don't fire until both sides look like a live candle (mid 0.10-0.90)
    if not prices_fresh:
        if 0.10 <= um <= 0.90 and 0.10 <= dm <= 0.90:
            prices_fresh = True
            # Set prev mids to current so first tick doesn't immediately cross
            prev_up_mid = um
            prev_dn_mid = dm
        return  # always return here — wait for next tick to detect actual crossover

    if prev_up_mid is None or prev_dn_mid is None:
        prev_up_mid = um
        prev_dn_mid = dm
        return

    for lvl in ENTRY_LEVELS:
        if lvl in levels_triggered: continue
        # Up side crosses DOWN through level (was above lvl, now at or below)
        up_crossed = prev_up_mid > lvl >= um
        # Down side crosses DOWN through level
        dn_crossed = prev_dn_mid > lvl >= dm

        if up_crossed or dn_crossed:
            levels_triggered.add(lvl)
            up_f = fee(SHARES_PER_LEVEL, up_ask)
            dn_f = fee(SHARES_PER_LEVEL, dn_ask)
            up_entries.append((up_ask, up_f))
            dn_entries.append((dn_ask, dn_f))
            elapsed = int(time.time() - candle_start_ts)
            print(f"  [ENTRY lvl={lvl}] t+{elapsed}s | "
                  f"Up {SHARES_PER_LEVEL}sh@{up_ask:.3f} + Dn {SHARES_PER_LEVEL}sh@{dn_ask:.3f} | "
                  f"cost=${SHARES_PER_LEVEL*(up_ask+dn_ask):.2f}")

    prev_up_mid = um
    prev_dn_mid = dm

def check_exits():
    """Sell the loser side when its mid drops to EXIT_MID."""
    global up_exit_bid, dn_exit_bid, loser_sold
    if loser_sold or not up_entries or not prices_fresh: return

    um = up_mid()
    dm = dn_mid()

    if up_exit_bid is None and um <= EXIT_MID and up_ask > 0:
        up_exit_bid = max(0.0, 2 * um - up_ask)
        elapsed = int(time.time() - candle_start_ts)
        total_up_shares = len(up_entries) * SHARES_PER_LEVEL
        recovered = up_exit_bid * total_up_shares
        print(f"  [SELL UP] t+{elapsed}s | mid={um:.3f} bid={up_exit_bid:.3f} | "
              f"recovered ${recovered:.2f}")
        loser_sold = True

    elif dn_exit_bid is None and dm <= EXIT_MID and dn_ask > 0:
        dn_exit_bid = max(0.0, 2 * dm - dn_ask)
        elapsed = int(time.time() - candle_start_ts)
        total_dn_shares = len(dn_entries) * SHARES_PER_LEVEL
        recovered = dn_exit_bid * total_dn_shares
        print(f"  [SELL DN] t+{elapsed}s | mid={dm:.3f} bid={dn_exit_bid:.3f} | "
              f"recovered ${recovered:.2f}")
        loser_sold = True

def resolve_candle():
    global total_candles, total_wins, session_pnl

    if not up_entries:
        print("  [RESOLVE] No entries this candle — skip")
        return

    um = up_mid()
    dm = dn_mid()
    winner = 'Up' if um >= dm else 'Down'

    n = len(up_entries)
    total_shares   = n * SHARES_PER_LEVEL
    total_up_cost  = sum(a for a, f in up_entries) * SHARES_PER_LEVEL
    total_dn_cost  = sum(a for a, f in dn_entries) * SHARES_PER_LEVEL
    total_up_fees  = sum(f for a, f in up_entries)
    total_dn_fees  = sum(f for a, f in dn_entries)
    total_cost     = total_up_cost + total_dn_cost + total_up_fees + total_dn_fees

    if winner == 'Up':
        win_pnl  = (1.0 * total_shares) - total_up_cost - total_up_fees
        if up_exit_bid is not None:
            # Up was sold early (shouldn't happen often, but handle it)
            win_pnl = (up_exit_bid * total_shares) - total_up_cost - total_up_fees
        lose_pnl = (dn_exit_bid * total_shares if dn_exit_bid is not None else 0.0) - total_dn_cost - total_dn_fees
    else:
        win_pnl  = (1.0 * total_shares) - total_dn_cost - total_dn_fees
        if dn_exit_bid is not None:
            win_pnl = (dn_exit_bid * total_shares) - total_dn_cost - total_dn_fees
        lose_pnl = (up_exit_bid * total_shares if up_exit_bid is not None else 0.0) - total_up_cost - total_up_fees

    candle_pnl = win_pnl + lose_pnl
    session_pnl += candle_pnl
    total_candles += 1
    if candle_pnl > 0:
        total_wins += 1

    result = "WIN" if candle_pnl > 0 else "LOSS"
    wr = 100 * total_wins / total_candles

    print()
    print(f"  ={'='*55}")
    print(f"  CANDLE RESOLVED | Winner: {winner} | {result}")
    print(f"  Levels hit:   {sorted(levels_triggered)}")
    print(f"  Entries:      {n} per side | {total_shares} shares each")
    print(f"  Total cost:   ${total_cost:.2f}")
    print(f"  Candle PnL:   ${candle_pnl:+.2f}")
    print(f"  Session:      {total_candles} candles | WR {wr:.1f}% | PnL ${session_pnl:+.2f}")
    print(f"  ={'='*55}")
    print()

# ── Book tracking ─────────────────────────────────────────────────────────────
async def stream_clob(stop_event):
    global up_bid, up_ask, dn_bid, dn_ask

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    books = {
        token_up:   {"bids": {}, "asks": {}},
        token_down: {"bids": {}, "asks": {}},
    }

    while not stop_event.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "auth": {}, "type": "subscribe",
                    "assets_ids": [token_up, token_down], "markets": []
                }))
                print(f"[WS] Connected: {question}")

                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        events = json.loads(msg)
                        if not isinstance(events, list): events = [events]

                        for event in events:
                            etype    = event.get("event_type", "")
                            asset_id = event.get("asset_id", "")
                            if asset_id not in books: continue

                            book = books[asset_id]
                            if etype == "book":
                                book["bids"] = {b["price"]: float(b["size"]) for b in event.get("bids", [])}
                                book["asks"] = {a["price"]: float(a["size"]) for a in event.get("asks", [])}
                            elif etype == "price_change":
                                for ch in event.get("changes", []):
                                    p, sz = ch["price"], float(ch["size"])
                                    d = book["bids"] if ch["side"] == "BUY" else book["asks"]
                                    if sz == 0: d.pop(p, None)
                                    else:       d[p] = sz

                            bids = [float(p) for p, s in book["bids"].items() if s > 0]
                            asks = [float(p) for p, s in book["asks"].items() if s > 0]
                            if not bids or not asks: continue

                            best_bid = max(bids)
                            best_ask = min(asks)

                            if asset_id == token_up:
                                if best_ask > 0: up_bid, up_ask = best_bid, best_ask
                            else:
                                if best_ask > 0: dn_bid, dn_ask = best_bid, best_ask

                            check_entries()
                            check_exits()

                    except asyncio.TimeoutError:
                        await ws.ping()

        except Exception as e:
            if not stop_event.is_set():
                print(f"[WS] Error: {e} — reconnecting in 5s...")
                await asyncio.sleep(5)

# ── Status printer ────────────────────────────────────────────────────────────
async def status_loop():
    while True:
        await asyncio.sleep(15)
        if not candle_start_ts: continue
        elapsed = int(time.time() - candle_start_ts)
        remaining = CANDLE_SECS - elapsed
        um = up_mid()
        dm = dn_mid()
        n_entries = len(up_entries)
        cost = sum(a for a, f in up_entries) * SHARES_PER_LEVEL + \
               sum(a for a, f in dn_entries) * SHARES_PER_LEVEL if up_entries else 0
        lvls = sorted(levels_triggered) if levels_triggered else []
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
              f"t+{elapsed}s ({remaining}s left) | "
              f"Up={um:.3f} Dn={dm:.3f} | "
              f"lvls={lvls} entries={n_entries} cost=${cost:.2f} | "
              f"session PnL=${session_pnl:+.2f}")

# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    global token_up, token_down, question, candle_start_ts

    print("\nPaper Layered Entry — BTC 5m")
    print(f"Levels: {ENTRY_LEVELS} | {SHARES_PER_LEVEL} shares/level | Exit loser at mid={EXIT_MID}\n")

    ws_task    = None
    stop_event = asyncio.Event()
    last_slug  = None

    asyncio.create_task(status_loop())

    while True:
        slug, cstart = current_slug()

        if slug != last_slug:
            # New candle — resolve old one first
            if last_slug is not None:
                resolve_candle()
                reset_candle()

            print(f"\n[CANDLE] {slug}")
            tu, td, q = fetch_tokens(slug)

            if tu and td:
                token_up   = tu
                token_down = td
                question   = q
                candle_start_ts = cstart

                # Cancel old WS
                if ws_task and not ws_task.done():
                    stop_event.set()
                    ws_task.cancel()
                    try: await ws_task
                    except: pass

                stop_event = asyncio.Event()
                ws_task = asyncio.create_task(stream_clob(stop_event))
                last_slug = slug
            else:
                print(f"[CANDLE] No market found for {slug} — retrying...")

        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
