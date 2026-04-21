"""
LIVE_S13: First CEX Move — LIVE trader
Structurally IDENTICAL to paper_s13api.py except:
  - Env vars (DRY_RUN, TRADE_USD, POLY_PRIVATE_KEY)
  - build_clob() + place_live_order() helpers
  - Inside check(): fire-and-forget order placement; compute shares from TRADE_USD/ask
  - resolve_delayed uses real shares+USDC instead of SHARES=100
Every WebSocket loop, check() flow, and setup() path is byte-identical to paper.
"""
import asyncio, json, time, aiohttp, websockets, sqlite3, os
from collections import deque
from datetime import datetime, timezone

INTERVAL = 300; MOVE_THRESH = 0.03
DB_DIR = "/root"

# ── LIVE-specific env config ─────────────────────────────────────────────────
TRADE_USD   = float(os.environ.get("TRADE_USD", "2.0"))
DRY_RUN     = os.environ.get("DRY_RUN", "true").lower() != "false"
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
    print("="*70+f"\n  LIVE_S13 [{mode_tag}]: First CEX Move (0.03%) — ${TRADE_USD}/trade — All 4 markets\n"+"="*70, flush=True)
    clob = build_clob()

    cb_prices={a["cb"]:0.0 for a in ASSETS}
    assets={}
    for cfg in ASSETS:
        assets[cfg["label"]]={**cfg,"candle_ts":0,"candle_open":None,"up_token":None,"dn_token":None,
            "up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"","entered":False,"trade":None,
            "last_cb_price":0.0,
            "up_book":{"bids":{},"asks":{}},"dn_book":{"bids":{},"asks":{}}}
    total_pnl=0.0;total_trades=0;total_wins=0

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

    async def setup(a):
        now=time.time();cs=(int(now)//INTERVAL)*INTERVAL
        if cs==a["candle_ts"]: return
        if a["candle_ts"]>0 and a["trade"]:
            ws_snap = (a["up_ask"], a["dn_ask"], a["up_bid"], a["dn_bid"])
            asyncio.create_task(resolve_delayed(a["label"], a["slug"], dict(a["trade"]), a["candle_ts"], ws_snap))
        a["candle_ts"]=cs;a["entered"]=False;a["trade"]=None
        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0
        a["up_book"]={"bids":{},"asks":{}};a["dn_book"]={"bids":{},"asks":{}}
        a["candle_open"]=None
        up,dn,q,_=await get_market(a["slug"])
        if up and dn: a["up_token"]=up;a["dn_token"]=dn;a["question"]=q;print(f"[{a['label']}] {q}",flush=True)

    async def _submit_order_bg(a, trade, token_id):
        """Fire-and-forget: place the real order, update trade dict with actual fill."""
        try:
            shares, spent, resp = await place_live_order(clob, token_id, TRADE_USD)
            trade["shares_filled"] = shares
            trade["usdc_spent"]    = spent
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] FILLED [{a['label']}] {trade['side']} shares={shares:.3f} spent=${spent:.2f}", flush=True)
        except Exception as e:
            a["entered"] = False  # allow retry in same candle
            a["trade"]   = None
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] ORDER_FAIL [{a['label']}] {trade['side']}: {e}", flush=True)

    def check(a):
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
        # ── entry recorded exactly like paper, plus live order path ──
        a["entered"]=True
        trade={"side":d,"ask":ask,"ts":now}
        if DRY_RUN or clob is None:
            # DRY: simulate the fill we'd get for $TRADE_USD at `ask`
            trade["shares_filled"] = TRADE_USD / ask
            trade["usdc_spent"]    = TRADE_USD
            tag = "DRY"
        else:
            # LIVE: spawn fire-and-forget order, updates trade dict async
            trade["shares_filled"] = 0.0  # filled later by _submit_order_bg
            trade["usdc_spent"]    = 0.0
            token_id = a["up_token"] if d == "Up" else a["dn_token"]
            asyncio.create_task(_submit_order_bg(a, trade, token_id))
            tag = "LIVE"
        a["trade"]=trade
        ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
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
                                book=a["up_book"] if side=="Up" else a["dn_book"]
                                etype=item.get("event_type","")
                                if etype=="book":
                                    book["bids"]={b["price"]:float(b["size"]) for b in item.get("bids",[])}
                                    book["asks"]={x["price"]:float(x["size"]) for x in item.get("asks",[])}
                                elif etype=="price_change":
                                    for ch in item.get("changes",[]):
                                        d=book["bids"] if ch["side"]=="BUY" else book["asks"]
                                        d[ch["price"]]=float(ch["size"])
                                bids_live=[float(p) for p,s in book["bids"].items() if s>0]
                                asks_live=[float(p) for p,s in book["asks"].items() if s>0]
                                if side=="Up":
                                    if bids_live: a["up_bid"]=max(bids_live)
                                    if asks_live: a["up_ask"]=min(asks_live)
                                else:
                                    if bids_live: a["dn_bid"]=max(bids_live)
                                    if asks_live: a["dn_ask"]=min(asks_live)
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
            print(f"[{ts}] LIVE_S13_V2[{tag}] | {total_trades}t WR={wr:.0f}% PnL=${total_pnl:+.2f}",flush=True)

    tasks=[asyncio.create_task(coinbase_ws()),asyncio.create_task(tick()),asyncio.create_task(status())]
    for a in assets.values(): tasks.append(asyncio.create_task(poly_ws(a)))
    await asyncio.gather(*tasks)

if __name__=="__main__": asyncio.run(main())
