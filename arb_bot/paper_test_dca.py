"""
paper_test_dca.py — Live paper test: Galindrast-style DCA to resolution
========================================================================
When BTC moves on Binance, pick the winning side and DCA into it
every DCA_INTERVAL seconds. Scale size based on poly mid confidence.
Hold ALL entries to candle resolution (candle end).

Usage: python paper_test_dca.py
"""
import asyncio
import json
import time
import aiohttp
import websockets
from collections import deque
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK = 15          # BTC move lookback (seconds)
MOVE_THRESH = 0.05     # min BTC move % to trigger signal
DCA_INTERVAL = 15      # buy every N seconds after signal
MAX_ENTRIES = 14       # max DCA entries per candle (sweet spot from backtest)
BASE_SHARES = 10       # base shares per buy

def get_shares(mid):
    """Scale shares based on confidence (poly mid)."""
    if mid >= 0.95:   return BASE_SHARES * 10
    elif mid >= 0.90: return BASE_SHARES * 8
    elif mid >= 0.80: return BASE_SHARES * 5
    elif mid >= 0.70: return BASE_SHARES * 3
    elif mid >= 0.55: return BASE_SHARES * 2
    else:             return BASE_SHARES * 1

def poly_fee(price):
    return price * 0.25 * (price * (1 - price)) ** 2


async def get_btc_market():
    print("Fetching current BTC 5m market...", flush=True)
    now = time.time()
    async with aiohttp.ClientSession() as s:
        for offset in range(5):
            candle_ts = int(now // 300) * 300 - (offset * 300)
            slug = f"btc-updown-5m-{candle_ts}"
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
    print("=" * 70)
    print("  PAPER TEST — Galindrast DCA Strategy")
    print("  DCA into winning side after BTC signal. Hold to resolution.")
    print(f"  DCA every {DCA_INTERVAL}s | Max {MAX_ENTRIES} entries | Base {BASE_SHARES}sh")
    print("=" * 70)

    btc_price = 0.0
    btc_buffer = deque(maxlen=5000)
    poly_up_bid = 0.0
    poly_up_ask = 0.0
    poly_dn_bid = 0.0
    poly_dn_ask = 0.0
    poly_ts = 0.0
    btc_ticks = 0
    poly_ticks = 0

    # Per-candle state
    signal_dir = None        # 'UP' or 'DOWN' — locked once set per candle
    candle_entries = []      # list of entries this candle
    last_dca_time = 0.0
    candle_pnl_history = []  # completed candle PnLs
    total_pnl = 0.0

    up_token = None
    dn_token = None
    question = None
    token_side = {}
    current_candle_ts = 0

    async def resolve_candle():
        """Called when candle ends. Resolve all entries."""
        nonlocal total_pnl, signal_dir, candle_entries, last_dca_time

        if not candle_entries:
            return

        # Determine winner from last known poly mids
        up_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else 0
        dn_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else 0

        # If mids are 0 (between candles), use last known
        winner = 'UP' if up_mid >= dn_mid else 'DOWN'
        if up_mid == 0 and dn_mid == 0:
            winner = signal_dir  # fallback

        candle_shares = 0
        candle_cost = 0.0
        candle_payout = 0.0

        for entry in candle_entries:
            payout = entry['shares'] if signal_dir == winner else 0.0
            candle_shares += entry['shares']
            candle_cost += entry['cost']
            candle_payout += payout

        candle_pnl = candle_payout - candle_cost
        total_pnl += candle_pnl
        candle_pnl_history.append(candle_pnl)

        won = signal_dir == winner
        n_candles = len(candle_pnl_history)
        n_wins = sum(1 for p in candle_pnl_history if p > 0)

        ts = datetime.now().strftime("%H:%M:%S")
        emoji = "W" if won else "L"
        print(
            f"\n  [{ts}] CANDLE #{n_candles} {emoji} | "
            f"dir={signal_dir} winner={winner} | "
            f"{len(candle_entries)} entries, {candle_shares:.0f}sh | "
            f"cost=${candle_cost:.2f} payout=${candle_payout:.2f} | "
            f"pnl=${candle_pnl:+.2f} | "
            f"total=${total_pnl:+.2f} | WR={n_wins}/{n_candles}",
            flush=True
        )

        # Reset for next candle
        signal_dir = None
        candle_entries = []
        last_dca_time = 0.0

    async def check_signal(move_pct, price, now):
        """Check if BTC move triggers initial signal."""
        nonlocal signal_dir

        if signal_dir is not None:
            return  # already have a direction this candle

        if poly_up_ask <= 0 or poly_dn_ask <= 0:
            return

        direction = "UP" if move_pct > 0 else "DOWN"

        # Check if poly is still stale (correct side mid < 0.55)
        if direction == "UP":
            current_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid else poly_up_ask
        else:
            current_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid else poly_dn_ask

        if current_mid > 0.60:
            return  # already repriced, too late for initial signal

        signal_dir = direction
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(
            f"\n  >>> [{ts}] SIGNAL: BTC {direction} {move_pct:+.3f}% @ ${price:,.0f} | "
            f"poly_mid={current_mid:.3f} | locking direction={direction}",
            flush=True
        )

    async def try_dca(now):
        """Attempt a DCA buy if conditions are met."""
        nonlocal last_dca_time

        if signal_dir is None:
            return
        if len(candle_entries) >= MAX_ENTRIES:
            return
        if now - last_dca_time < DCA_INTERVAL:
            return

        if signal_dir == "UP":
            ask = poly_up_ask
            mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid else poly_up_ask
        else:
            ask = poly_dn_ask
            mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid else poly_dn_ask

        if ask <= 0 or ask > 0.99:
            return
        if mid < 0.10:  # our side collapsed — don't keep buying
            return

        shares = get_shares(mid)
        fee = poly_fee(ask)
        cost = ask * shares + fee * shares

        candle_entries.append({
            'time': now,
            'ask': ask,
            'mid': mid,
            'shares': shares,
            'cost': cost,
        })
        last_dca_time = now

        ts = datetime.now().strftime("%H:%M:%S")
        total_sh = sum(e['shares'] for e in candle_entries)
        total_cost = sum(e['cost'] for e in candle_entries)
        print(
            f"  [{ts}] DCA #{len(candle_entries)} | "
            f"BUY {signal_dir} {shares}sh @ {ask:.3f} (mid={mid:.3f}) | "
            f"total: {total_sh:.0f}sh ${total_cost:.2f}",
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

                        # Check for BTC move
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
                                await check_signal(move, p, now)

                        # Try DCA every tick (interval enforced inside)
                        if signal_dir and btc_ticks % 20 == 0:
                            await try_dca(now)
            except Exception as e:
                print(f"[Binance] Error: {e} -- reconnecting", flush=True)
                await asyncio.sleep(2)

    async def poly_feed():
        nonlocal poly_up_bid, poly_up_ask, poly_dn_bid, poly_dn_ask, poly_ts, poly_ticks
        nonlocal up_token, dn_token, question, token_side, current_candle_ts

        while True:
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
                        if time.time() > candle_end + 10:
                            await resolve_candle()
                            print(f"\n[Poly] Candle ended, rolling over...", flush=True)
                            break

                        msg = json.loads(raw)
                        items = msg if isinstance(msg, list) else [msg]
                        for item in items:
                            aid = item.get("asset_id")

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
            await asyncio.sleep(30)
            ts = datetime.now().strftime("%H:%M:%S")
            up_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else 0
            dn_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else 0
            n = len(candle_pnl_history)
            wins = sum(1 for p in candle_pnl_history if p > 0)
            wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
            cur_entries = len(candle_entries)
            cur_cost = sum(e['cost'] for e in candle_entries)
            print(
                f"  [{ts}] BTC=${btc_price:,.0f} | "
                f"Up={up_mid:.3f} Dn={dn_mid:.3f} | "
                f"dir={signal_dir or 'none'} entries={cur_entries} cost=${cur_cost:.0f} | "
                f"candles={n} WR={wr} PnL=${total_pnl:+.2f}",
                flush=True
            )

    print("\nStarting DCA paper test...\n", flush=True)
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
