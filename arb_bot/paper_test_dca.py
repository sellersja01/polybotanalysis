"""
paper_test_dca.py — Galindrast-style strategy (full behavioral replica)
=========================================================================
Replicates ALL observed behaviors from Galindrast wallet analysis:

1. Enter BOTH sides immediately at candle start (~5-10s in)
2. When BTC moves on Binance, identify likely winner
3. DCA into the winning side throughout candle, scaling up as confidence grows
4. If candle REVERSES (our side drops below 0.30), FLIP to the new winner
5. Resolution scalp: pile in 10x size when any side hits 0.90+
6. Sell the losing side at 0.15-0.20 to recover capital
7. Hold all winning entries to candle resolution ($1.00)

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
LOOKBACK = 15
MOVE_THRESH = 0.05
DCA_INTERVAL = 10       # buy every 10 seconds
BASE_SHARES = 10        # base shares per DCA buy
MAX_ENTRIES = 20        # max total entries per candle (both sides combined)
FLIP_THRESHOLD = 0.25   # if our side drops below this, consider flipping
SELL_THRESHOLD = 0.20   # sell losing side when it drops below this
RESOLUTION_THRESHOLD = 0.90  # pile in when a side hits this

def get_shares(mid):
    """Scale shares based on confidence. 10x at resolution prices."""
    if mid >= 0.95:   return BASE_SHARES * 10   # 100sh — near certain
    elif mid >= 0.90: return BASE_SHARES * 8    # 80sh — resolution scalp
    elif mid >= 0.80: return BASE_SHARES * 5    # 50sh
    elif mid >= 0.70: return BASE_SHARES * 3    # 30sh
    elif mid >= 0.55: return BASE_SHARES * 2    # 20sh
    else:             return BASE_SHARES * 1    # 10sh — early/exploratory

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
    print("  GALINDRAST STRATEGY — Full Behavioral Replica")
    print("  Buy both sides early → DCA winner → flip on reversal")
    print("  → resolution scalp at 0.90+ → sell loser at 0.20")
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
    primary_dir = None       # 'UP' or 'DOWN' — which side we're DCA-ing into
    initial_entered = False  # did we do the initial both-sides buy?
    up_entries = []          # list of {'shares', 'cost'}
    dn_entries = []
    up_sold = []             # list of {'shares', 'revenue'}
    dn_sold = []
    last_dca_time = 0.0
    n_entries = 0
    flip_count = 0           # how many times we flipped this candle

    candle_pnl_history = []
    total_pnl = 0.0

    up_token = None
    dn_token = None
    question = None
    token_side = {}
    current_candle_ts = 0

    def up_mid():
        return (poly_up_bid + poly_up_ask) / 2 if poly_up_bid and poly_up_ask else poly_up_ask

    def dn_mid():
        return (poly_dn_bid + poly_dn_ask) / 2 if poly_dn_bid and poly_dn_ask else poly_dn_ask

    async def resolve_candle():
        nonlocal total_pnl, primary_dir, initial_entered, up_entries, dn_entries
        nonlocal up_sold, dn_sold, last_dca_time, n_entries, flip_count

        if not up_entries and not dn_entries:
            return

        um = up_mid()
        dm = dn_mid()
        winner = 'UP' if um >= dm else 'DOWN'
        if um == 0 and dm == 0:
            winner = primary_dir or 'UP'

        # Calculate PnL
        up_shares = sum(e['shares'] for e in up_entries)
        dn_shares = sum(e['shares'] for e in dn_entries)
        up_cost = sum(e['cost'] for e in up_entries)
        dn_cost = sum(e['cost'] for e in dn_entries)
        up_sell_rev = sum(s['revenue'] for s in up_sold)
        dn_sell_rev = sum(s['revenue'] for s in dn_sold)
        up_sold_sh = sum(s['shares'] for s in up_sold)
        dn_sold_sh = sum(s['shares'] for s in dn_sold)

        # Payout: remaining shares on winning side get $1, losing side gets $0
        up_remaining = up_shares - up_sold_sh
        dn_remaining = dn_shares - dn_sold_sh
        if winner == 'UP':
            payout = up_remaining * 1.0
        else:
            payout = dn_remaining * 1.0

        total_cost = up_cost + dn_cost
        total_revenue = payout + up_sell_rev + dn_sell_rev
        candle_pnl = total_revenue - total_cost
        total_pnl += candle_pnl
        candle_pnl_history.append(candle_pnl)

        n_candles = len(candle_pnl_history)
        n_wins = sum(1 for p in candle_pnl_history if p > 0)
        won = candle_pnl > 0

        ts = datetime.now().strftime("%H:%M:%S")
        emoji = "W" if won else "L"
        print(
            f"\n  [{ts}] CANDLE #{n_candles} {emoji} | "
            f"dir={primary_dir} winner={winner} flips={flip_count} | "
            f"Up: {up_remaining:.0f}sh/${up_cost:.0f} Dn: {dn_remaining:.0f}sh/${dn_cost:.0f} | "
            f"sold=${up_sell_rev + dn_sell_rev:.0f} | "
            f"pnl=${candle_pnl:+.2f} | total=${total_pnl:+.2f} | WR={n_wins}/{n_candles}",
            flush=True
        )

        # Reset
        primary_dir = None
        initial_entered = False
        up_entries = []
        dn_entries = []
        up_sold = []
        dn_sold = []
        last_dca_time = 0.0
        n_entries = 0
        flip_count = 0

    async def do_initial_entry():
        """Buy both sides at candle start — small exploratory position."""
        nonlocal initial_entered, n_entries

        if poly_up_ask <= 0 or poly_dn_ask <= 0:
            return

        # Buy small amount of both sides
        for side, ask, entries in [('UP', poly_up_ask, up_entries), ('DOWN', poly_dn_ask, dn_entries)]:
            if ask <= 0 or ask > 0.99:
                continue
            shares = BASE_SHARES
            fee = poly_fee(ask)
            cost = ask * shares + fee * shares
            entries.append({'shares': shares, 'cost': cost, 'price': ask})
            n_entries += 1

        initial_entered = True
        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"  [{ts}] INITIAL: bought both sides | "
            f"Up {BASE_SHARES}sh@{poly_up_ask:.3f} + Dn {BASE_SHARES}sh@{poly_dn_ask:.3f}",
            flush=True
        )

    async def check_signal(move_pct, price, now):
        """BTC moved — set or update primary direction."""
        nonlocal primary_dir

        direction = "UP" if move_pct > 0 else "DOWN"

        if primary_dir is None:
            primary_dir = direction
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(
                f"\n  >>> [{ts}] SIGNAL: BTC {direction} {move_pct:+.3f}% @ ${price:,.0f} | "
                f"locking primary={direction}",
                flush=True
            )

    async def check_flip(now):
        """Check if we should flip direction (our side collapsing)."""
        nonlocal primary_dir, flip_count

        if primary_dir is None:
            return

        if primary_dir == 'UP':
            our_mid = up_mid()
            their_mid = dn_mid()
        else:
            our_mid = dn_mid()
            their_mid = up_mid()

        # If our side dropped below flip threshold AND other side is strong
        if our_mid < FLIP_THRESHOLD and their_mid > 0.70 and flip_count < 3:
            old_dir = primary_dir
            primary_dir = 'DOWN' if primary_dir == 'UP' else 'UP'
            flip_count += 1
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"\n  !!! [{ts}] FLIP #{flip_count}: {old_dir} -> {primary_dir} | "
                f"our_mid={our_mid:.3f} their_mid={their_mid:.3f}",
                flush=True
            )

    async def try_sell_loser(now):
        """Sell the losing side if it drops below sell threshold."""
        nonlocal up_sold, dn_sold

        if primary_dir is None:
            return

        # The losing side is the opposite of primary_dir
        if primary_dir == 'UP':
            loser_mid = dn_mid()
            loser_bid = poly_dn_bid
            loser_entries = dn_entries
            loser_sold = dn_sold
            loser_name = 'DOWN'
        else:
            loser_mid = up_mid()
            loser_bid = poly_up_bid
            loser_entries = up_entries
            loser_sold = up_sold
            loser_name = 'UP'

        if loser_mid <= 0 or loser_mid > SELL_THRESHOLD:
            return
        if loser_bid <= 0:
            return

        # Calculate remaining shares on losing side
        already_sold = sum(s['shares'] for s in loser_sold)
        total_shares = sum(e['shares'] for e in loser_entries)
        remaining = total_shares - already_sold

        if remaining <= 0:
            return

        # Sell at bid price
        revenue = remaining * loser_bid
        fee = poly_fee(loser_bid) * remaining
        net_revenue = revenue - fee
        loser_sold.append({'shares': remaining, 'revenue': net_revenue})

        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"  [{ts}] SELL {loser_name} {remaining:.0f}sh @ {loser_bid:.3f} | "
            f"recovered ${net_revenue:.2f}",
            flush=True
        )

    async def try_dca(now):
        """DCA into the primary direction. Scale up at resolution prices."""
        nonlocal last_dca_time, n_entries

        if primary_dir is None:
            return
        if n_entries >= MAX_ENTRIES:
            return
        if now - last_dca_time < DCA_INTERVAL:
            return

        if primary_dir == 'UP':
            ask = poly_up_ask
            mid = up_mid()
            entries = up_entries
        else:
            ask = poly_dn_ask
            mid = dn_mid()
            entries = dn_entries

        if ask <= 0 or ask > 0.99:
            return
        if mid < 0.10:  # our side collapsed, don't buy
            return

        shares = get_shares(mid)
        fee = poly_fee(ask)
        cost = ask * shares + fee * shares

        entries.append({'shares': shares, 'cost': cost, 'price': ask})
        last_dca_time = now
        n_entries += 1

        ts = datetime.now().strftime("%H:%M:%S")
        total_sh = sum(e['shares'] for e in up_entries) + sum(e['shares'] for e in dn_entries)
        total_cost = sum(e['cost'] for e in up_entries) + sum(e['cost'] for e in dn_entries)
        res_flag = " *** RESOLUTION SCALP" if mid >= RESOLUTION_THRESHOLD else ""
        print(
            f"  [{ts}] DCA #{n_entries} | BUY {primary_dir} {shares}sh @ {ask:.3f} "
            f"(mid={mid:.3f}) | total: {total_sh:.0f}sh ${total_cost:.2f}{res_flag}",
            flush=True
        )

    async def try_resolution_scalp(now):
        """If either side hits 0.90+ and we don't have a direction, set it and buy big."""
        nonlocal primary_dir

        um = up_mid()
        dm = dn_mid()

        # If no direction yet but a side is at resolution price, jump in
        if primary_dir is None:
            if um >= RESOLUTION_THRESHOLD:
                primary_dir = 'UP'
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{ts}] RESOLUTION TRIGGER: Up at {um:.3f} — setting dir=UP", flush=True)
            elif dm >= RESOLUTION_THRESHOLD:
                primary_dir = 'DOWN'
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{ts}] RESOLUTION TRIGGER: Down at {dm:.3f} — setting dir=DOWN", flush=True)

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

                        # Periodic checks every ~20 ticks
                        if btc_ticks % 20 == 0:
                            # Initial entry at candle start
                            if not initial_entered and poly_up_ask > 0 and poly_dn_ask > 0:
                                await do_initial_entry()

                            await try_dca(now)
                            await check_flip(now)
                            await try_sell_loser(now)
                            await try_resolution_scalp(now)

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
            um = up_mid()
            dm = dn_mid()
            n = len(candle_pnl_history)
            wins = sum(1 for p in candle_pnl_history if p > 0)
            wr = f"{wins}/{n} ({wins/n*100:.0f}%)" if n else "0/0"
            up_sh = sum(e['shares'] for e in up_entries)
            dn_sh = sum(e['shares'] for e in dn_entries)
            cur_cost = sum(e['cost'] for e in up_entries) + sum(e['cost'] for e in dn_entries)
            print(
                f"  [{ts}] BTC=${btc_price:,.0f} | "
                f"Up={um:.3f} Dn={dm:.3f} | "
                f"dir={primary_dir or 'none'} flips={flip_count} | "
                f"Up:{up_sh:.0f}sh Dn:{dn_sh:.0f}sh cost=${cur_cost:.0f} | "
                f"candles={n} WR={wr} PnL=${total_pnl:+.2f}",
                flush=True
            )

    print("\nStarting Galindrast strategy...\n", flush=True)
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
