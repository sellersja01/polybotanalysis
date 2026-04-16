"""
live_coinbase.py — Latency arb bot (paper + live) — ETH 5m
============================================================
Matches backtest exactly:
  - Coinbase ETH price, 15s lookback, 0.05% move threshold
  - Poly mid < 0.55, max entry 0.75
  - Exit: sell at mid when profit >= 2c OR 20s elapsed OR candle last 10s
  - No entries in last 30s of candle
  - Cooldown 2s, unlimited trades per candle

Usage:
    python3 -u live_coinbase.py                    # paper
    DRY_RUN=false POLY_PRIVATE_KEY="0x..." TRADE_USD="1.0" python3 -u live_coinbase.py
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
SHARES    = int(os.environ.get("SHARES", "100"))
TRADE_SHARES = int(os.environ.get("TRADE_SHARES", "5"))
MOVE_THRESH = float(os.environ.get("MOVE_THRESH", "0.05"))

LOOKBACK            = 15
COOLDOWN            = 2
MAX_ENTRY_PRICE     = 0.75
MAX_STALE           = 0.55
MAX_OPEN            = 1
MAX_TRADES_PER_CANDLE = 1
CANDLE_INTERVAL     = 300  # 5m


def poly_fee(price):
    return price * 0.072 * (price * (1 - price))


# ── Polymarket CLOB client ──────────────────────────────────────────────────
def _build_clob():
    pk = os.environ.get("POLY_PRIVATE_KEY", "")
    if not pk:
        return None
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=137,
            signature_type=2,
            funder="0x6826c3197fff281144b07fe6c3e72636854769ab",
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print(f"[CLOB] Credentials derived: {creds.api_key[:8]}...", flush=True)
        return client
    except Exception as e:
        print(f"[CLOB] Init error: {e}", flush=True)
        return None


class PolyClient:
    def __init__(self, session):
        self._session = session
        self._clob = _build_clob() if not DRY_RUN else None

    async def place_order(self, token_id, price, size):
        if not self._clob:
            raise RuntimeError("CLOB not initialized")
        from py_clob_client.clob_types import MarketOrderArgs
        amount = round(size * price, 2)
        args = MarketOrderArgs(token_id=token_id, amount=amount, side="BUY", price=0.99)
        signed = await asyncio.to_thread(self._clob.create_market_order, args)
        resp = await asyncio.to_thread(self._clob.post_order, signed, "FAK")
        if float(resp.get("takingAmount", 0)) <= 0:
            raise RuntimeError(f"Filled 0 shares: {resp}")
        return resp

    async def sell_order(self, token_id, shares):
        if not self._clob:
            raise RuntimeError("CLOB not initialized")
        from py_clob_client.clob_types import MarketOrderArgs, BalanceAllowanceParams, AssetType
        # Query actual on-chain balance
        bal = await asyncio.to_thread(
            self._clob.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        actual = float(bal.get("balance", 0)) / 1e6
        if actual <= 0:
            raise RuntimeError(f"Zero balance for token {token_id[:16]}")
        args = MarketOrderArgs(token_id=token_id, amount=actual, side="SELL", price=0.01)
        signed = await asyncio.to_thread(self._clob.create_market_order, args)
        return await asyncio.to_thread(self._clob.post_order, signed, "FAK")


# ── Fetch ETH 5m market ─────────────────────────────────────────────────────
async def get_eth_market():
    now = time.time()
    async with aiohttp.ClientSession() as s:
        for offset in range(5):
            candle_ts = int(now // CANDLE_INTERVAL) * CANDLE_INTERVAL - (offset * CANDLE_INTERVAL)
            slug = f"eth-updown-5m-{candle_ts}"
            try:
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=10) as r:
                    data = await r.json()
                if data:
                    mkt = data[0].get("markets", [{}])[0]
                    tokens = json.loads(mkt.get("clobTokenIds", "[]"))
                    outcomes = json.loads(mkt.get("outcomes", "[]"))
                    if len(tokens) >= 2:
                        up_idx = 0 if outcomes[0] == "Up" else 1
                        dn_idx = 1 - up_idx
                        question = mkt.get("question") or slug
                        return tokens[up_idx], tokens[dn_idx], question, candle_ts
            except:
                pass
    return None, None, None, None


# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    mode_str = "DRY RUN (paper)" if DRY_RUN else "*** LIVE TRADING ***"
    print("=" * 65)
    print(f"  LATENCY ARB BOT -- {mode_str}")
    print(f"  Feed: Coinbase ETH-USD | ETH 5m")
    print(f"  MOVE_THRESH={MOVE_THRESH}% | MAX_ENTRY={MAX_ENTRY_PRICE}")
    print(f"  COOLDOWN={COOLDOWN}s | 1 trade per candle (1 buy + 1 hedge)")
    print(f"  Entry: {TRADE_SHARES} shares | Hedge: {TRADE_SHARES} shares of other side")
    print(f"  Exit: hedge when profit>=2c OR 20s OR candle last 10s")
    print(f"  No entries in last 30s of candle")
    print("=" * 65)

    poly_client = None
    poly_session = None
    if not DRY_RUN:
        poly_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=60),
            timeout=aiohttp.ClientTimeout(total=5, connect=2),
        )
        poly_client = PolyClient(session=poly_session)

    # State
    eth_price = 0.0
    eth_buffer = deque(maxlen=5000)
    eth_ticks = 0
    poly_up_bid = 0.0
    poly_up_ask = 0.0
    poly_dn_bid = 0.0
    poly_dn_ask = 0.0
    poly_ts = 0.0
    poly_ticks = 0
    up_token = None
    dn_token = None
    question = None
    token_side = {}
    current_candle_ts = 0

    open_trades = []
    closed_trades = []
    total_pnl = 0.0
    last_signal = 0.0
    candle_trade_count = 0

    # ── Resolve: buy OTHER side to hedge ───────────────────────────────────
    async def resolve_open_trades():
        nonlocal total_pnl
        now = time.time()
        still_open = []
        for trade in open_trades:
            age = now - trade["entry_ts"]
            if trade["side"] == "up":
                current_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else 0
                other_ask = poly_dn_ask
                other_token = dn_token
                other_side = "down"
            else:
                current_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else 0
                other_ask = poly_up_ask
                other_token = up_token
                other_side = "up"

            profit_per_share = current_mid - trade["entry_price"] - trade["fee"]

            # Force exit if candle ends in 10 seconds
            candle_start = (int(now) // CANDLE_INTERVAL) * CANDLE_INTERVAL
            candle_remaining = CANDLE_INTERVAL - (now - candle_start)
            force_exit = candle_remaining <= 10

            if current_mid > 0 and age >= 3.5 and (profit_per_share >= 0.02 or age >= 20 or force_exit):
                trade["exit_ts"] = now
                trade["hold_time"] = age

                if not DRY_RUN and poly_client and trade.get("shares_bought", 0) > 0:
                    # Buy exact TRADE_SHARES of the OTHER side to hedge
                    try:
                        from py_clob_client.clob_types import OrderArgs
                        args = OrderArgs(token_id=other_token, price=round(other_ask, 2), size=TRADE_SHARES, side="BUY")
                        t0 = time.perf_counter_ns()
                        signed = await asyncio.to_thread(poly_client._clob.create_order, args)
                        r = await asyncio.to_thread(poly_client._clob.post_order, signed, "FOK")
                        ms = (time.perf_counter_ns() - t0) / 1e6

                        hedge_spent = float(r.get("makingAmount", 0))
                        hedge_shares = float(r.get("takingAmount", 0))
                        entry_spent = float(trade.get("usdc_spent", 0))
                        total_spent = entry_spent + hedge_spent
                        locked_shares = min(trade.get("shares_bought", 0), hedge_shares)
                        guaranteed_pnl = locked_shares - total_spent
                        total_pnl += guaranteed_pnl
                        trade["pnl"] = guaranteed_pnl
                        trade["exit_price"] = other_ask
                        closed_trades.append(trade)
                        emoji = "W" if guaranteed_pnl > 0 else "L"
                        ts_str = datetime.now().strftime("%H:%M:%S")
                        print(
                            f"  [{ts_str}] HEDGE #{len(closed_trades)} {emoji} | "
                            f"{trade['side'].upper()} entry=${entry_spent:.2f} + {other_side.upper()} hedge=${hedge_spent:.2f} | "
                            f"locked={locked_shares:.4f}sh -> ${locked_shares:.2f} payout | "
                            f"pnl=${guaranteed_pnl:+.3f} | {ms:.0f}ms | hold={age:.1f}s | "
                            f"total=${total_pnl:+.3f} | "
                            f"WR={sum(1 for t in closed_trades if t['pnl']>0)}/{len(closed_trades)}",
                            flush=True
                        )
                    except Exception as e:
                        print(f"  HEDGE ERROR: {e}", flush=True)
                        still_open.append(trade)
                        continue
                else:
                    pnl = profit_per_share * SHARES
                    total_pnl += pnl
                    trade["pnl"] = pnl
                    trade["exit_price"] = current_mid
                    closed_trades.append(trade)
                    emoji = "W" if pnl > 0 else "L"
                    ts_str = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"  [{ts_str}] CLOSE #{len(closed_trades)} {emoji} [PAPER] | "
                        f"{trade['side'].upper()} entry={trade['entry_price']:.3f} exit={current_mid:.3f} | "
                        f"pnl=${pnl:+.2f} | hold={age:.1f}s | total=${total_pnl:+.2f} | "
                        f"WR={sum(1 for t in closed_trades if t['pnl']>0)}/{len(closed_trades)}",
                        flush=True
                    )
            else:
                still_open.append(trade)

        open_trades.clear()
        open_trades.extend(still_open)

    # ── Signal ──────────────────────────────────────────────────────────────
    async def check_move(move_pct, price, now):
        nonlocal last_signal, candle_trade_count

        if poly_up_ask <= 0 or poly_dn_ask <= 0:
            return
        if now - last_signal < COOLDOWN:
            return
        if len(open_trades) >= MAX_OPEN:
            return
        if candle_trade_count >= MAX_TRADES_PER_CANDLE:
            return
        # No entries in last 30s of candle
        candle_start = (int(now) // CANDLE_INTERVAL) * CANDLE_INTERVAL
        candle_age = now - candle_start
        if candle_age > CANDLE_INTERVAL - 30:
            return
        # Don't enter if Poly data is stale (candle rolled but WS hasn't reconnected)
        if current_candle_ts > 0 and candle_start != current_candle_ts:
            return

        direction = "UP" if move_pct > 0 else "DOWN"

        if direction == "UP":
            current_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid else poly_up_ask
            stale = current_mid < MAX_STALE
            entry_price = poly_up_ask
            side = "up"
            token_id = up_token
        else:
            current_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid else poly_dn_ask
            stale = current_mid < MAX_STALE
            entry_price = poly_dn_ask
            side = "down"
            token_id = dn_token

        if not stale:
            return
        if entry_price < 0.25 or entry_price >= MAX_ENTRY_PRICE:
            return

        last_signal = now
        candle_trade_count += 1
        fee = poly_fee(entry_price)

        trade = {
            "id": len(closed_trades) + len(open_trades) + 1,
            "side": side,
            "direction": direction,
            "entry_price": entry_price,
            "entry_mid": current_mid,
            "fee": fee,
            "eth_price": price,
            "eth_move": move_pct,
            "entry_ts": now,
            "token_id": token_id,
            "shares_bought": 0.0,
            "usdc_spent": 0.0,
        }
        open_trades.append(trade)

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(
            f"\n  >>> [{ts}] TRADE #{trade['id']} | "
            f"ETH {direction} {move_pct:+.3f}% @ ${price:,.2f} | "
            f"BUY {side.upper()} @ {entry_price:.3f} (mid={current_mid:.3f}) | "
            f"fee={fee:.4f} | open={len(open_trades)} | "
            f"{'LIVE' if not DRY_RUN else 'PAPER'}",
            flush=True
        )

        if not DRY_RUN and poly_client and token_id:
            try:
                from py_clob_client.clob_types import OrderArgs
                args = OrderArgs(token_id=token_id, price=round(entry_price, 2), size=TRADE_SHARES, side="BUY")
                t0 = time.perf_counter_ns()
                signed = await asyncio.to_thread(poly_client._clob.create_order, args)
                r = await asyncio.to_thread(poly_client._clob.post_order, signed, "FOK")
                ms = (time.perf_counter_ns() - t0) / 1e6
                shares_got = float(r.get("takingAmount", 0))
                usdc_spent = float(r.get("makingAmount", 0))
                trade["shares_bought"] = shares_got
                trade["usdc_spent"] = usdc_spent
                print(f"  BUY: {ms:.0f}ms | {shares_got:.4f} shares | spent=${usdc_spent:.4f} | status={r.get('status')}", flush=True)
            except Exception as e:
                print(f"  ORDER ERROR: {e}", flush=True)

    # ── Coinbase ETH feed ───────────────────────────────────────────────────
    async def coinbase_feed():
        nonlocal eth_price, eth_ticks
        while True:
            try:
                async with websockets.connect(
                    "wss://ws-feed.exchange.coinbase.com",
                    ping_interval=20
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channels": [{"name": "ticker", "product_ids": ["ETH-USD"]}]
                    }))
                    print("[Coinbase] Connected: ETH-USD", flush=True)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "ticker":
                            continue
                        p = float(msg.get("price", 0))
                        if p <= 0:
                            continue
                        eth_price = p
                        now = time.time()
                        eth_buffer.append((now, p))
                        eth_ticks += 1

                        cutoff = now - LOOKBACK
                        old_p = None
                        for ts, pr in eth_buffer:
                            if ts <= cutoff:
                                old_p = pr
                            else:
                                break
                        if old_p and old_p > 0:
                            move = (p - old_p) / old_p * 100
                            if abs(move) >= MOVE_THRESH:
                                await check_move(move, p, now)

                        if open_trades and eth_ticks % 10 == 0:
                            await resolve_open_trades()
            except Exception as e:
                print(f"[Coinbase] Error: {e} -- reconnecting", flush=True)
                await asyncio.sleep(2)

    # ── Poly feed ───────────────────────────────────────────────────────────
    async def poly_feed():
        nonlocal poly_up_bid, poly_up_ask, poly_dn_bid, poly_dn_ask, poly_ts, poly_ticks
        nonlocal up_token, dn_token, question, token_side, current_candle_ts, candle_trade_count

        while True:
            ut, dt, q, cts = await get_eth_market()
            if not ut:
                print("[Poly] No market found, retrying in 30s...", flush=True)
                await asyncio.sleep(30)
                continue

            up_token = ut
            dn_token = dt
            question = q
            current_candle_ts = cts
            candle_trade_count = 0
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

                    candle_end = current_candle_ts + CANDLE_INTERVAL

                    async for raw in ws:
                        if time.time() > candle_end + 10:
                            await resolve_open_trades()
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
                                    if bb > 0: poly_up_bid = bb
                                    if ba > 0: poly_up_ask = ba
                                else:
                                    if bb > 0: poly_dn_bid = bb
                                    if ba > 0: poly_dn_ask = ba
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

    # ── Status ──────────────────────────────────────────────────────────────
    async def status_printer():
        while True:
            await asyncio.sleep(20)
            ts = datetime.now().strftime("%H:%M:%S")
            wins = sum(1 for t in closed_trades if t.get("pnl") and t["pnl"] > 0)
            n = len(closed_trades)
            wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
            up_mid = (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else 0
            dn_mid = (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else 0
            print(
                f"  [{ts}] ETH=${eth_price:,.2f} | Up={up_mid:.3f} Dn={dn_mid:.3f} | "
                f"trades={n} open={len(open_trades)} WR={wr} | PnL=${total_pnl:+.2f} | "
                f"ticks: eth={eth_ticks:,} poly={poly_ticks:,}",
                flush=True
            )

    print("\nStarting...\n", flush=True)
    try:
        await asyncio.gather(
            coinbase_feed(),
            poly_feed(),
            status_printer(),
        )
    except Exception as e:
        print(f"\nFATAL: {e}", flush=True)
        if poly_session:
            await poly_session.close()


if __name__ == "__main__":
    asyncio.run(main())
