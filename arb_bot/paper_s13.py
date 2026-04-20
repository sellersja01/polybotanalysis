"""
S13: First CEX Move — Paper Trader
When Coinbase moves >= 0.03% from candle open AND Poly mid < 0.55,
buy that direction. One trade per candle. Hold to resolution.
"""
import asyncio, json, time, aiohttp, websockets, sqlite3
from collections import deque
from datetime import datetime, timezone

SHARES = 100; INTERVAL = 300; MOVE_THRESH = 0.03
DB_DIR = "/root"  # collector DBs path on VPS

def get_winner_from_db(label, cs):
    """Query collector DB for the last tick of the finished candle."""
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
    """Query Polymarket gamma API for resolved outcome."""
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

def fee(p): return p * 0.072 * (p*(1-p))

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
    print("="*70+"\n  S13: First CEX Move (0.03%) — All 4 markets\n"+"="*70)
    cb_prices={a["cb"]:0.0 for a in ASSETS}
    assets={}
    for cfg in ASSETS:
        assets[cfg["label"]]={**cfg,"candle_ts":0,"candle_open":None,"up_token":None,"dn_token":None,
            "up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"","entered":False,"trade":None,
            "last_cb_price":0.0}
    total_pnl=0.0;total_trades=0;total_wins=0

    def um(a): return (a["up_bid"]+a["up_ask"])/2 if a["up_bid"]>0 and a["up_ask"]>0 else 0
    def dm(a): return (a["dn_bid"]+a["dn_ask"])/2 if a["dn_bid"]>0 and a["dn_ask"]>0 else 0

    async def resolve_delayed(label, slug, trade, cs, ws_snapshot):
        nonlocal total_pnl,total_trades,total_wins
        await asyncio.sleep(5)  # let DB collect final ticks + market fully resolve
        winner = get_winner_from_db(label, cs); src = "DB"
        if winner is None:
            winner = await get_winner_from_api(slug, cs); src = "API"
        if winner is None:
            src = "WS"
            ua, da, ub, db = ws_snapshot
            if ua == 0 and da > 0: winner = "Up"
            elif da == 0 and ua > 0: winner = "Down"
            else:
                um_v = (ub+ua)/2; dm_v = (db+da)/2
                winner = "Up" if um_v >= dm_v else "Down"
        cost = trade["ask"] + fee(trade["ask"])
        pnl = ((1.0 - cost) if trade["side"] == winner else (0 - cost)) * SHARES
        total_pnl += pnl; total_trades += 1
        if pnl > 0: total_wins += 1
        tag = "W" if pnl > 0 else "L"; wr = 100 * total_wins / total_trades
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] {tag} [{label}] {trade['side']} @{trade['ask']:.3f} pnl=${pnl:+.2f} ({src}) | {total_trades}t WR={wr:.0f}% ${total_pnl:+.2f}", flush=True)

    async def setup(a):
        now=time.time();cs=(int(now)//INTERVAL)*INTERVAL
        if cs==a["candle_ts"]: return
        if a["candle_ts"]>0 and a["trade"]:
            ws_snap = (a["up_ask"], a["dn_ask"], a["up_bid"], a["dn_bid"])
            asyncio.create_task(resolve_delayed(a["label"], a["slug"], dict(a["trade"]), a["candle_ts"], ws_snap))
        a["candle_ts"]=cs;a["entered"]=False;a["trade"]=None
        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0
        a["candle_open"]=None  # set on first CB tick inside this candle (matches backtest)
        up,dn,q,_=await get_market(a["slug"])
        if up and dn: a["up_token"]=up;a["dn_token"]=dn;a["question"]=q;print(f"[{a['label']}] {q}",flush=True)

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
        a["entered"]=True;a["trade"]={"side":d,"ask":ask,"ts":now}
        ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] ENTRY [{a['label']}] {d} @{ask:.3f} mid={mid:.3f} mv={move:+.3f}%",flush=True)

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
                                bids=item.get("bids",[]);asks=item.get("asks",[])
                                bb=max((float(b["price"]) for b in bids),default=0) if bids else 0
                                ba=min((float(x["price"]) for x in asks),default=0) if asks else 0
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
            print(f"[{ts}] S13 | {total_trades}t WR={wr:.0f}% PnL=${total_pnl:+.2f}",flush=True)

    tasks=[asyncio.create_task(coinbase_ws()),asyncio.create_task(tick()),asyncio.create_task(status())]
    for a in assets.values(): tasks.append(asyncio.create_task(poly_ws(a)))
    await asyncio.gather(*tasks)

if __name__=="__main__": asyncio.run(main())
