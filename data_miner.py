import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DATABASES = [
    (r"C:\Users\James\polybotanalysis\market_btc_5m.db",  "BTC 5M"),
    (r"C:\Users\James\polybotanalysis\market_eth_5m.db",  "ETH 5M"),
    (r"C:\Users\James\polybotanalysis\market_btc_15m.db", "BTC 15M"),
    (r"C:\Users\James\polybotanalysis\market_eth_15m.db", "ETH 15M"),
]

def run(DB_PATH, LABEL):
    print(f"\n\n{'#'*75}")
    print(f"  {LABEL}")
    print(f"{'#'*75}")

    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT unix_time, market_id, outcome, bid, ask, mid FROM polymarket_odds ORDER BY unix_time ASC").fetchall()
        conn.close()
    except Exception as e:
        print(f"  SKIP: {e}"); return

    print(f"  Rows: {len(rows):,}")

    candles = defaultdict(lambda: {"Up": [], "Down": []})
    for unix_time, market_id, outcome, bid, ask, mid in rows:
        if outcome in ("Up", "Down") and mid is not None:
            candles[market_id][outcome].append((unix_time, float(bid or 0), float(ask or 1), float(mid)))

    def extract(cdata):
        up   = cdata["Up"]
        down = cdata["Down"]
        if not up or not down: return None
        start = up[0][0]
        end   = up[-1][0]
        final_mid = up[-1][3]
        if final_mid >= 0.90:   resolution = "Up"
        elif final_mid <= 0.10: resolution = "Down"
        else: return None

        open_up  = [m for t,_,_,m in up   if t <= start+10]
        open_dn  = [m for t,_,_,m in down if t <= start+10]
        if not open_up or not open_dn: return None
        open_up_mid = open_up[0]
        open_dn_mid = open_dn[0]

        mid30_up = next((m for t,_,_,m in up   if start+25 <= t <= start+35), open_up_mid)
        mid60_up = next((m for t,_,_,m in up   if start+55 <= t <= start+65), mid30_up)
        mid30_dn = next((m for t,_,_,m in down if start+25 <= t <= start+35), open_dn_mid)

        open_ask_up = next((a for t,_,a,_ in up   if t <= start+10), 1.0)
        open_ask_dn = next((a for t,_,a,_ in down if t <= start+10), 1.0)

        mids_up_60  = [m for t,_,_,m in up if t <= start+60]
        volatility  = max(mids_up_60) - min(mids_up_60) if mids_up_60 else 0
        momentum_up = mid30_up - open_up_mid
        bias        = open_up_mid - 0.50
        hour        = datetime.fromtimestamp(start, tz=timezone.utc).hour
        combined_ask= open_ask_up + open_ask_dn

        resolve_time = end - start
        for t,_,_,m in up:
            if m >= 0.80: resolve_time = t - start; break
        for t,_,_,m in down:
            if m >= 0.80: resolve_time = t - start; break

        return dict(resolution=resolution, start=start, open_up=open_up_mid,
                    open_dn=open_dn_mid, mid30_up=mid30_up, mid60_up=mid60_up,
                    mid30_dn=mid30_dn, momentum_up=momentum_up, volatility=volatility,
                    bias=bias, resolve_time=resolve_time, hour=hour,
                    combined_ask=combined_ask)

    feats = []
    for mid, cdata in sorted(candles.items(), key=lambda x: x[1]["Up"][0][0] if x[1]["Up"] else 0):
        f = extract(cdata)
        if f:
            f["prev_res"]  = feats[-1]["resolution"] if feats else None
            f["prev2_res"] = feats[-2]["resolution"] if len(feats)>=2 else None
            f["prev3_res"] = feats[-3]["resolution"] if len(feats)>=3 else None
            f["prev_rt"]   = feats[-1]["resolve_time"] if feats else 999
            feats.append(f)

    print(f"  Candles: {len(feats)}")
    up_c = sum(1 for f in feats if f["resolution"]=="Up")
    print(f"  Up: {up_c} ({100*up_c//len(feats)}%) | Down: {len(feats)-up_c} ({100*(len(feats)-up_c)//len(feats)}%)")

    def test(name, cond, side):
        subset = [f for f in feats if cond(f)]
        if len(subset) < 10: return
        wins = sum(1 for f in subset if f["resolution"]==side)
        wr   = 100*wins/len(subset)
        if wr >= 62 or wr <= 38:
            m = "✓✓✓" if wr>=75 else "✓✓" if wr>=70 else "✓" if wr>=62 else "✗✗✗" if wr<=25 else "✗✗" if wr<=30 else "✗"
            print(f"  {name:<60} n={len(subset):>4} WR={wr:>5.1f}% {m}")

    print("\n--- Momentum 30s ---")
    for t in [0.03,0.05,0.08,0.10,0.15]:
        test(f"Up momentum >{t} → Up",          lambda f,x=t: f["momentum_up"]>x,  "Up")
        test(f"Down momentum >{t} → Down",       lambda f,x=t: f["momentum_up"]<-x, "Down")
        test(f"Up momentum >{t} → Down (fade)",  lambda f,x=t: f["momentum_up"]>x,  "Down")
        test(f"Down momentum >{t} → Up (fade)",  lambda f,x=t: f["momentum_up"]<-x, "Up")

    print("\n--- Momentum 60s ---")
    for t in [0.05,0.10,0.15,0.20]:
        test(f"60s Up move >{t} → Up",          lambda f,x=t: f["mid60_up"]-f["open_up"]>x,  "Up")
        test(f"60s Down move >{t} → Down",      lambda f,x=t: f["mid60_up"]-f["open_up"]<-x, "Down")
        test(f"60s Up move >{t} → Down (fade)", lambda f,x=t: f["mid60_up"]-f["open_up"]>x,  "Down")

    print("\n--- Opening Bias ---")
    for t in [0.02,0.05,0.08,0.10,0.15]:
        test(f"Open Up bias >{t} → Up",   lambda f,x=t: f["bias"]>x,  "Up")
        test(f"Open Up bias >{t} → Down", lambda f,x=t: f["bias"]>x,  "Down")
        test(f"Open Dn bias >{t} → Down", lambda f,x=t: f["bias"]<-x, "Down")
        test(f"Open Dn bias >{t} → Up",   lambda f,x=t: f["bias"]<-x, "Up")

    print("\n--- Streak Patterns ---")
    test("After Up → Up",             lambda f: f["prev_res"]=="Up",   "Up")
    test("After Up → Down",           lambda f: f["prev_res"]=="Up",   "Down")
    test("After Down → Down",         lambda f: f["prev_res"]=="Down", "Down")
    test("After Down → Up",           lambda f: f["prev_res"]=="Down", "Up")
    test("Up,Up → Up",   lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Up",   "Up")
    test("Up,Up → Down", lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Up",   "Down")
    test("Dn,Dn → Down", lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Down", "Down")
    test("Dn,Dn → Up",   lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Down", "Up")
    test("Up,Dn → Up",   lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Up",   "Up")
    test("Up,Dn → Down", lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Up",   "Down")
    test("Dn,Up → Up",   lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Down", "Up")
    test("Dn,Up → Down", lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Down", "Down")
    test("Up,Up,Up → Down", lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Up"   and f["prev3_res"]=="Up",   "Down")
    test("Dn,Dn,Dn → Up",   lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Down" and f["prev3_res"]=="Down", "Up")
    test("Up,Up,Up → Up",   lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Up"   and f["prev3_res"]=="Up",   "Up")
    test("Dn,Dn,Dn → Down", lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Down" and f["prev3_res"]=="Down", "Down")

    print("\n--- Hour of Day (UTC) ---")
    for h in range(24):
        test(f"Hour {h:02d} → Up",   lambda f,hh=h: f["hour"]==hh, "Up")
        test(f"Hour {h:02d} → Down", lambda f,hh=h: f["hour"]==hh, "Down")

    print("\n--- Volatility ---")
    for t in [0.05,0.08,0.10,0.15,0.20]:
        test(f"High vol >{t} → Up",   lambda f,x=t: f["volatility"]>x, "Up")
        test(f"High vol >{t} → Down", lambda f,x=t: f["volatility"]>x, "Down")
        test(f"Low vol <{t} → Up",    lambda f,x=t: f["volatility"]<x, "Up")
        test(f"Low vol <{t} → Down",  lambda f,x=t: f["volatility"]<x, "Down")

    print("\n--- Opening Combined Ask ---")
    for t in [0.90,0.95,1.00,1.05,1.10,1.15]:
        test(f"Combined ask <{t} → Up",   lambda f,x=t: f["combined_ask"]<x, "Up")
        test(f"Combined ask <{t} → Down", lambda f,x=t: f["combined_ask"]<x, "Down")
        test(f"Combined ask >{t} → Up",   lambda f,x=t: f["combined_ask"]>x, "Up")
        test(f"Combined ask >{t} → Down", lambda f,x=t: f["combined_ask"]>x, "Down")

    print("\n--- Fast Previous Resolution ---")
    for s in [30,60,90,120,180]:
        test(f"Prev resolved fast <{s}s → Up",   lambda f,x=s: f["prev_rt"]<x, "Up")
        test(f"Prev resolved fast <{s}s → Down", lambda f,x=s: f["prev_rt"]<x, "Down")
        test(f"Prev resolved slow >{s}s → Up",   lambda f,x=s: f["prev_rt"]>x, "Up")
        test(f"Prev resolved slow >{s}s → Down", lambda f,x=s: f["prev_rt"]>x, "Down")

    print("\n--- Combo Strategies ---")
    test("Prev Dn + momentum Up >0.05 → Up",          lambda f: f["prev_res"]=="Down" and f["momentum_up"]>0.05,  "Up")
    test("Prev Up + momentum Dn >0.05 → Down",        lambda f: f["prev_res"]=="Up"   and f["momentum_up"]<-0.05, "Down")
    test("Prev Dn + momentum Up >0.05 → Down (fade)", lambda f: f["prev_res"]=="Down" and f["momentum_up"]>0.05,  "Down")
    test("Prev Up + momentum Up >0.05 → Up",          lambda f: f["prev_res"]=="Up"   and f["momentum_up"]>0.05,  "Up")
    test("Open Up + momentum Up → Up",                lambda f: f["bias"]>0.03 and f["momentum_up"]>0.03,         "Up")
    test("Open Dn + momentum Dn → Down",              lambda f: f["bias"]<-0.03 and f["momentum_up"]<-0.03,       "Down")
    test("High vol + Up momentum → Up",               lambda f: f["volatility"]>0.10 and f["momentum_up"]>0.05,   "Up")
    test("High vol + Down momentum → Down",           lambda f: f["volatility"]>0.10 and f["momentum_up"]<-0.05,  "Down")
    test("Low vol + Up bias → Up",                    lambda f: f["volatility"]<0.08 and f["bias"]>0.03,          "Up")
    test("Low vol + Down bias → Down",                lambda f: f["volatility"]<0.08 and f["bias"]<-0.03,         "Down")
    test("Dn,Dn + Dn momentum → Down",                lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Down" and f["momentum_up"]<-0.03, "Down")
    test("Up,Up + Up momentum → Up",                  lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Up"   and f["momentum_up"]>0.03,  "Up")
    test("Dn,Dn + Up momentum → Up (reversal)",       lambda f: f["prev_res"]=="Down" and f["prev2_res"]=="Down" and f["momentum_up"]>0.03,  "Up")
    test("Up,Up + Dn momentum → Down (reversal)",     lambda f: f["prev_res"]=="Up"   and f["prev2_res"]=="Up"   and f["momentum_up"]<-0.03, "Down")
    test("Prev fast + momentum Up → Up",              lambda f: f["prev_rt"]<60 and f["momentum_up"]>0.05,        "Up")
    test("Prev fast + momentum Dn → Down",            lambda f: f["prev_rt"]<60 and f["momentum_up"]<-0.05,       "Down")
    test("Open Up + Dn momentum → Down (fade)",       lambda f: f["bias"]>0.05 and f["momentum_up"]<-0.03,        "Down")
    test("Open Dn + Up momentum → Up (fade)",         lambda f: f["bias"]<-0.05 and f["momentum_up"]>0.03,        "Up")
    test("High vol + prev fast → Up",                 lambda f: f["volatility"]>0.10 and f["prev_rt"]<60,         "Up")
    test("High vol + prev fast → Down",               lambda f: f["volatility"]>0.10 and f["prev_rt"]<60,         "Down")
    test("Dn bias + prev Dn + momentum Dn → Down",    lambda f: f["bias"]<-0.03 and f["prev_res"]=="Down" and f["momentum_up"]<-0.03, "Down")
    test("Up bias + prev Up + momentum Up → Up",      lambda f: f["bias"]>0.03 and f["prev_res"]=="Up" and f["momentum_up"]>0.03,    "Up")

for db, label in DATABASES:
    run(db, label)

print("\n\nDone.")
