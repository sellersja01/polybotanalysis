"""
paper_test.py — Latency arb bot (paper + live)
================================================
Monitors Binance BTC + ETH price. When either moves and Poly hasn't
repriced yet, buys the correct side at stale odds.

Usage:
    python paper_test.py                          # paper (default)
    $env:DRY_RUN="false"; python paper_test.py    # live trading
    $env:TRADE_USD="1.0"; python paper_test.py    # USDC per live trade
"""
import asyncio
import json
import math
import os
import time
import aiohttp
import websockets
from collections import deque
from datetime import datetime

DRY_RUN   = os.environ.get("DRY_RUN", "true").lower() != "false"
SHARES    = int(os.environ.get("SHARES", "100"))    # paper sim shares
TRADE_USD = float(os.environ.get("TRADE_USD", "1.0"))  # USDC per live trade
MOVE_THRESH = float(os.environ.get("MOVE_THRESH", "0.07"))

LOOKBACK            = 15
COOLDOWN            = 2
MIN_ENTRY_PRICE     = 0.25
MAX_ENTRY_PRICE     = 0.75
MAX_TRADES_PER_CANDLE = 10
MAX_OPEN            = 5   # across all assets

# ── Asset definitions ────────────────────────────────────────────────────────
ASSET_CONFIGS = [
    {"label": "BTC", "ws_symbol": "btcusdt@trade", "slug_prefix": "btc"},
    {"label": "ETH", "ws_symbol": "ethusdt@trade", "slug_prefix": "eth"},
]


def make_asset_state(cfg):
    return {
        **cfg,
        "price":            0.0,
        "buffer":           deque(maxlen=5000),
        "btc_ticks":        0,
        "poly_up_bid":      0.0,
        "poly_up_ask":      0.0,
        "poly_dn_bid":      0.0,
        "poly_dn_ask":      0.0,
        "poly_ts":          0.0,
        "poly_ticks":       0,
        "up_token":         None,
        "dn_token":         None,
        "question":         None,
        "token_side":       {},
        "current_candle_ts": 0,
        "candle_trade_count": 0,
        "last_signal":      0.0,
        "last_move_pct":    0.0,
    }


def poly_fee(price):
    return price * 0.25 * (price * (1 - price)) ** 2


# ── Market lookup ────────────────────────────────────────────────────────────
async def get_market(asset):
    prefix = asset["slug_prefix"]
    now = time.time()
    async with aiohttp.ClientSession() as s:
        for offset in range(5):
            candle_ts = int(now // 300) * 300 - (offset * 300)
            slug = f"{prefix}-updown-5m-{candle_ts}"
            async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}") as r:
                data = await r.json()
            if data:
                mkt = data[0].get("markets", [{}])[0]
                tokens = json.loads(mkt.get("clobTokenIds", "[]"))
                outcomes = json.loads(mkt.get("outcomes", "[]"))
                if len(tokens) >= 2:
                    up_idx = 0 if outcomes[0] == "Up" else 1
                    dn_idx = 1 - up_idx
                    question = mkt.get("question") or data[0].get("title", slug)
                    return tokens[up_idx], tokens[dn_idx], question, candle_ts
    return None, None, None, None


# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    mode_str = "DRY RUN (paper)" if DRY_RUN else "*** LIVE TRADING ***"
    print("=" * 65)
    print(f"  LATENCY ARB BOT -- {mode_str}")
    print(f"  Assets: BTC+ETH 5m | MOVE_THRESH={MOVE_THRESH}% | MAX_ENTRY={MAX_ENTRY_PRICE}")
    print(f"  {'TRADE_USD=' + str(TRADE_USD) if not DRY_RUN else 'SHARES=' + str(SHARES)}")
    print("=" * 65)

    poly_client = None
    poly_session = None
    if not DRY_RUN:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from polymarket_client import PolymarketClient
        poly_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=5, connect=2),
        )
        poly_client = PolymarketClient(session=poly_session)
        print("Live client initialized.\n")

    # Shared state
    open_trades   = []
    closed_trades = []
    total_pnl     = 0.0

    # Per-asset state
    assets = [make_asset_state(cfg) for cfg in ASSET_CONFIGS]

    # ── Trade resolution ─────────────────────────────────────────────────────
    async def resolve_open_trades(asset):
        nonlocal total_pnl
        now = time.time()
        still_open = []
        for trade in open_trades:
            if trade["label"] != asset["label"]:
                still_open.append(trade)
                continue

            age = now - trade["entry_ts"]
            if trade["side"] == "up":
                current_mid = (asset["poly_up_bid"] + asset["poly_up_ask"]) / 2 \
                    if asset["poly_up_bid"] and asset["poly_up_ask"] else 0
            else:
                current_mid = (asset["poly_dn_bid"] + asset["poly_dn_ask"]) / 2 \
                    if asset["poly_dn_bid"] and asset["poly_dn_ask"] else 0

            profit_per_share = current_mid - trade["entry_price"] - trade["fee"]

            if current_mid > 0 and age >= 4 and (profit_per_share >= 0.02 or age >= 60):
                trade["exit_ts"]   = now
                trade["hold_time"] = age

                if not DRY_RUN and poly_client and trade.get("shares_bought", 0) > 0:
                    try:
                        r = await poly_client.sell_order(trade["token_id"], trade["shares_bought"])
                        usdc_received = float(r.get("takingAmount", 0))
                        usdc_spent    = float(trade.get("usdc_spent", 0))
                        real_pnl      = usdc_received - usdc_spent
                        total_pnl    += real_pnl
                        trade["pnl"]  = real_pnl
                        trade["exit_price"] = current_mid
                        closed_trades.append(trade)
                        emoji = "W" if real_pnl > 0 else "L"
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(
                            f"  [{ts}] CLOSE #{len(closed_trades)} {emoji} [{asset['label']}] | "
                            f"{trade['side'].upper()} bought=${usdc_spent:.2f} sold=${usdc_received:.2f} | "
                            f"REAL pnl=${real_pnl:+.3f} | hold={age:.1f}s | "
                            f"total=${total_pnl:+.3f} | "
                            f"WR={sum(1 for t in closed_trades if t['pnl']>0)}/{len(closed_trades)}",
                            flush=True
                        )
                    except Exception as e:
                        print(f"  SELL ERROR [{asset['label']}]: {e}", flush=True)
                        still_open.append(trade)
                        continue
                else:
                    pnl = profit_per_share * SHARES
                    total_pnl += pnl
                    trade["pnl"]        = pnl
                    trade["exit_price"] = current_mid
                    closed_trades.append(trade)
                    emoji = "W" if pnl > 0 else "L"
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"  [{ts}] CLOSE #{len(closed_trades)} {emoji} [PAPER/{asset['label']}] | "
                        f"{trade['side'].upper()} entry={trade['entry_price']:.3f} exit={current_mid:.3f} | "
                        f"pnl=${pnl:+.2f} | hold={age:.1f}s | total=${total_pnl:+.2f} | "
                        f"WR={sum(1 for t in closed_trades if t['pnl']>0)}/{len(closed_trades)}",
                        flush=True
                    )
            else:
                still_open.append(trade)

        open_trades.clear()
        open_trades.extend(still_open)

    # ── Signal check ─────────────────────────────────────────────────────────
    async def check_move(asset, move_pct, price, now):
        if asset["poly_up_ask"] <= 0 or asset["poly_dn_ask"] <= 0:
            return
        if now - asset["last_signal"] < COOLDOWN:
            return
        if len(open_trades) >= MAX_OPEN:
            return
        if asset["candle_trade_count"] >= MAX_TRADES_PER_CANDLE:
            return

        direction = "UP" if move_pct > 0 else "DOWN"

        if direction == "UP":
            current_mid = (asset["poly_up_bid"] + asset["poly_up_ask"]) / 2 \
                if asset["poly_up_bid"] else asset["poly_up_ask"]
            stale       = current_mid < 0.55
            entry_price = asset["poly_up_ask"]
            side        = "up"
        else:
            current_mid = (asset["poly_dn_bid"] + asset["poly_dn_ask"]) / 2 \
                if asset["poly_dn_bid"] else asset["poly_dn_ask"]
            stale       = current_mid < 0.55
            entry_price = asset["poly_dn_ask"]
            side        = "down"

        if not stale:
            return
        if entry_price <= 0 or entry_price < MIN_ENTRY_PRICE or entry_price >= MAX_ENTRY_PRICE:
            return

        asset["last_signal"] = now
        asset["candle_trade_count"] += 1
        fee = poly_fee(entry_price)

        trade = {
            "id":           len(closed_trades) + len(open_trades) + 1,
            "label":        asset["label"],
            "side":         side,
            "direction":    direction,
            "entry_price":  entry_price,
            "entry_mid":    current_mid,
            "fee":          fee,
            "asset_price":  price,
            "move_pct":     move_pct,
            "entry_ts":     now,
            "exit_price":   None,
            "exit_ts":      None,
            "pnl":          None,
            "hold_time":    None,
            "token_id":     asset["up_token"] if side == "up" else asset["dn_token"],
            "shares_bought": 0.0,
        }
        open_trades.append(trade)

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(
            f"\n  >>> [{ts}] TRADE #{trade['id']} [{asset['label']}] | "
            f"{direction} {move_pct:+.3f}% @ ${price:,.0f} | "
            f"BUY {side.upper()} @ {entry_price:.3f} (mid={current_mid:.3f}) | "
            f"fee={fee:.4f} | open={len(open_trades)} | {'DRY' if DRY_RUN else 'LIVE'}",
            flush=True
        )

        if not DRY_RUN and poly_client:
            live_size = math.ceil(max(1.0, TRADE_USD) / entry_price)
            try:
                t0 = time.perf_counter_ns()
                r  = await poly_client.place_order(trade["token_id"], entry_price, live_size)
                ms = (time.perf_counter_ns() - t0) / 1e6
                shares_got = float(r.get("takingAmount", 0))
                usdc_spent = float(r.get("makingAmount", 0))
                trade["shares_bought"] = shares_got
                trade["usdc_spent"]    = usdc_spent
                print(f"  BUY: {ms:.0f}ms | {shares_got:.4f} shares | spent=${usdc_spent:.4f} | status={r.get('status')}", flush=True)
            except Exception as e:
                print(f"  ORDER ERROR [{asset['label']}]: {e}", flush=True)

    # ── Binance feed ─────────────────────────────────────────────────────────
    async def binance_feed(asset):
        while True:
            try:
                url = f"wss://stream.binance.com:9443/ws/{asset['ws_symbol']}"
                async with websockets.connect(url, ping_interval=20) as ws:
                    print(f"[Binance/{asset['label']}] Connected", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        p = float(msg.get("p", 0))
                        if p <= 0:
                            continue
                        asset["price"] = p
                        now = time.time()
                        asset["buffer"].append((now, p))
                        asset["btc_ticks"] += 1

                        cutoff = now - LOOKBACK
                        old_p = None
                        for ts, pr in asset["buffer"]:
                            if ts <= cutoff:
                                old_p = pr
                            else:
                                break
                        if old_p and old_p > 0:
                            move = (p - old_p) / old_p * 100
                            asset["last_move_pct"] = move
                            if abs(move) >= MOVE_THRESH:
                                await check_move(asset, move, p, now)

                        if open_trades and asset["btc_ticks"] % 50 == 0:
                            await resolve_open_trades(asset)
            except Exception as e:
                print(f"[Binance/{asset['label']}] Error: {e} -- reconnecting", flush=True)
                await asyncio.sleep(2)

    # ── Polymarket feed ───────────────────────────────────────────────────────
    async def poly_feed(asset):
        while True:
            ut, dt, q, cts = await get_market(asset)
            if not ut:
                print(f"[Poly/{asset['label']}] No market found, retrying in 30s...", flush=True)
                await asyncio.sleep(30)
                continue

            asset["up_token"]         = ut
            asset["dn_token"]         = dt
            asset["question"]         = q
            asset["current_candle_ts"] = cts
            asset["token_side"]       = {ut: "up", dt: "down"}
            asset["poly_up_bid"]      = 0.0
            asset["poly_up_ask"]      = 0.0
            asset["poly_dn_bid"]      = 0.0
            asset["poly_dn_ask"]      = 0.0

            try:
                async with websockets.connect(
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
                ) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": [ut, dt],
                        "type": "market",
                    }))
                    print(f"[Poly/{asset['label']}] Subscribed: {q}", flush=True)

                    candle_end = cts + 300

                    async for raw in ws:
                        if time.time() > candle_end + 10:
                            await resolve_open_trades(asset)
                            asset["candle_trade_count"] = 0
                            print(f"\n[Poly/{asset['label']}] Candle ended, rolling over...", flush=True)
                            break

                        msg   = json.loads(raw)
                        items = msg if isinstance(msg, list) else [msg]
                        for item in items:
                            aid = item.get("asset_id")

                            if aid and aid in asset["token_side"] and "bids" in item:
                                side = asset["token_side"][aid]
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                bb = max((float(b["price"]) for b in bids), default=0) if bids else 0
                                ba = min((float(a["price"]) for a in asks), default=0) if asks else 0
                                # Only overwrite prices if non-zero — empty book on candle open
                                # would otherwise reset previously-set prices to 0
                                if side == "up":
                                    if bb > 0: asset["poly_up_bid"] = bb
                                    if ba > 0: asset["poly_up_ask"] = ba
                                else:
                                    if bb > 0: asset["poly_dn_bid"] = bb
                                    if ba > 0: asset["poly_dn_ask"] = ba
                                asset["poly_ts"] = time.time()
                                asset["poly_ticks"] += 1
                                continue

                            for ch in item.get("price_changes", []):
                                ch_aid = ch.get("asset_id")
                                if ch_aid not in asset["token_side"]:
                                    continue
                                side  = asset["token_side"][ch_aid]
                                price = float(ch.get("price", 0))
                                if ch.get("side") == "BUY":
                                    if side == "up":
                                        asset["poly_up_bid"] = price
                                    else:
                                        asset["poly_dn_bid"] = price
                                elif ch.get("side") == "SELL":
                                    if side == "up":
                                        asset["poly_up_ask"] = price
                                    else:
                                        asset["poly_dn_ask"] = price
                                asset["poly_ts"] = time.time()
                                asset["poly_ticks"] += 1

            except Exception as e:
                print(f"[Poly/{asset['label']}] Error: {e} -- reconnecting", flush=True)
                await asyncio.sleep(2)

    # ── Status printer ────────────────────────────────────────────────────────
    async def status_printer():
        while True:
            await asyncio.sleep(20)
            ts   = datetime.now().strftime("%H:%M:%S")
            wins = sum(1 for t in closed_trades if t["pnl"] and t["pnl"] > 0)
            n    = len(closed_trades)
            wr   = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
            lines = [f"  [{ts}] WR={wr} PnL=${total_pnl:+.2f} open={len(open_trades)}"]
            for a in assets:
                up_mid = (a["poly_up_bid"] + a["poly_up_ask"]) / 2 \
                    if a["poly_up_bid"] and a["poly_up_ask"] else 0
                dn_mid = (a["poly_dn_bid"] + a["poly_dn_ask"]) / 2 \
                    if a["poly_dn_bid"] and a["poly_dn_ask"] else 0
                lines.append(
                    f"    {a['label']}: ${a['price']:,.0f} | Up={up_mid:.3f} Dn={dn_mid:.3f} | "
                    f"move={a['last_move_pct']:+.3f}% (need±{MOVE_THRESH}%) | "
                    f"ticks={a['btc_ticks']:,}/{a['poly_ticks']:,}"
                )
            print("\n".join(lines), flush=True)

    print("\nStarting...\n", flush=True)
    try:
        coros = []
        for a in assets:
            coros.append(binance_feed(a))
            coros.append(poly_feed(a))
        coros.append(status_printer())
        await asyncio.gather(*coros)
    except Exception as e:
        print(f"\nFATAL: {e} — restarting in 10s...", flush=True)
        await asyncio.sleep(10)
        await main()
    finally:
        if poly_session and not poly_session.closed:
            await poly_session.close()


if __name__ == "__main__":
    asyncio.run(main())
