"""
Paper Trader — Strat 7 (Candle Momentum) — DUAL TRACKING
==========================================================
Tracks TWO versions simultaneously:
  A) INSTANT  — enters at the ask price when signal fires
  B) SLIPPAGE — waits for next 3 Poly ticks, enters at avg ask

Monitors Coinbase BTC/ETH price. When price moved >= 0.05% from candle open
and Polymarket mid is still < 0.55 (stale), triggers entry.

Usage:
    python arb_bot/paper_momentum.py
    $env:MOVE_THRESH="0.07"; python arb_bot/paper_momentum.py
"""
import asyncio
import json
import os
import time
import aiohttp
import websockets
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────
SHARES              = int(os.environ.get("SHARES", "100"))
MOVE_THRESH         = float(os.environ.get("MOVE_THRESH", "0.10"))
COOLDOWN            = 30
MAX_STALE_MID       = 0.45
MAX_ASK             = 0.90
MIN_ASK             = 0.01
MAX_ENTRIES_PER_CANDLE = 3
SLIPPAGE_DELAY      = 2  # seconds to wait before filling slippage entry

MARKET_CONFIGS = [
    {"label": "BTC_5m",  "slug_prefix": "btc", "interval": 300,  "coinbase_id": "BTC-USD"},
    {"label": "ETH_5m",  "slug_prefix": "eth", "interval": 300,  "coinbase_id": "ETH-USD"},
    {"label": "BTC_15m", "slug_prefix": "btc", "interval": 900,  "coinbase_id": "BTC-USD"},
    {"label": "ETH_15m", "slug_prefix": "eth", "interval": 900,  "coinbase_id": "ETH-USD"},
]

def fee(shares, price):
    return shares * price * 0.072 * (price * (1 - price))


async def get_market(slug_prefix, interval):
    now = time.time()
    tf = "5m" if interval == 300 else "15m"
    async with aiohttp.ClientSession() as s:
        for offset in range(3):
            candle_ts = int(now // interval) * interval - (offset * interval)
            slug = f"{slug_prefix}-updown-{tf}-{candle_ts}"
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


async def main():
    print("=" * 70)
    print(f"  STRAT 7 -- CANDLE MOMENTUM -- DUAL TRACKER")
    print(f"  A) Instant entry   B) Slippage ({SLIPPAGE_DELAY}s delay, then current price)")
    print(f"  Markets: BTC 5m/15m + ETH 5m/15m")
    print(f"  MOVE_THRESH={MOVE_THRESH}% | SHARES={SHARES} | COOLDOWN={COOLDOWN}s")
    print(f"  MAX_ENTRIES_PER_CANDLE={MAX_ENTRIES_PER_CANDLE}")
    print("=" * 70)
    print()

    markets = {}
    for cfg in MARKET_CONFIGS:
        markets[cfg["label"]] = {
            **cfg,
            "candle_ts": 0,
            "candle_open_price": None,
            "up_token": None, "dn_token": None,
            "up_bid": 0.0, "up_ask": 0.0,
            "dn_bid": 0.0, "dn_ask": 0.0,
            "question": "",
            "entries_a": [],
            "entries_b": [],
            "pending_b": [],
            "candle_trades": 0,
        }

    coinbase_prices = {"BTC-USD": 0.0, "ETH-USD": 0.0}

    stats = {
        'a': {'pnl': 0.0, 'trades': 0, 'wins': 0},
        'b': {'pnl': 0.0, 'trades': 0, 'wins': 0},
    }
    last_trade_time = 0.0

    def up_mid(mkt):
        if mkt["up_bid"] > 0 and mkt["up_ask"] > 0:
            return (mkt["up_bid"] + mkt["up_ask"]) / 2
        return 0

    def dn_mid(mkt):
        if mkt["dn_bid"] > 0 and mkt["dn_ask"] > 0:
            return (mkt["dn_bid"] + mkt["dn_ask"]) / 2
        return 0

    def resolve_candle(mkt):
        um = up_mid(mkt)
        dm = dn_mid(mkt)
        winner = 'Up' if um >= dm else 'Down'

        for version, entries_key in [('a', 'entries_a'), ('b', 'entries_b')]:
            entries = mkt[entries_key]
            if not entries:
                continue
            total_cost = 0.0
            total_pnl = 0.0
            for e in entries:
                cost = e["cost"]
                total_cost += cost
                if e["side"] == winner:
                    total_pnl += (1.0 * e["shares"]) - cost
                else:
                    total_pnl += 0 - cost

            stats[version]['pnl'] += total_pnl
            stats[version]['trades'] += 1
            if total_pnl > 0:
                stats[version]['wins'] += 1

            tag = "WIN" if total_pnl > 0 else "LOSS"
            v_label = "INSTANT" if version == 'a' else "SLIPPAGE"
            s = stats[version]
            wr = 100 * s['wins'] / s['trades'] if s['trades'] > 0 else 0

            print(f"  [{v_label}] {mkt['label']} | {winner} {tag} | {len(entries)} entries | cost=${total_cost:.2f} | PnL=${total_pnl:+.2f} | session: {s['trades']}c WR={wr:.0f}% ${s['pnl']:+.2f}")

        # Drop unfilled pending signals
        dropped = len(mkt["pending_b"])
        if dropped > 0:
            print(f"  [SLIPPAGE] {mkt['label']} | {dropped} pending signals expired (candle ended before {SLIPPAGE_DELAY}s delay)")
        mkt["pending_b"] = []

    async def setup_candle(mkt):
        now = time.time()
        interval = mkt["interval"]
        new_cs = (int(now) // interval) * interval

        if new_cs == mkt["candle_ts"]:
            return

        if mkt["entries_a"] or mkt["entries_b"] or mkt["pending_b"]:
            print()
            print(f"  {'='*60}")
            print(f"  CANDLE RESOLVED [{mkt['label']}]")
            resolve_candle(mkt)
            print(f"  {'='*60}")
            print()

        mkt["candle_ts"] = new_cs
        mkt["entries_a"] = []
        mkt["entries_b"] = []
        mkt["pending_b"] = []
        mkt["candle_trades"] = 0
        mkt["up_bid"] = mkt["up_ask"] = mkt["dn_bid"] = mkt["dn_ask"] = 0.0

        cb_price = coinbase_prices.get(mkt["coinbase_id"], 0)
        mkt["candle_open_price"] = cb_price if cb_price > 0 else None

        up_tok, dn_tok, question, _ = await get_market(mkt["slug_prefix"], mkt["interval"])
        if up_tok and dn_tok:
            mkt["up_token"] = up_tok
            mkt["dn_token"] = dn_tok
            mkt["question"] = question
            print(f"[{mkt['label']}] New candle: {question}")

    def check_signal(mkt):
        nonlocal last_trade_time
        now = time.time()
        if now - last_trade_time < COOLDOWN:
            return
        if mkt["candle_trades"] >= MAX_ENTRIES_PER_CANDLE:
            return
        if mkt["candle_open_price"] is None:
            return

        cb_price = coinbase_prices.get(mkt["coinbase_id"], 0)
        if cb_price <= 0:
            return

        offset = now - mkt["candle_ts"]
        if offset < 15 or offset > mkt["interval"] - 30:
            return

        move_pct = (cb_price - mkt["candle_open_price"]) / mkt["candle_open_price"] * 100
        if abs(move_pct) < MOVE_THRESH:
            return

        direction = "Up" if move_pct > 0 else "Down"

        if direction == "Up":
            mid = up_mid(mkt)
            ask = mkt["up_ask"]
        else:
            mid = dn_mid(mkt)
            ask = mkt["dn_ask"]

        if mid <= 0 or mid > MAX_STALE_MID:
            return
        if ask <= MIN_ASK or ask > MAX_ASK:
            return

        # A: instant entry
        cost_a = ask * SHARES + fee(SHARES, ask)
        mkt["entries_a"].append({
            "side": direction, "ask": ask, "shares": SHARES, "cost": cost_a, "ts": now,
        })

        # B: queue for slippage fill (will fill after SLIPPAGE_DELAY seconds)
        mkt["pending_b"].append({
            "direction": direction, "signal_ts": now, "fill_at": now + SLIPPAGE_DELAY,
        })

        mkt["candle_trades"] += 1
        last_trade_time = now

        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"  [{ts_str}] SIGNAL [{mkt['label']}] {direction} | ask={ask:.3f} mid={mid:.3f} | "
            f"mv={move_pct:+.3f}% | t+{int(offset)}s | "
            f"A: entered@{ask:.3f} (${cost_a:.2f}) | B: filling in {SLIPPAGE_DELAY}s... | "
            f"entries={mkt['candle_trades']}/{MAX_ENTRIES_PER_CANDLE}",
            flush=True
        )

    def process_slippage_pending(mkt):
        now = time.time()
        for pending in list(mkt["pending_b"]):
            if now < pending["fill_at"]:
                continue

            # Time's up — fill at current ask
            if pending["direction"] == "Up":
                ask = mkt["up_ask"]
            else:
                ask = mkt["dn_ask"]

            if ask <= MIN_ASK or ask > MAX_ASK:
                mkt["pending_b"].remove(pending)
                continue

            cost_b = ask * SHARES + fee(SHARES, ask)
            mkt["entries_b"].append({
                "side": pending["direction"], "ask": ask, "shares": SHARES,
                "cost": cost_b, "ts": pending["signal_ts"],
            })
            ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            # Find original instant entry ask for comparison
            orig_asks = [e["ask"] for e in mkt["entries_a"] if abs(e["ts"] - pending["signal_ts"]) < 1]
            orig = orig_asks[0] if orig_asks else 0
            diff = ask - orig if orig else 0
            print(
                f"  [{ts_str}] SLIPPAGE FILL [{mkt['label']}] {pending['direction']} | "
                f"ask={ask:.3f} (was {orig:.3f}, diff={diff:+.3f}) | cost=${cost_b:.2f}",
                flush=True
            )
            mkt["pending_b"].remove(pending)

    # ── Coinbase WebSocket ──────────────────────────────────────────────────
    async def coinbase_ws():
        url = "wss://ws-feed.exchange.coinbase.com"
        sub = {
            "type": "subscribe",
            "channels": [{"name": "ticker", "product_ids": ["BTC-USD", "ETH-USD"]}]
        }
        while True:
            try:
                async with websockets.connect(url, ping_interval=30) as ws:
                    await ws.send(json.dumps(sub))
                    print("[Coinbase] Connected: BTC-USD + ETH-USD")
                    async for msg in ws:
                        data = json.loads(msg)
                        if data.get("type") == "ticker":
                            pid = data.get("product_id")
                            price = float(data.get("price", 0))
                            if pid and price > 0:
                                coinbase_prices[pid] = price
            except Exception as e:
                print(f"[Coinbase] Error: {e}, reconnecting...")
                await asyncio.sleep(2)

    # ── Polymarket WebSocket ────────────────────────────────────────────────
    async def poly_ws(mkt):
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        while True:
            try:
                await setup_candle(mkt)
                if not mkt["up_token"] or not mkt["dn_token"]:
                    await asyncio.sleep(5)
                    continue

                token_side = {mkt["up_token"]: "Up", mkt["dn_token"]: "Down"}
                sub = {"type": "market", "assets_ids": [mkt["up_token"], mkt["dn_token"]]}

                async with websockets.connect(url, ping_interval=30) as ws:
                    await ws.send(json.dumps(sub))
                    async for msg in ws:
                        data = json.loads(msg)

                        now = time.time()
                        new_cs = (int(now) // mkt["interval"]) * mkt["interval"]
                        if new_cs != mkt["candle_ts"]:
                            await setup_candle(mkt)
                            break

                        if isinstance(data, list):
                            for item in data:
                                asset_id = item.get("asset_id")
                                side = token_side.get(asset_id)
                                if not side:
                                    continue
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                best_bid = max((float(b["price"]) for b in bids), default=0) if bids else 0
                                best_ask = min((float(a["price"]) for a in asks), default=0) if asks else 0
                                if side == "Up":
                                    if best_bid > 0: mkt["up_bid"] = best_bid
                                    if best_ask > 0: mkt["up_ask"] = best_ask
                                else:
                                    if best_bid > 0: mkt["dn_bid"] = best_bid
                                    if best_ask > 0: mkt["dn_ask"] = best_ask

                                process_slippage_pending(mkt)
                                check_signal(mkt)

            except Exception as e:
                print(f"[{mkt['label']}] WS error: {e}, reconnecting...")
                await asyncio.sleep(3)

    # ── Periodic signal checker (1s) ────────────────────────────────────────
    async def signal_loop():
        while True:
            await asyncio.sleep(1)
            for label, mkt in markets.items():
                now = time.time()
                new_cs = (int(now) // mkt["interval"]) * mkt["interval"]
                if new_cs != mkt["candle_ts"] and mkt["candle_ts"] > 0:
                    await setup_candle(mkt)
                process_slippage_pending(mkt)
                check_signal(mkt)

    # ── Status printer ──────────────────────────────────────────────────────
    async def status_loop():
        while True:
            await asyncio.sleep(15)
            now = time.time()
            parts = []
            for label, mkt in markets.items():
                cb = coinbase_prices.get(mkt["coinbase_id"], 0)
                op = mkt["candle_open_price"]
                move = (cb - op) / op * 100 if cb > 0 and op and op > 0 else 0
                um = up_mid(mkt)
                dm = dn_mid(mkt)
                remaining = mkt["interval"] - int(now - mkt["candle_ts"])
                ea = len(mkt["entries_a"])
                eb = len(mkt["entries_b"])
                pb = len(mkt["pending_b"])
                parts.append(f"{label}: mv={move:+.3f}% U={um:.2f} D={dm:.2f} A={ea} B={eb}({pb}p) e={mkt['candle_trades']}/{MAX_ENTRIES_PER_CANDLE} {remaining}s")

            sa = stats['a']; sb = stats['b']
            wr_a = 100*sa['wins']/sa['trades'] if sa['trades'] > 0 else 0
            wr_b = 100*sb['wins']/sb['trades'] if sb['trades'] > 0 else 0
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] {' | '.join(parts)}")
            print(f"         A(instant): {sa['trades']}c WR={wr_a:.0f}% ${sa['pnl']:+.2f} | B(slippage): {sb['trades']}c WR={wr_b:.0f}% ${sb['pnl']:+.2f}", flush=True)

    tasks = [asyncio.create_task(coinbase_ws())]
    for label, mkt in markets.items():
        tasks.append(asyncio.create_task(poly_ws(mkt)))
    tasks.append(asyncio.create_task(signal_loop()))
    tasks.append(asyncio.create_task(status_loop()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
