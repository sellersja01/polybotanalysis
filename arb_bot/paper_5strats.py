"""
paper_5strats.py — Paper trader for top 5 strategies on BTC/ETH/SOL/XRP 5m
===========================================================================
All Coinbase price feed. Polymarket odds via WS. Paper only.

Strategies:
  S13: First CEX Move >= 0.03% from candle open, Poly stale, hold to resolution
  S12: Both sides ask sum < 0.90, buy both, guaranteed $1.00 payout
  S4:  Latency arb — 15s lookback >= 0.05% move, sell at mid after 60s
  S6:  Penny reversal — buy when ask <= 0.10, hold to resolution
  S11: Mid-candle momentum — at t+150s buy leader if mid > 0.60

Usage: python3 -u paper_5strats.py
"""
import asyncio
import json
import time
import aiohttp
import websockets
from collections import deque
from datetime import datetime, timezone

SHARES = 100
INTERVAL = 300  # 5m

ASSETS = [
    {"label": "BTC", "slug_prefix": "btc", "cb_id": "BTC-USD"},
    {"label": "ETH", "slug_prefix": "eth", "cb_id": "ETH-USD"},
    {"label": "SOL", "slug_prefix": "sol", "cb_id": "SOL-USD"},
    {"label": "XRP", "slug_prefix": "xrp", "cb_id": "XRP-USD"},
]

def fee(price):
    return price * 0.072 * (price * (1 - price))


async def get_market(slug_prefix):
    now = time.time()
    async with aiohttp.ClientSession() as s:
        for offset in range(5):
            candle_ts = int(now // INTERVAL) * INTERVAL - (offset * INTERVAL)
            slug = f"{slug_prefix}-updown-5m-{candle_ts}"
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
    print("=" * 80)
    print("  TOP 5 STRATEGIES PAPER TRADER")
    print("  S13: First CEX Move | S12: Both Sides Cheap | S4: Latency Arb")
    print("  S6: Penny Reversal  | S11: Mid-Candle Momentum")
    print(f"  Markets: BTC/ETH/SOL/XRP 5m | SHARES={SHARES}")
    print("=" * 80)
    print()

    # Per-asset state
    assets = {}
    for cfg in ASSETS:
        assets[cfg["label"]] = {
            **cfg,
            "price": 0.0,
            "buffer": deque(maxlen=5000),
            "ticks": 0,
            "candle_ts": 0,
            "candle_open": None,
            "up_token": None, "dn_token": None,
            "up_bid": 0.0, "up_ask": 0.0,
            "dn_bid": 0.0, "dn_ask": 0.0,
            "question": "",
            # S13 state
            "s13_entered": False,
            # S12 state
            "s12_entered": False,
            # S4 state
            "s4_last_signal": 0.0,
            "s4_open_trades": [],
            # S6 state
            "s6_entered_up": False,
            "s6_entered_dn": False,
            # S11 state
            "s11_entered": False,
        }

    # Coinbase prices
    cb_prices = {a["cb_id"]: 0.0 for a in ASSETS}

    # Results tracking per strategy
    strat_results = {
        "S13": {"trades": 0, "wins": 0, "pnl": 0.0},
        "S12": {"trades": 0, "wins": 0, "pnl": 0.0},
        "S4":  {"trades": 0, "wins": 0, "pnl": 0.0},
        "S6":  {"trades": 0, "wins": 0, "pnl": 0.0},
        "S11": {"trades": 0, "wins": 0, "pnl": 0.0},
    }

    def up_mid(a):
        return (a["up_bid"] + a["up_ask"]) / 2 if a["up_bid"] > 0 and a["up_ask"] > 0 else 0

    def dn_mid(a):
        return (a["dn_bid"] + a["dn_ask"]) / 2 if a["dn_bid"] > 0 and a["dn_ask"] > 0 else 0

    def log_trade(strat, asset_label, side, entry_ask, pnl, extra=""):
        s = strat_results[strat]
        s["trades"] += 1
        s["pnl"] += pnl
        if pnl > 0: s["wins"] += 1
        wr = 100 * s["wins"] / s["trades"] if s["trades"] else 0
        tag = "W" if pnl > 0 else "L"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"  [{ts}] {strat} {tag} [{asset_label}] {side} @{entry_ask:.3f} "
            f"pnl=${pnl:+.2f} | {s['trades']}t WR={wr:.0f}% ${s['pnl']:+.2f} {extra}",
            flush=True
        )

    # ── Resolve candle ──────────────────────────────────────────────────────
    def resolve_candle(a):
        um = up_mid(a)
        dm = dn_mid(a)
        winner = "Up" if um >= dm else "Down"
        label = a["label"]

        # S13: resolve
        if a.get("s13_trade"):
            t = a["s13_trade"]
            cost = t["ask"] + fee(t["ask"])
            if t["side"] == winner:
                pnl = (1.0 - cost) * SHARES
            else:
                pnl = (0 - cost) * SHARES
            log_trade("S13", label, t["side"], t["ask"], pnl)

        # S12: resolve (guaranteed win)
        if a.get("s12_trade"):
            t = a["s12_trade"]
            cost = (t["up_ask"] + t["dn_ask"] + fee(t["up_ask"]) + fee(t["dn_ask"])) * SHARES
            pnl = SHARES - cost  # $1.00 payout
            log_trade("S12", label, "BOTH", t["up_ask"] + t["dn_ask"], pnl)

        # S4: resolve any still open
        for t in a["s4_open_trades"]:
            if not t.get("closed"):
                # Force close at current mid
                if t["side"] == "up":
                    exit_mid = um
                else:
                    exit_mid = dm
                pps = exit_mid - t["ask"] - fee(t["ask"])
                pnl = pps * SHARES
                log_trade("S4", label, t["side"], t["ask"], pnl, "force-close")

        # S6: resolve
        for t in a.get("s6_trades", []):
            cost = t["ask"] + fee(t["ask"])
            if t["side"] == winner:
                pnl = (1.0 - cost) * SHARES
            else:
                pnl = (0 - cost) * SHARES
            log_trade("S6", label, t["side"], t["ask"], pnl)

        # S11: resolve
        if a.get("s11_trade"):
            t = a["s11_trade"]
            cost = t["ask"] + fee(t["ask"])
            if t["side"] == winner:
                pnl = (1.0 - cost) * SHARES
            else:
                pnl = (0 - cost) * SHARES
            log_trade("S11", label, t["side"], t["ask"], pnl)

    # ── Setup candle ────────────────────────────────────────────────────────
    async def setup_candle(a):
        now = time.time()
        new_cs = (int(now) // INTERVAL) * INTERVAL
        if new_cs == a["candle_ts"]:
            return

        # Resolve previous candle
        if a["candle_ts"] > 0:
            resolve_candle(a)

        a["candle_ts"] = new_cs
        a["up_bid"] = a["up_ask"] = a["dn_bid"] = a["dn_ask"] = 0.0

        # Set candle open from Coinbase
        cb = cb_prices.get(a["cb_id"], 0)
        a["candle_open"] = cb if cb > 0 else None

        # Reset all strategy states
        a["s13_entered"] = False
        a["s13_trade"] = None
        a["s12_entered"] = False
        a["s12_trade"] = None
        a["s4_open_trades"] = []
        a["s4_last_signal"] = 0.0
        a["s6_entered_up"] = False
        a["s6_entered_dn"] = False
        a["s6_trades"] = []
        a["s11_entered"] = False
        a["s11_trade"] = None

        # Fetch new tokens
        up, dn, q, _ = await get_market(a["slug_prefix"])
        if up and dn:
            a["up_token"] = up; a["dn_token"] = dn; a["question"] = q
            print(f"[{a['label']}] {q}", flush=True)

    # ── Strategy checks (called on every price update) ──────────────────────
    def check_strategies(a):
        now = time.time()
        if a["candle_ts"] == 0 or a["up_ask"] <= 0 or a["dn_ask"] <= 0:
            return

        candle_age = now - a["candle_ts"]
        if candle_age < 5:
            return

        cb = cb_prices.get(a["cb_id"], 0)
        um = up_mid(a)
        dm = dn_mid(a)
        label = a["label"]

        # ── S13: First CEX Move >= 0.03% from candle open ──
        if not a["s13_entered"] and a["candle_open"] and cb > 0 and candle_age < INTERVAL - 30:
            move = (cb - a["candle_open"]) / a["candle_open"] * 100
            if abs(move) >= 0.03:
                direction = "Up" if move > 0 else "Down"
                mid = um if direction == "Up" else dm
                ask = a["up_ask"] if direction == "Up" else a["dn_ask"]
                if mid > 0 and mid < 0.55 and ask > 0 and ask < 0.75:
                    a["s13_entered"] = True
                    a["s13_trade"] = {"side": direction, "ask": ask, "ts": now}
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [{ts}] S13 ENTRY [{label}] {direction} @{ask:.3f} mid={mid:.3f} mv={move:+.3f}% t+{int(candle_age)}s", flush=True)

        # ── S12: Both Sides Cheap ──
        if not a["s12_entered"] and a["up_ask"] > 0.05 and a["dn_ask"] > 0.05:
            combined = a["up_ask"] + a["dn_ask"]
            if combined < 0.90:
                a["s12_entered"] = True
                a["s12_trade"] = {"up_ask": a["up_ask"], "dn_ask": a["dn_ask"], "ts": now}
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{ts}] S12 ENTRY [{label}] BOTH up={a['up_ask']:.3f}+dn={a['dn_ask']:.3f}={combined:.3f} (< 0.90)", flush=True)

        # ── S4: Latency Arb (60s exit) ──
        if now - a["s4_last_signal"] >= 2 and len(a["s4_open_trades"]) < 5 and candle_age < INTERVAL - 30:
            buf = a["buffer"]
            if len(buf) > 15:
                cutoff = now - 15
                old_p = None
                for ts, pr in buf:
                    if ts <= cutoff:
                        old_p = pr
                    else:
                        break
                if old_p and old_p > 0 and cb > 0:
                    move = (cb - old_p) / old_p * 100
                    if abs(move) >= 0.05:
                        direction = "up" if move > 0 else "down"
                        mid = um if direction == "up" else dm
                        ask = a["up_ask"] if direction == "up" else a["dn_ask"]
                        if mid > 0 and mid < 0.55 and 0.25 <= ask <= 0.75:
                            a["s4_last_signal"] = now
                            a["s4_open_trades"].append({"side": direction, "ask": ask, "ts": now, "closed": False})

        # ── S4: Check exits (60s or profit >= 2c) ──
        for t in a["s4_open_trades"]:
            if t["closed"]:
                continue
            age = now - t["ts"]
            if t["side"] == "up":
                cur_mid = um
            else:
                cur_mid = dm
            pps = cur_mid - t["ask"] - fee(t["ask"])
            if cur_mid > 0 and (pps >= 0.02 or age >= 60):
                pnl = pps * SHARES
                t["closed"] = True
                log_trade("S4", label, t["side"], t["ask"], pnl, f"hold={age:.0f}s")

        # ── S6: Penny Reversal ──
        if not a["s6_entered_up"] and a["up_ask"] <= 0.10 and a["up_ask"] > 0:
            a["s6_entered_up"] = True
            a["s6_trades"].append({"side": "Up", "ask": a["up_ask"], "ts": now})
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] S6  ENTRY [{label}] Up @{a['up_ask']:.3f}", flush=True)
        if not a["s6_entered_dn"] and a["dn_ask"] <= 0.10 and a["dn_ask"] > 0:
            a["s6_entered_dn"] = True
            a["s6_trades"].append({"side": "Down", "ask": a["dn_ask"], "ts": now})
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] S6  ENTRY [{label}] Down @{a['dn_ask']:.3f}", flush=True)

        # ── S11: Mid-Candle Momentum ──
        if not a["s11_entered"] and candle_age >= INTERVAL * 0.5:
            if um > dm and um > 0.60:
                a["s11_entered"] = True
                a["s11_trade"] = {"side": "Up", "ask": a["up_ask"], "ts": now}
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{ts}] S11 ENTRY [{label}] Up @{a['up_ask']:.3f} mid={um:.3f}", flush=True)
            elif dm > um and dm > 0.60:
                a["s11_entered"] = True
                a["s11_trade"] = {"side": "Down", "ask": a["dn_ask"], "ts": now}
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{ts}] S11 ENTRY [{label}] Down @{a['dn_ask']:.3f} mid={dm:.3f}", flush=True)

    # ── Coinbase WebSocket ──────────────────────────────────────────────────
    async def coinbase_ws():
        url = "wss://ws-feed.exchange.coinbase.com"
        product_ids = [a["cb_id"] for a in ASSETS]
        sub = {"type": "subscribe", "channels": [{"name": "ticker", "product_ids": product_ids}]}
        while True:
            try:
                async with websockets.connect(url, ping_interval=30) as ws:
                    await ws.send(json.dumps(sub))
                    print(f"[Coinbase] Connected: {', '.join(product_ids)}")
                    async for msg in ws:
                        data = json.loads(msg)
                        if data.get("type") == "ticker":
                            pid = data.get("product_id")
                            price = float(data.get("price", 0))
                            if pid and price > 0:
                                cb_prices[pid] = price
                                # Update asset buffer
                                for a in assets.values():
                                    if a["cb_id"] == pid:
                                        a["price"] = price
                                        a["buffer"].append((time.time(), price))
                                        a["ticks"] += 1
                                        check_strategies(a)
            except Exception as e:
                print(f"[Coinbase] Error: {e}, reconnecting...")
                await asyncio.sleep(2)

    # ── Polymarket WebSocket (one per asset) ────────────────────────────────
    async def poly_ws(a):
        url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        while True:
            try:
                await setup_candle(a)
                if not a["up_token"] or not a["dn_token"]:
                    await asyncio.sleep(5); continue

                token_side = {a["up_token"]: "Up", a["dn_token"]: "Down"}
                sub = {"type": "market", "assets_ids": [a["up_token"], a["dn_token"]]}

                async with websockets.connect(url, ping_interval=30) as ws:
                    await ws.send(json.dumps(sub))
                    async for msg in ws:
                        now = time.time()
                        new_cs = (int(now) // INTERVAL) * INTERVAL
                        if new_cs != a["candle_ts"]:
                            await setup_candle(a)
                            break

                        data = json.loads(msg)
                        if isinstance(data, list):
                            for item in data:
                                side = token_side.get(item.get("asset_id"))
                                if not side: continue
                                bids = item.get("bids", [])
                                asks = item.get("asks", [])
                                bb = max((float(b["price"]) for b in bids), default=0) if bids else 0
                                ba = min((float(a_["price"]) for a_ in asks), default=0) if asks else 0
                                if side == "Up":
                                    if bb > 0: a["up_bid"] = bb
                                    if ba > 0: a["up_ask"] = ba
                                else:
                                    if bb > 0: a["dn_bid"] = bb
                                    if ba > 0: a["dn_ask"] = ba
                                check_strategies(a)
            except Exception as e:
                print(f"[{a['label']}] WS: {e}")
                await asyncio.sleep(3)

    # ── Candle rollover checker ─────────────────────────────────────────────
    async def candle_checker():
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for a in assets.values():
                new_cs = (int(now) // INTERVAL) * INTERVAL
                if new_cs != a["candle_ts"] and a["candle_ts"] > 0:
                    await setup_candle(a)

    # ── Status printer ──────────────────────────────────────────────────────
    async def status_loop():
        while True:
            await asyncio.sleep(30)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            total_pnl = sum(s["pnl"] for s in strat_results.values())
            total_trades = sum(s["trades"] for s in strat_results.values())

            parts = []
            for name, s in strat_results.items():
                wr = 100*s["wins"]/s["trades"] if s["trades"] else 0
                parts.append(f"{name}:{s['trades']}t/{wr:.0f}%/${s['pnl']:+.0f}")

            print(f"[{ts}] {' | '.join(parts)} | TOTAL: {total_trades}t ${total_pnl:+.2f}", flush=True)

    # ── Launch ──────────────────────────────────────────────────────────────
    tasks = [asyncio.create_task(coinbase_ws()), asyncio.create_task(candle_checker()), asyncio.create_task(status_loop())]
    for a in assets.values():
        tasks.append(asyncio.create_task(poly_ws(a)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
