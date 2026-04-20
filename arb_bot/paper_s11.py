"""
S11: Mid-Candle Momentum — Paper Trader
At candle midpoint (t+150s), buy whichever side has mid > 0.60.
Hold to resolution.
"""
import asyncio, json, time, aiohttp, websockets
from datetime import datetime, timezone

SHARES=100;INTERVAL=300;MID_THRESH=0.60
ASSETS=[{"label":"BTC","slug":"btc","cb":"BTC-USD"},{"label":"ETH","slug":"eth","cb":"ETH-USD"},
        {"label":"SOL","slug":"sol","cb":"SOL-USD"},{"label":"XRP","slug":"xrp","cb":"XRP-USD"}]

def fee(p): return p*0.072*(p*(1-p))

async def get_market(slug):
    now=time.time()
    async with aiohttp.ClientSession() as s:
        for off in range(5):
            cs=int(now//INTERVAL)*INTERVAL-(off*INTERVAL)
            try:
                async with s.get(f"https://gamma-api.polymarket.com/events?slug={slug}-updown-5m-{cs}",timeout=10) as r:
                    d=await r.json()
                if d:
                    m=d[0]["markets"][0];t=json.loads(m.get("clobTokenIds","[]"));o=json.loads(m.get("outcomes","[]"))
                    if len(t)>=2:
                        ui=0 if o[0]=="Up" else 1
                        return t[ui],t[1-ui],m.get("question",""),cs
            except: pass
    return None,None,None,None

async def main():
    print("="*70+"\n  S11: Mid-Candle Momentum (mid>0.60 at t+150s) — All 4 markets\n"+"="*70)
    assets={}
    for cfg in ASSETS:
        assets[cfg["label"]]={**cfg,"candle_ts":0,"up_token":None,"dn_token":None,
            "up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"","entered":False,"trade":None,
            "last_cb_price":0.0,"candle_open_cb":0.0}
    total_pnl=0.0;total_trades=0;total_wins=0

    def um(a): return (a["up_bid"]+a["up_ask"])/2 if a["up_bid"]>0 and a["up_ask"]>0 else 0
    def dm(a): return (a["dn_bid"]+a["dn_ask"])/2 if a["dn_bid"]>0 and a["dn_ask"]>0 else 0

    def resolve(a):
        nonlocal total_pnl,total_trades,total_wins
        if not a["trade"]: return
        ua,da=a["up_ask"],a["dn_ask"]
        ub,db=a["up_bid"],a["dn_bid"]
        if ua == 0 and da > 0:
            winner = "Up"
        elif da == 0 and ua > 0:
            winner = "Down"
        else:
            um_v = (ub+ua)/2
            dm_v = (db+da)/2
            winner = "Up" if um_v >= dm_v else "Down"
        t=a["trade"];cost=t["ask"]+fee(t["ask"])
        pnl=((1.0-cost) if t["side"]==winner else (0-cost))*SHARES
        total_pnl+=pnl;total_trades+=1
        if pnl>0: total_wins+=1
        tag="W" if pnl>0 else "L";wr=100*total_wins/total_trades
        ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] {tag} [{a['label']}] {t['side']} @{t['ask']:.3f} pnl=${pnl:+.2f} | {total_trades}t WR={wr:.0f}% ${total_pnl:+.2f}",flush=True)

    async def setup(a):
        now=time.time();cs=(int(now)//INTERVAL)*INTERVAL
        if cs==a["candle_ts"]: return
        if a["candle_ts"]>0: resolve(a)
        a["candle_ts"]=cs;a["entered"]=False;a["trade"]=None
        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0
        a["candle_open_cb"]=a.get("last_cb_price",0)
        up,dn,q,_=await get_market(a["slug"])
        if up and dn: a["up_token"]=up;a["dn_token"]=dn;a["question"]=q;print(f"[{a['label']}] {q}",flush=True)

    def check(a):
        if a["entered"]: return
        now=time.time();age=now-a["candle_ts"]
        if age<INTERVAL*0.5: return  # only after midpoint
        u=um(a);d=dm(a)
        if u>d and u>MID_THRESH:
            a["entered"]=True;a["trade"]={"side":"Up","ask":a["up_ask"],"ts":now}
            ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] ENTRY [{a['label']}] Up @{a['up_ask']:.3f} mid={u:.3f} t+{int(age)}s",flush=True)
        elif d>u and d>MID_THRESH:
            a["entered"]=True;a["trade"]={"side":"Down","ask":a["dn_ask"],"ts":now}
            ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{ts}] ENTRY [{a['label']}] Down @{a['dn_ask']:.3f} mid={d:.3f} t+{int(age)}s",flush=True)

    async def coinbase_ws():
        while True:
            try:
                async with websockets.connect("wss://ws-feed.exchange.coinbase.com",ping_interval=30) as ws:
                    await ws.send(json.dumps({"type":"subscribe","channels":[{"name":"ticker","product_ids":[a["cb"] for a in ASSETS]}]}))
                    print("[CB] Connected",flush=True)
                    async for msg in ws: json.loads(msg)
            except Exception as e: print(f"[CB] {e}");await asyncio.sleep(2)

    async def poly_ws(a):
        while True:
            try:
                await setup(a)
                if not a["up_token"]: await asyncio.sleep(5);continue
                ts_map={a["up_token"]:"Up",a["dn_token"]:"Down"}
                async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market",ping_interval=30) as ws:
                    await ws.send(json.dumps({"type":"market","assets_ids":[a["up_token"],a["dn_token"]]}))
                    async for msg in ws:
                        now=time.time()
                        if (int(now)//INTERVAL)*INTERVAL!=a["candle_ts"]: await setup(a);break
                        data=json.loads(msg)
                        if isinstance(data,list):
                            for item in data:
                                side=ts_map.get(item.get("asset_id"))
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
                                u=um(a);d=dm(a)
                                if u>0: a["last_up_mid"]=u
                                if d>0: a["last_dn_mid"]=d
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
            wr=100*total_wins/total_trades if total_trades else 0
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] S11 | {total_trades}t WR={wr:.0f}% PnL=${total_pnl:+.2f}",flush=True)

    tasks=[asyncio.create_task(coinbase_ws()),asyncio.create_task(tick()),asyncio.create_task(status())]
    for a in assets.values(): tasks.append(asyncio.create_task(poly_ws(a)))
    await asyncio.gather(*tasks)

if __name__=="__main__": asyncio.run(main())
