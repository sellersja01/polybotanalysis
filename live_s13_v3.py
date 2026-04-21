"""
LIVE_S13_V3: First CEX Move — LIVE trader with FIXED WS book handler.

THE KEY FIX vs live_s13.py:
  Old code only read `bids`/`asks` on each WS message (treating every message
  as a full snapshot). It silently ignored `price_change` events (incremental
  diffs), so the bot's view of the book stayed frozen at the opening snapshot
  while the real market moved. Data-staleness measured at 15-45¢ vs the
  collector — bot was entering trades on ~30s-stale prices.

  v3 maintains a full local book as price→size dict and handles both `book`
  (snapshot) and `price_change` (diff) events — same pattern as collector_v2.py.
  Best bid/ask are always computed from the live book.

Everything else (pre-sign, batch dispatcher, keep-alive pinger, latency
instrumentation) is unchanged from live_s13.py.
"""
import asyncio, json, time, aiohttp, websockets, sqlite3, os
from collections import deque
from datetime import datetime, timezone

INTERVAL = 300; MOVE_THRESH = 0.03
DB_DIR = "/root"

# ── LIVE-specific env config ─────────────────────────────────────────────────
TRADE_USD   = float(os.environ.get("TRADE_USD", "2.0"))
DRY_RUN     = os.environ.get("DRY_RUN", "true").lower() != "false"
MAX_FIRES   = int(os.environ.get("MAX_FIRES", "0"))  # 0 = unlimited. Caps LIVE fires only.
FIRES_FILE  = "/tmp/live_s13_v3_fires.txt"  # persistent counter — survives restart

def _read_fires():
    try:
        with open(FIRES_FILE, "r") as f: return int(f.read().strip() or "0")
    except FileNotFoundError: return 0
    except Exception: return 0
def _write_fires(n):
    try:
        with open(FIRES_FILE, "w") as f: f.write(str(n))
    except Exception: pass
POLY_KEY    = os.environ.get("POLY_PRIVATE_KEY", "")
POLY_FUNDER = "0x6826c3197fff281144b07fe6c3e72636854769ab"
POLY_CLOB   = "https://clob.polymarket.com"

def build_clob():
    if DRY_RUN:
        return None
    if not POLY_KEY:
        raise RuntimeError("DRY_RUN=false but POLY_PRIVATE_KEY is empty")
    from py_clob_client.client import ClobClient
    c = ClobClient(host=POLY_CLOB, key=POLY_KEY, chain_id=137,
                   signature_type=2, funder=POLY_FUNDER)
    creds = c.create_or_derive_api_creds()
    c.set_api_creds(creds)
    print(f"[poly] CLOB creds derived api_key={creds.api_key[:8]}...", flush=True)
    return c

async def place_live_order(clob, token_id, amount_usdc):
    from py_clob_client.clob_types import MarketOrderArgs
    args = MarketOrderArgs(token_id=token_id, amount=round(amount_usdc, 2),
                           side="BUY", price=0.99)
    signed = await asyncio.to_thread(clob.create_market_order, args)
    resp   = await asyncio.to_thread(clob.post_order, signed, "FAK")
    taking = float(resp.get("takingAmount", 0) or 0)
    making = float(resp.get("makingAmount", amount_usdc) or amount_usdc)
    return taking, making, resp

# ── Everything below MIRRORS paper_s13api.py structurally ────────────────────

def get_winner_from_db(label, cs):
    try:
        c = sqlite3.connect(f'{DB_DIR}/market_{label.lower()}_5m.db', timeout=2)
        rows = c.execute(
            "SELECT outcome, ask, bid FROM polymarket_odds "
            "WHERE unix_time >= ? AND unix_time < ? "
            "AND outcome IN ('Up','Down') AND ask > 0 ORDER BY unix_time",
            (cs, cs + INTERVAL)).fetchall()
        c.close()
    except Exception:
        return None
    ups = [(float(a), float(b)) for o, a, b in rows if o == 'Up']
    dns = [(float(a), float(b)) for o, a, b in rows if o == 'Down']
    if not ups or not dns: return None
    ua, ub = ups[-1]; da, db = dns[-1]
    if ua == 0 and da > 0: return "Up"
    if da == 0 and ua > 0: return "Down"
    um = (ub + ua) / 2; dm = (db + da) / 2
    return "Up" if um >= dm else "Down"

async def get_winner_from_api(slug, cs):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}-updown-5m-{cs}", timeout=10) as r:
                d = await r.json()
        if not d: return None
        m = d[0].get("markets", [{}])[0]
        if not m.get("closed"): return None
        prices = json.loads(m.get("outcomePrices", "[]"))
        outcomes = json.loads(m.get("outcomes", "[]"))
        if len(prices) < 2 or len(outcomes) < 2: return None
        for i, p in enumerate(prices):
            if float(p) >= 0.99:
                return outcomes[i]
    except Exception:
        return None
    return None

ASSETS = [
    {"label":"BTC","slug":"btc","cb":"BTC-USD"},
    {"label":"ETH","slug":"eth","cb":"ETH-USD"},
    {"label":"SOL","slug":"sol","cb":"SOL-USD"},
    {"label":"XRP","slug":"xrp","cb":"XRP-USD"},
]

async def get_market(slug):
    now=time.time()
    async with aiohttp.ClientSession() as s:
        for off in range(5):
            cs=int(now//INTERVAL)*INTERVAL-(off*INTERVAL)
            sl=f"{slug}-updown-5m-{cs}"
            try:
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={sl}",timeout=10) as r:
                    d=await r.json()
                if d:
                    m=d[0]["markets"][0];t=json.loads(m.get("clobTokenIds","[]"));o=json.loads(m.get("outcomes","[]"))
                    if len(t)>=2:
                        ui=0 if o[0]=="Up" else 1
                        return t[ui],t[1-ui],m.get("question",sl),cs
            except: pass
    return None,None,None,None

async def main():
    mode_tag = "DRY" if DRY_RUN else "LIVE"
    print("="*70+f"\n  LIVE_S13_V3 [{mode_tag}]: First CEX Move (0.03%) — ${TRADE_USD}/trade — FIXED WS handler\n"+"="*70, flush=True)
    clob = build_clob()

    cb_prices={a["cb"]:0.0 for a in ASSETS}
    assets={}
    for cfg in ASSETS:
        assets[cfg["label"]]={**cfg,"candle_ts":0,"candle_open":None,"up_token":None,"dn_token":None,
            "up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"","entered":False,"trade":None,
            "last_cb_price":0.0,
            "signed_up":None,"signed_dn":None,  # pre-signed orders (set by _presign)
            # v3: full book state (price → size) for each side, maintained from `book` + `price_change` events
            "up_book":{"bids":{},"asks":{}},
            "dn_book":{"bids":{},"asks":{}}}
    total_pnl=0.0;total_trades=0;total_wins=0
    live_fires = _read_fires()  # persistent counter — survives service restart
    if live_fires > 0:
        print(f"[startup] loaded live_fires={live_fires} from {FIRES_FILE} (MAX_FIRES={MAX_FIRES})", flush=True)

    def um(a): return (a["up_bid"]+a["up_ask"])/2 if a["up_bid"]>0 and a["up_ask"]>0 else 0
    def dm(a): return (a["dn_bid"]+a["dn_ask"])/2 if a["dn_bid"]>0 and a["dn_ask"]>0 else 0

    async def resolve_delayed(label, slug, trade, cs, ws_snapshot):
        nonlocal total_pnl,total_trades,total_wins
        await asyncio.sleep(5)
        winner = await get_winner_from_api(slug, cs); src = "API"
        if winner is None:
            winner = get_winner_from_db(label, cs); src = "DB"
        if winner is None:
            src = "WS"
            ua, da, ub, db = ws_snapshot
            if ua == 0 and da > 0: winner = "Up"
            elif da == 0 and ua > 0: winner = "Down"
            else:
                um_v = (ub+ua)/2; dm_v = (db+da)/2
                winner = "Up" if um_v >= dm_v else "Down"

        # Real PnL using actual shares + USDC spent (either DRY estimate or LIVE fill)
        shares = float(trade.get("shares_filled", 0.0) or 0.0)
        spent  = float(trade.get("usdc_spent", 0.0) or 0.0)
        if shares <= 0 or spent <= 0:
            # Order never filled — log & bail
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] SKIP [{label}] {trade['side']} @{trade['ask']:.3f} — no fill recorded", flush=True)
            return
        payout = shares if trade["side"] == winner else 0.0
        pnl    = payout - spent

        total_pnl += pnl; total_trades += 1
        if pnl > 0: total_wins += 1
        tag = "W" if pnl > 0 else "L"; wr = 100 * total_wins / total_trades
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] {tag} [{label}] {trade['side']} @{trade['ask']:.3f} "
              f"shares={shares:.3f} spent=${spent:.2f} payout=${payout:.2f} "
              f"pnl=${pnl:+.2f} ({src}) | {total_trades}t WR={wr:.0f}% ${total_pnl:+.2f}", flush=True)

    async def _presign(a, token_id, side_label, expected_candle_ts=None):
        """Pre-build + pre-sign a $TRADE_USD market BUY for this token.
        Retries up to 5 times with exponential backoff (0.5→1→2→4→8s).
        Bails out early if candle rolls over (tokens become stale).
        Saves the signed order in a['signed_up'] or a['signed_dn']."""
        if DRY_RUN or clob is None: return
        key = "signed_up" if side_label == "Up" else "signed_dn"
        # Skip if already have a signed order (e.g., watchdog raced with entry path)
        if a.get(key) is not None: return
        max_attempts = 5
        delay = 0.5
        from py_clob_client.clob_types import MarketOrderArgs
        for attempt in range(1, max_attempts + 1):
            # Don't waste effort on a candle we've already rolled past
            if expected_candle_ts is not None and a["candle_ts"] != expected_candle_ts:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                print(f"  [{ts}] PRESIGN_STALE [{a['label']}] {side_label} — candle rolled over, bailing", flush=True)
                return
            try:
                t0 = time.time()
                args = MarketOrderArgs(token_id=token_id, amount=round(TRADE_USD,2), side="BUY", price=0.99)
                signed = await asyncio.to_thread(clob.create_market_order, args)
                dt_ms = (time.time() - t0) * 1000
                a[key] = signed
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                suffix = f" (retry {attempt})" if attempt > 1 else ""
                print(f"  [{ts}] PRESIGN [{a['label']}] {side_label} ready in {dt_ms:.0f}ms{suffix}", flush=True)
                return
            except Exception as e:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                print(f"  [{ts}] PRESIGN_FAIL [{a['label']}] {side_label} (attempt {attempt}/{max_attempts}): {e}", flush=True)
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        print(f"  [{ts}] PRESIGN_GIVEUP [{a['label']}] {side_label} — triggers will fall back to OD (slow path)", flush=True)

    async def _presign_watchdog():
        """Every 5s, check each asset. If any side is missing a signed order
        mid-candle, kick off a presign attempt. Catches orphans from transient
        API failures."""
        if DRY_RUN or clob is None: return
        await asyncio.sleep(10)  # give initial presigns a chance
        while True:
            try:
                for a in assets.values():
                    if a["candle_ts"] <= 0: continue
                    now = time.time()
                    age = now - a["candle_ts"]
                    # Only attempt mid-candle — candle almost ending is wasted effort
                    if age < 5 or age > INTERVAL - 60: continue
                    if a["up_token"] and a.get("signed_up") is None:
                        asyncio.create_task(_presign(a, a["up_token"], "Up", a["candle_ts"]))
                    if a["dn_token"] and a.get("signed_dn") is None:
                        asyncio.create_task(_presign(a, a["dn_token"], "Down", a["candle_ts"]))
            except Exception as e:
                print(f"[watchdog] error: {e}", flush=True)
            await asyncio.sleep(5)

    async def setup(a):
        now=time.time();cs=(int(now)//INTERVAL)*INTERVAL
        if cs==a["candle_ts"]: return
        if a["candle_ts"]>0 and a["trade"]:
            ws_snap = (a["up_ask"], a["dn_ask"], a["up_bid"], a["dn_bid"])
            asyncio.create_task(resolve_delayed(a["label"], a["slug"], dict(a["trade"]), a["candle_ts"], ws_snap))
        a["candle_ts"]=cs;a["entered"]=False;a["trade"]=None
        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0
        a["candle_open"]=None
        # v3: new candle = new market tokens, discard all book state
        a["up_book"]={"bids":{},"asks":{}}
        a["dn_book"]={"bids":{},"asks":{}}
        # Discard last candle's pre-signed orders — new tokens next candle
        a["signed_up"]=None;a["signed_dn"]=None
        up,dn,q,_=await get_market(a["slug"])
        if up and dn:
            a["up_token"]=up;a["dn_token"]=dn;a["question"]=q
            print(f"[{a['label']}] {q}",flush=True)
            # Kick off pre-signing for both sides in parallel (background)
            # Pass candle_ts so the retry loop bails out if it's rolled over
            if not DRY_RUN and clob is not None:
                asyncio.create_task(_presign(a, up, "Up",   a["candle_ts"]))
                asyncio.create_task(_presign(a, dn, "Down", a["candle_ts"]))

    # ── Order submission queue + batch dispatcher ────────────────────────────
    # Pre-signed orders go here. Dispatcher drains and batches simultaneously-
    # queued orders into one post_orders() call (amortizes HTTP RTT across
    # multiple trades). Single orders still use post_order() for simplicity.
    order_queue = asyncio.Queue()

    def _record_fill(a, trade, resp, t0, t_send, mode):
        """Write fill result + log the latency breakdown."""
        shares = float(resp.get("takingAmount", 0) or 0)
        spent  = float(resp.get("makingAmount", TRADE_USD) or TRADE_USD)
        trade["shares_filled"] = shares
        trade["usdc_spent"]    = spent
        t3 = time.time()
        post_ms  = (t3 - t_send) * 1000
        total_ms = (t3 - t0) * 1000
        pre_ms   = (t_send - t0) * 1000
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        print(f"  [{ts}] FILLED[{mode}] [{a['label']}] {trade['side']} shares={shares:.3f} spent=${spent:.2f} "
              f"lat: pre={pre_ms:.0f}ms sign=0ms post={post_ms:.0f}ms TOTAL={total_ms:.0f}ms", flush=True)

    async def _fire_batch(batch):
        """Fire a batch of queued pre-signed orders.
        batch: list of (a_ref, trade_dict, signed_order)
        Uses post_orders (batch) if len > 1, else post_order (single)."""
        t0 = min(item[1]["ts"] for item in batch)  # earliest entry-decision time
        t_send = time.time()
        try:
            if len(batch) == 1:
                a, trade, signed = batch[0]
                resp = await asyncio.to_thread(clob.post_order, signed, "FAK")
                _record_fill(a, trade, resp, t0, t_send, mode="PS")
            else:
                from py_clob_client.clob_types import PostOrdersArgs
                args = [PostOrdersArgs(order=item[2], orderType="FAK") for item in batch]
                resps = await asyncio.to_thread(clob.post_orders, args)
                mode = f"PS_B{len(batch)}"
                # Expected: resps is a list/dict-list matching the input order
                if isinstance(resps, list) and len(resps) == len(batch):
                    for (a, trade, _), r in zip(batch, resps):
                        _record_fill(a, trade, r, t0, t_send, mode=mode)
                elif isinstance(resps, dict) and "orderHashes" in resps:
                    # Alternative batch response shape: dict with aggregate info
                    # Fall back to best-effort per-item record using whatever makingAmount we can derive
                    for a, trade, _ in batch:
                        # Attribute full TRADE_USD spent per item (best guess); takingAmount unknown
                        _record_fill(a, trade, {"takingAmount": 0, "makingAmount": TRADE_USD}, t0, t_send, mode=mode+"?")
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                    print(f"  [{ts}] BATCH_RESP(unmapped): {resps}", flush=True)
                else:
                    # Unknown response — record FAIL to be safe
                    raise RuntimeError(f"unexpected batch response shape: {resps}")
        except Exception as e:
            for a, trade, _ in batch:
                a["entered"] = False  # allow retry in same candle
                a["trade"]   = None
                fail_ms = (time.time() - t0) * 1000
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                print(f"  [{ts}] ORDER_FAIL [{a['label']}] {trade['side']} after {fail_ms:.0f}ms: {e}", flush=True)

    async def _order_dispatcher():
        """Drains order_queue. If multiple items arrived in the same event-loop
        window, combines into a batch. Otherwise fires single."""
        while True:
            first = await order_queue.get()
            batch = [first]
            # Drain anything else already waiting (no blocking wait)
            while True:
                try:
                    batch.append(order_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            # Fire the batch in its own task so dispatcher can pick up next work
            asyncio.create_task(_fire_batch(batch))

    async def _submit_od(a, trade, token_id):
        """On-demand sign+post fallback when pre-signed order wasn't ready."""
        t0 = trade["ts"]
        try:
            from py_clob_client.clob_types import MarketOrderArgs
            args = MarketOrderArgs(token_id=token_id, amount=round(TRADE_USD, 2),
                                   side="BUY", price=0.99)
            t1 = time.time()
            signed = await asyncio.to_thread(clob.create_market_order, args)
            t2 = time.time()
            resp = await asyncio.to_thread(clob.post_order, signed, "FAK")
            t3 = time.time()
            shares = float(resp.get("takingAmount", 0) or 0)
            spent  = float(resp.get("makingAmount", TRADE_USD) or TRADE_USD)
            trade["shares_filled"] = shares
            trade["usdc_spent"]    = spent
            sign_ms  = (t2 - t1) * 1000
            post_ms  = (t3 - t2) * 1000
            total_ms = (t3 - t0) * 1000
            pre_ms   = (t1 - t0) * 1000
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [{ts}] FILLED[OD] [{a['label']}] {trade['side']} shares={shares:.3f} spent=${spent:.2f} "
                  f"lat: pre={pre_ms:.0f}ms sign={sign_ms:.0f}ms post={post_ms:.0f}ms TOTAL={total_ms:.0f}ms",
                  flush=True)
        except Exception as e:
            a["entered"] = False
            a["trade"]   = None
            fail_ms = (time.time() - t0) * 1000
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [{ts}] ORDER_FAIL [{a['label']}] {trade['side']} after {fail_ms:.0f}ms: {e}", flush=True)

    async def _tls_keepalive():
        """Ping the CLOB host every 3s to keep httpx's persistent HTTP/2 connection warm.
        Without this, quiet periods cause the TLS connection to go idle and the next
        order pays a ~70ms cold-TLS penalty on reconnect."""
        if DRY_RUN or clob is None: return
        try:
            from py_clob_client.http_helpers.helpers import _http_client
        except Exception as e:
            print(f"[keepalive] can't import shared httpx client: {e}", flush=True)
            return
        url = "https://clob.polymarket.com/markets/keepalive"  # any 404 is fine, we just want the TCP/TLS exercise
        print("[keepalive] started (3s interval)", flush=True)
        while True:
            try:
                await asyncio.to_thread(_http_client.get, url)
            except Exception:
                pass  # silently absorb errors — ping is best-effort
            await asyncio.sleep(3.0)

    def check(a):
        nonlocal live_fires
        if a["entered"] or not a["candle_open"]: return
        now=time.time();age=now-a["candle_ts"]
        if age<10 or age>INTERVAL-30: return
        cb=cb_prices.get(a["cb"],0)
        if cb<=0: return
        move=(cb-a["candle_open"])/a["candle_open"]*100
        if abs(move)<MOVE_THRESH: return
        d="Up" if move>0 else "Down"
        mid=um(a) if d=="Up" else dm(a);ask=a["up_ask"] if d=="Up" else a["dn_ask"]
        if mid<=0 or mid>0.55 or ask<=0 or ask>=0.75: return
        # MAX_FIRES safety cap: only counted against LIVE fires, not DRY
        if not DRY_RUN and MAX_FIRES > 0 and live_fires >= MAX_FIRES:
            # Mark as entered so we don't keep re-evaluating same candle
            a["entered"]=True
            ts=datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"  [{ts}] MAX_FIRES_REACHED [{a['label']}] {d} @{ask:.3f} — skipping (cap={MAX_FIRES})",flush=True)
            return
        # ── entry recorded exactly like paper, plus live order path ──
        a["entered"]=True
        trade={"side":d,"ask":ask,"ts":now}
        if DRY_RUN or clob is None:
            # DRY: simulate the fill we'd get for $TRADE_USD at `ask`
            trade["shares_filled"] = TRADE_USD / ask
            trade["usdc_spent"]    = TRADE_USD
            tag = "DRY"
        else:
            # LIVE: if pre-signed order is ready, enqueue for batch dispatcher.
            # Otherwise fall back to on-demand sign+post.
            trade["shares_filled"] = 0.0  # filled later by dispatcher/OD
            trade["usdc_spent"]    = 0.0
            token_id = a["up_token"] if d == "Up" else a["dn_token"]
            signed = a["signed_up"] if d == "Up" else a["signed_dn"]
            if signed is not None:
                # Invalidate after use (signed orders have a unique salt), then
                # kick off re-signing in the background for the next trigger.
                if d == "Up":
                    a["signed_up"] = None
                    asyncio.create_task(_presign(a, a["up_token"], "Up",   a["candle_ts"]))
                else:
                    a["signed_dn"] = None
                    asyncio.create_task(_presign(a, a["dn_token"], "Down", a["candle_ts"]))
                order_queue.put_nowait((a, trade, signed))
                tag = "LIVE"
            else:
                # Loud warning: we're about to pay ~1s of signing on the hot path
                ts_warn=datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                print(f"  [{ts_warn}] WARN_NO_PRESIGN [{a['label']}] {d} — falling back to OD (slow ~1s path). Presign was never ready for this side this candle.", flush=True)
                asyncio.create_task(_submit_od(a, trade, token_id))
                tag = "LIVE-OD"
            live_fires += 1  # increment count for MAX_FIRES tracking
            _write_fires(live_fires)  # persist so restarts can't reset
        a["trade"]=trade
        ts=datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        print(f"  [{ts}] ENTRY[{tag}] [{a['label']}] {d} @{ask:.3f} mid={mid:.3f} mv={move:+.3f}%",flush=True)

    async def coinbase_ws():
        while True:
            try:
                async with websockets.connect("wss://ws-feed.exchange.coinbase.com",ping_interval=30) as ws:
                    await ws.send(json.dumps({"type":"subscribe","channels":[{"name":"ticker","product_ids":[a["cb"] for a in ASSETS]}]}))
                    print("[CB] Connected",flush=True)
                    async for msg in ws:
                        d=json.loads(msg)
                        if d.get("type")=="ticker":
                            pid=d.get("product_id");p=float(d.get("price",0))
                            if pid and p>0:
                                cb_prices[pid]=p
                                for a in assets.values():
                                    if a["cb"]==pid:
                                        a["last_cb_price"]=p
                                        if a["candle_open"] is None and a["candle_ts"]>0:
                                            a["candle_open"]=p
                                        check(a)
            except Exception as e: print(f"[CB] {e}");await asyncio.sleep(2)

    async def poly_ws(a):
        while True:
            try:
                await setup(a)
                if not a["up_token"]: await asyncio.sleep(5);continue
                ts={a["up_token"]:"Up",a["dn_token"]:"Down"}
                async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market",ping_interval=30) as ws:
                    await ws.send(json.dumps({"type":"market","assets_ids":[a["up_token"],a["dn_token"]]}))
                    async for msg in ws:
                        now=time.time()
                        if (int(now)//INTERVAL)*INTERVAL!=a["candle_ts"]: await setup(a);break
                        data=json.loads(msg)
                        if isinstance(data,list):
                            for item in data:
                                side=ts.get(item.get("asset_id"))
                                if not side: continue
                                # v3: maintain full book state from book + price_change events
                                book = a["up_book"] if side=="Up" else a["dn_book"]
                                etype = item.get("event_type","")
                                if etype == "book":
                                    # Full snapshot — replace entire book
                                    book["bids"] = {b["price"]: float(b["size"]) for b in item.get("bids", [])}
                                    book["asks"] = {x["price"]: float(x["size"]) for x in item.get("asks", [])}
                                elif etype == "price_change":
                                    # Incremental diffs — update specific levels
                                    for ch in item.get("changes", []):
                                        p  = ch["price"]
                                        sz = float(ch["size"])
                                        d  = book["bids"] if ch["side"]=="BUY" else book["asks"]
                                        if sz == 0: d.pop(p, None)
                                        else:       d[p] = sz
                                # Recompute best bid/ask from live book state
                                live_bids = [float(p) for p,s in book["bids"].items() if s>0]
                                live_asks = [float(p) for p,s in book["asks"].items() if s>0]
                                bb = max(live_bids) if live_bids else 0
                                ba = min(live_asks) if live_asks else 0
                                if side=="Up":
                                    if bb>0: a["up_bid"]=bb
                                    if ba>0: a["up_ask"]=ba
                                else:
                                    if bb>0: a["dn_bid"]=bb
                                    if ba>0: a["dn_ask"]=ba
                                check(a)
            except Exception as e: print(f"[{a['label']}] {e}");await asyncio.sleep(3)

    async def tick():
        while True:
            await asyncio.sleep(5)
            for a in assets.values():
                if (int(time.time())//INTERVAL)*INTERVAL!=a["candle_ts"] and a["candle_ts"]>0: await setup(a)

    async def status():
        while True:
            await asyncio.sleep(60)
            ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
            wr=100*total_wins/total_trades if total_trades else 0
            tag = "LIVE" if not DRY_RUN else "DRY"
            print(f"[{ts}] LIVE_S13_V3[{tag}] | {total_trades}t WR={wr:.0f}% PnL=${total_pnl:+.2f}",flush=True)

    tasks=[asyncio.create_task(coinbase_ws()),asyncio.create_task(tick()),asyncio.create_task(status()),
           asyncio.create_task(_order_dispatcher()),asyncio.create_task(_tls_keepalive()),
           asyncio.create_task(_presign_watchdog())]
    for a in assets.values(): tasks.append(asyncio.create_task(poly_ws(a)))
    await asyncio.gather(*tasks)

if __name__=="__main__": asyncio.run(main())
