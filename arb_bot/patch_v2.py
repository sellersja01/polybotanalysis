"""
Patch paper_s13.py / paper_s13api.py / live_s13.py → *_v2.py
Fixes WebSocket book-handling: process both 'book' and 'price_change' events
using a full maintained order book (same pattern as collector_v2.py).

Changes applied to each file:
  1. Asset state init — add 'up_book' and 'dn_book' dicts
  2. setup() candle rollover — reset those books
  3. poly_ws() event handler — process 'book' (snapshot) and 'price_change' (diff)
     then compute best bid/ask from the full living book

Originals are left untouched.
"""
import sys, os

PATCHES = [
    # ── 1. Asset state init ────────────────────────────────────────────────
    (
        '"up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"","entered":False,"trade":None,\n'
        '            "last_cb_price":0.0}',
        '"up_bid":0.0,"up_ask":0.0,"dn_bid":0.0,"dn_ask":0.0,"question":"","entered":False,"trade":None,\n'
        '            "last_cb_price":0.0,\n'
        '            "up_book":{"bids":{},"asks":{}},"dn_book":{"bids":{},"asks":{}}}'
    ),
    # ── 2. setup() candle rollover book reset ──────────────────────────────
    (
        'a["candle_ts"]=cs;a["entered"]=False;a["trade"]=None\n'
        '        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0\n'
        '        a["candle_open"]=None',
        'a["candle_ts"]=cs;a["entered"]=False;a["trade"]=None\n'
        '        a["up_bid"]=a["up_ask"]=a["dn_bid"]=a["dn_ask"]=0.0\n'
        '        a["up_book"]={"bids":{},"asks":{}};a["dn_book"]={"bids":{},"asks":{}}\n'
        '        a["candle_open"]=None'
    ),
    # ── 3. WS handler: process 'book' and 'price_change' ───────────────────
    (
        '                                bids=item.get("bids",[]);asks=item.get("asks",[])\n'
        '                                bb=max((float(b["price"]) for b in bids),default=0) if bids else 0\n'
        '                                ba=min((float(x["price"]) for x in asks),default=0) if asks else 0\n'
        '                                if side=="Up":\n'
        '                                    if bb>0: a["up_bid"]=bb\n'
        '                                    if ba>0: a["up_ask"]=ba\n'
        '                                else:\n'
        '                                    if bb>0: a["dn_bid"]=bb\n'
        '                                    if ba>0: a["dn_ask"]=ba\n'
        '                                check(a)',
        '                                book=a["up_book"] if side=="Up" else a["dn_book"]\n'
        '                                etype=item.get("event_type","")\n'
        '                                if etype=="book":\n'
        '                                    book["bids"]={b["price"]:float(b["size"]) for b in item.get("bids",[])}\n'
        '                                    book["asks"]={x["price"]:float(x["size"]) for x in item.get("asks",[])}\n'
        '                                elif etype=="price_change":\n'
        '                                    for ch in item.get("changes",[]):\n'
        '                                        d=book["bids"] if ch["side"]=="BUY" else book["asks"]\n'
        '                                        d[ch["price"]]=float(ch["size"])\n'
        '                                bids_live=[float(p) for p,s in book["bids"].items() if s>0]\n'
        '                                asks_live=[float(p) for p,s in book["asks"].items() if s>0]\n'
        '                                if side=="Up":\n'
        '                                    if bids_live: a["up_bid"]=max(bids_live)\n'
        '                                    if asks_live: a["up_ask"]=min(asks_live)\n'
        '                                else:\n'
        '                                    if bids_live: a["dn_bid"]=max(bids_live)\n'
        '                                    if asks_live: a["dn_ask"]=min(asks_live)\n'
        '                                check(a)'
    ),
]

BANNER_PATCHES = [
    ('paper_s13.py',    'PAPER_S13[',   'PAPER_S13_V2['),
    ('paper_s13api.py', 'PAPER_S13API[','PAPER_S13API_V2['),
    ('live_s13.py',     'LIVE_S13[',    'LIVE_S13_V2['),
]

def patch_file(src, dst):
    with open(src) as f:
        code = f.read()
    applied = 0
    for old, new in PATCHES:
        if old not in code:
            print(f"  [WARN] pattern not found in {src}: {old[:80]!r}...")
            continue
        code = code.replace(old, new, 1)
        applied += 1
    # update status banner so logs clearly mark v2 runs
    base = os.path.basename(src)
    for f_, old, new in BANNER_PATCHES:
        if f_ == base:
            if old in code:
                code = code.replace(old, new)
    with open(dst, "w") as f:
        f.write(code)
    print(f"  {src} -> {dst}  ({applied}/{len(PATCHES)} patches applied)")

if __name__ == "__main__":
    for name in ("paper_s13.py", "paper_s13api.py", "live_s13.py"):
        src = f"/root/{name}"
        dst = src.replace(".py", "_v2.py")
        patch_file(src, dst)
