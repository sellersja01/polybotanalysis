"""
S4: Latency Arb (60s exit) — Paper Trader
15s lookback, Coinbase moves >= 0.05%, Poly mid < 0.55.
Buy stale side, exit at mid after 60s or profit >= 2c.
"""
import asyncio, json, time, aiohttp, websockets
from collections import deque
from datetime import datetime, timezone

SHARES=100;INTERVAL=300;LOOKBACK=15;MOVE_THRESH=0.05;COOLDOWN=2
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
    print("="*70+"\n  S4: Latency Arb (60s exit) — All 4 markets\n"+"="*70)
    cb_prices={a["cb"]:0.0 for a in ASSETS}
    assets={}
    for cfg in ASSETS:
        assets[cfg["label"]]={**cfg,"candle_ts":0,"up_token":None,"dn_token":None,
            "up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"",
            "buffer":deque(maxlen=5000),"last_signal":0.0,"open_trades":[]}
    total_pnl=0.0;total_trades=0;total_wins=0

    def um(a): return (a["up_bid"]+a["up_ask"])/2 if a["up_bid"]>0 and a["up_ask"]>0 else 0
    def dm(a): return (a["dn_bid"]+a["dn_ask"])/2 if a["dn_bid"]>0 and a["dn_ask"]>0 else 0

    def try_close(a):
        nonlocal total_pnl,total_trades,total_wins
        now=time.time();still=[]
        for t in a["open_trades"]:
            age=now-t["ts"]
            mid=um(a) if t["side"]=="up" else dm(a)
            pps=mid-t["ask"]-fee(t["ask"])
            if mid>0 and (pps>=0.02 or age>=60):
                pnl=pps*SHARES;total_pnl+=pnl;total_trades+=1
                if pnl>0: total_wins+=1
                tag="W" if pnl>0 else "L";wr=100*total_wins/total_trades
                ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{ts}] {tag} [{a['label']}] {t['side']} @{t['ask']:.3f} exit={mid:.3f} pnl=${pnl:+.2f} hold={age:.0f}s | {total_trades}t WR={wr:.0f}% ${total_pnl:+.2f}",flush=True)
            else: still.append(t)
        a["open_trades"]=still

    async def setup(a):
        now=time.time();cs=(int(now)//INTERVAL)*INTERVAL
        if cs==a["candle_ts"]: return
        try_close(a)
        a["candle_ts"]=cs;a["open_trades"]=[];a["last_signal"]=0.0
        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0
        up,dn,q,_=await get_market(a["slug"])
        if up and dn: a["up_token"]=up;a["dn_token"]=dn;a["question"]=q;print(f"[{a['label']}] {q}",flush=True)

    def check(a):
        now=time.time()
        if now-a["last_signal"]<COOLDOWN or len(a["open_trades"])>=5: return
        age=now-a["candle_ts"]
        if age<5 or age>INTERVAL-30: return
        cb=cb_prices.get(a["cb"],0)
        if cb<=0: return
        buf=a["buffer"]
        if len(buf)<LOOKBACK: return
        cutoff=now-LOOKBACK;old_p=None
        for ts,pr in buf:
            if ts<=cutoff: old_p=pr
            else: break
        if not old_p or old_p<=0: return
        move=(cb-old_p)/old_p*100
        if abs(move)<MOVE_THRESH: return
        d="up" if move>0 else "down"
        mid=um(a) if d=="up" else dm(a);ask=a["up_ask"] if d=="up" else a["dn_ask"]
        if mid<=0 or mid>0.55 or ask<0.25 or ask>0.75: return
        a["last_signal"]=now
        a["open_trades"].append({"side":d,"ask":ask,"ts":now})

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
                                    if a["cb"]==pid: a["buffer"].append((time.time(),p));check(a)
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
                                try_close(a);check(a)
            except Exception as e: print(f"[{a['label']}] {e}");await asyncio.sleep(3)

    async def tick():
        while True:
            await asyncio.sleep(5)
            for a in assets.values():
                if (int(time.time())//INTERVAL)*INTERVAL!=a["candle_ts"] and a["candle_ts"]>0: await setup(a)
                try_close(a)

    async def status():
        while True:
            await asyncio.sleep(60)
            wr=100*total_wins/total_trades if total_trades else 0
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] S4 | {total_trades}t WR={wr:.0f}% PnL=${total_pnl:+.2f}",flush=True)

    tasks=[asyncio.create_task(coinbase_ws()),asyncio.create_task(tick()),asyncio.create_task(status())]
    for a in assets.values(): tasks.append(asyncio.create_task(poly_ws(a)))
    await asyncio.gather(*tasks)

if __name__=="__main__": asyncio.run(main())
