"""
paper_test.py — Live paper trading test for latency arb
========================================================
Runs in terminal. Connects to Binance + Polymarket (public, no auth).
When lag is detected, records a paper trade and tracks PnL.

Usage: python paper_test.py
"""
import asyncio
import json
import time
import aiohttp
import websockets
from collections import deque
from datetime import datetime

SHARES = 100  # simulated shares per trade

# ── Fetch current BTC market ─────────────────────────────────────────────────
async def get_btc_market():
    print("Fetching current BTC 5m market...", flush=True)
    now = time.time()
    async with aiohttp.ClientSession() as s:
        # Try both 5m and 15m candles
        intervals = [
            (300, "5m"),
            (900, "15m"),
        ]
        for interval, label in intervals:
          for offset in range(5):
            candle_ts = int(now // interval) * interval - (offset * interval)
            slug = f"btc-updown-{label}-{candle_ts}"
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
                    print(f"  {question}", flush=True)
                    return tokens[up_idx], tokens[dn_idx], question, candle_ts
    return None, None, None, None


async def main():
    print("=" * 65)
    print("  PAPER TRADING TEST -- Latency Arb")
    print("  No real money. Just watching + simulating trades.")
    print("=" * 65)

    # State
    btc_price = 0.0
    btc_buffer = deque(maxlen=5000)
    poly_up_bid = 0.0
    poly_up_ask = 0.0
    poly_dn_bid = 0.0
    poly_dn_ask = 0.0
    poly_ts = 0.0
    btc_ticks = 0
    poly_ticks = 0

    # Paper trading state
    open_trades = []      # trades waiting to resolve
    closed_trades = []    # completed trades
    total_pnl = 0.0
    last_signal = 0.0
    current_candle_ts = 0

    # Market tokens (will be set by candle manager)
    up_token = None
    dn_token = None
    question = None
    token_side = {}
    reconnect_poly = asyncio.Event()
    reconnect_poly.set()

    LOOKBACK = 15
    MOVE_THRESH = 0.05
    COOLDOWN = 2  # seconds between trades
    MAX_ENTRY_PRICE = 0.80

    def poly_fee(price):
        return price * 0.25 * (price * (1 - price)) ** 2

    async def resolve_open_trades():
        """Check if any open trades can be closed (poly repriced in our favor)."""
        nonlocal total_pnl
        now = time.time()
        still_open = []
        for trade in open_trades:
            age = now - trade["entry_ts"]

            if trade["side"] == "up":
                current_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else 0
            else:
                current_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else 0

            # Exit conditions:
            # 1. Poly repriced 2c+ in our favor -> take profit
            # 2. 60s elapsed -> mark to market and close
            # 3. Candle ended -> close at last known mid
            profit_per_share = current_mid - trade["entry_price"] - trade["fee"]

            if current_mid > 0 and (profit_per_share >= 0.02 or age >= 60):
                pnl = profit_per_share * SHARES
                total_pnl += pnl
                trade["exit_price"] = current_mid
                trade["exit_ts"] = now
                trade["pnl"] = pnl
                trade["hold_time"] = age
                closed_trades.append(trade)

                emoji = "W" if pnl > 0 else "L"
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"  [{ts}] CLOSE #{len(closed_trades)} {emoji} | "
                    f"{trade['side'].upper()} entry={trade['entry_price']:.3f} "
                    f"exit={current_mid:.3f} | "
                    f"pnl=${pnl:+.2f} | hold={age:.1f}s | "
                    f"total=${total_pnl:+.2f} | "
                    f"WR={sum(1 for t in closed_trades if t['pnl']>0)}/{len(closed_trades)}",
                    flush=True
                )
            else:
                still_open.append(trade)

        open_trades.clear()
        open_trades.extend(still_open)

    async def check_move(move_pct, price, now):
        nonlocal last_signal

        if poly_up_ask <= 0 or poly_dn_ask <= 0:
            return
        if now - last_signal < COOLDOWN:
            return
        if len(open_trades) >= 5:  # max 5 open at once
            return

        direction = "UP" if move_pct > 0 else "DOWN"

        if direction == "UP":
            current_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid else poly_up_ask
            stale = current_mid < 0.55
            entry_price = poly_up_ask
            side = "up"
        else:
            current_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid else poly_dn_ask
            stale = current_mid < 0.55
            entry_price = poly_dn_ask
            side = "down"

        if not stale:
            return
        if entry_price <= 0 or entry_price >= MAX_ENTRY_PRICE:
            return

        last_signal = now
        fee = poly_fee(entry_price)

        trade = {
            "id": len(closed_trades) + len(open_trades) + 1,
            "side": side,
            "direction": direction,
            "entry_price": entry_price,
            "entry_mid": current_mid,
            "fee": fee,
            "btc_price": price,
            "btc_move": move_pct,
            "entry_ts": now,
            "exit_price": None,
            "exit_ts": None,
            "pnl": None,
            "hold_time": None,
        }
        open_trades.append(trade)

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(
            f"\n  >>> [{ts}] TRADE #{trade['id']} | "
            f"BTC {direction} {move_pct:+.3f}% @ ${price:,.0f} | "
            f"BUY {side.upper()} @ {entry_price:.3f} (mid={current_mid:.3f}) | "
            f"fee={fee:.4f} | open={len(open_trades)}",
            flush=True
        )

    async def binance_feed():
        nonlocal btc_price, btc_ticks
        while True:
            try:
                async with websockets.connect(
                    "wss://stream.binance.com:9443/ws/btcusdt@trade",
                    ping_interval=20
                ) as ws:
                    print("[Binance] Connected", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        p = float(msg.get("p", 0))
                        if p <= 0:
                            continue
                        btc_price = p
                        now = time.time()
                        btc_buffer.append((now, p))
                        btc_ticks += 1

                        # Check for move
                        cutoff = now - LOOKBACK
                        old_p = None
                        for ts, pr in btc_buffer:
                            if ts <= cutoff:
                                old_p = pr
                            else:
                                break
                        if old_p and old_p > 0:
                            move = (p - old_p) / old_p * 100
                            if abs(move) >= MOVE_THRESH:
                                await check_move(move, p, now)

                        # Try to resolve open trades
                        if open_trades and btc_ticks % 50 == 0:
                            await resolve_open_trades()
            except Exception as e:
                print(f"[Binance] Error: {e} -- reconnecting", flush=True)
                await asyncio.sleep(2)

    async def poly_feed():
        nonlocal poly_up_bid, poly_up_ask, poly_dn_bid, poly_dn_ask, poly_ts, poly_ticks
        nonlocal up_token, dn_token, question, token_side, current_candle_ts

        while True:
            # Get current market
            ut, dt, q, cts = await get_btc_market()
            if not ut:
                print("[Poly] No market found, retrying in 30s...", flush=True)
                await asyncio.sleep(30)
                continue

            up_token = ut
            dn_token = dt
            question = q
            current_candle_ts = cts
            token_side = {up_token: "up", dn_token: "down"}

            # Reset poly prices for new candle
            poly_up_bid = 0.0
            poly_up_ask = 0.0
            poly_dn_bid = 0.0
            poly_dn_ask = 0.0

            try:
                async with websockets.connect(
                    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
                ) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": [up_token, dn_token],
                        "type": "market",
                    }))
                    print(f"[Poly] Subscribed: {question}", flush=True)

                    candle_end = current_candle_ts + 300

                    async for raw in ws:
                        # Check if candle expired
                        if time.time() > candle_end + 10:
                            # Close any open trades
                            await resolve_open_trades()
                            print(f"\n[Poly] Candle ended, rolling over...", flush=True)
                            break

                        msg = json.loads(raw)
                        items = msg if isinstance(msg, list) else [msg]
                        for item in items:
                            aid = item.get("asset_id")

                            # Full book snapshot
                            if aid and aid in token_side and "bids" in item:
                                side = token_side[aid]
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                bb = max((float(b["price"]) for b in bids), default=0) if bids else 0
                                ba = min((float(a["price"]) for a in asks), default=0) if asks else 0
                                if side == "up":
                                    poly_up_bid = bb
                                    poly_up_ask = ba
                                else:
                                    poly_dn_bid = bb
                                    poly_dn_ask = ba
                                poly_ts = time.time()
                                poly_ticks += 1
                                continue

                            # Price changes
                            for ch in item.get("price_changes", []):
                                ch_aid = ch.get("asset_id")
                                if ch_aid not in token_side:
                                    continue
                                side = token_side[ch_aid]
                                price = float(ch.get("price", 0))
                                if ch.get("side") == "BUY":
                                    if side == "up":
                                        poly_up_bid = price
                                    else:
                                        poly_dn_bid = price
                                elif ch.get("side") == "SELL":
                                    if side == "up":
                                        poly_up_ask = price
                                    else:
                                        poly_dn_ask = price
                                poly_ts = time.time()
                                poly_ticks += 1

            except Exception as e:
                print(f"[Poly] Error: {e} -- reconnecting", flush=True)
                await asyncio.sleep(2)

    async def status_printer():
        while True:
            await asyncio.sleep(20)
            ts = datetime.now().strftime("%H:%M:%S")
            wins = sum(1 for t in closed_trades if t["pnl"] and t["pnl"] > 0)
            n = len(closed_trades)
            wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
            up_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else 0
            dn_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else 0
            print(
                f"  [{ts}] BTC=${btc_price:,.0f} | "
                f"Up={up_mid:.3f} Dn={dn_mid:.3f} | "
                f"trades={n} open={len(open_trades)} WR={wr} | "
                f"PnL=${total_pnl:+.2f} | "
                f"ticks: btc={btc_ticks:,} poly={poly_ticks:,}",
                flush=True
            )

    print("\nStarting paper trade...\n", flush=True)
    try:
        await asyncio.gather(
            binance_feed(),
            poly_feed(),
            status_printer(),
        )
    except Exception as e:
        print(f"\nFATAL: {e} — restarting in 10s...", flush=True)
        await asyncio.sleep(10)
        await main()


if __name__ == "__main__":
    asyncio.run(main())
