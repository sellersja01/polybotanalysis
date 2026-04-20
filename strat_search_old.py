"""
Comprehensive strategy search on 199h of fresh Hetzner data.
Tests 15 different entry/exit approaches across all 8 markets.
100% of candles included. Post-Mar30 fees.
"""
import sqlite3
import numpy as np
from bisect import bisect_right
from collections import defaultdict

BASE = r'.'
INTERVAL_5m = 300
INTERVAL_15m = 900
SH = 100

def fee(price):
    return price * 0.072 * (price * (1 - price))

def load_market(db_path, interval):
    conn = sqlite3.connect(db_path)
    pr = conn.execute("SELECT unix_time, price FROM asset_price WHERE price > 0 ORDER BY unix_time").fetchall()
    odds = conn.execute(
        'SELECT unix_time, outcome, ask, bid, mid FROM polymarket_odds '
        'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time'
    ).fetchall()
    conn.close()

    price_t = np.array([float(r[0]) for r in pr])
    price_p = np.array([float(r[1]) for r in pr])

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, out, ask, bid, mid in odds:
        cs = (int(float(ts)) // interval) * interval
        candles[cs][out].append((float(ts), float(ask), float(bid), float(mid)))

    return price_t, price_p, candles

def get_winner(sides):
    if not sides['Up'] or not sides['Down']:
        return None
    return 'Up' if sides['Up'][-1][3] >= sides['Down'][-1][3] else 'Down'

def get_entry_at(sides, side, time_offset_min, time_offset_max, cs, interval):
    """Get ask price for a side within a time window"""
    ticks = sides[side]
    for ts, ask, bid, mid in ticks:
        offset = ts - cs
        if time_offset_min <= offset <= time_offset_max:
            return ask, mid
    return None, None

# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY FUNCTIONS — each returns list of PnL results
# ═══════════════════════════════════════════════════════════════════════════

def strat_resolution_scalp(candles, interval, threshold=0.85, entry_after=0.8):
    """S1: Buy leading side at t+80% if mid >= threshold. Hold to resolution."""
    results = []
    t_entry = int(interval * entry_after)
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        lu=la=ld=lda=0; entered=False
        for t,s,a,b,m in all_t:
            if s=='Up': lu=m; la=a
            else: ld=m; lda=a
            if t-cs < t_entry or entered or lu==0 or ld==0: continue
            if lu >= threshold and lu > ld:
                es='Up'; ea=la; entered=True
            elif ld >= threshold and ld > lu:
                es='Down'; ea=lda; entered=True
        if not entered: results.append(0.0); continue
        cost = ea*SH + fee(ea)*SH
        results.append((SH - cost) if es==winner else (0 - cost))
    return results

def strat_latency_arb(price_t, price_p, candles, interval, lookback=15, move_thresh=0.05, cooldown=2, exit_secs=30):
    """S5: CEX moves >= threshold in lookback secs, Poly mid < 0.55. Exit after exit_secs."""
    if len(price_t) < 20: return []
    sample_t = np.arange(price_t[0], price_t[-1], 1.0)
    price_1s = np.interp(sample_t, price_t, price_p)

    up_t=[]; up_a=[]; up_m=[]; dn_t=[]; dn_a=[]; dn_m=[]
    for cs, sides in candles.items():
        for t,a,b,m in sides.get('Up',[]): up_t.append(t); up_a.append(a); up_m.append(m)
        for t,a,b,m in sides.get('Down',[]): dn_t.append(t); dn_a.append(a); dn_m.append(m)
    up_t=np.array(up_t); up_a=np.array(up_a); up_m=np.array(up_m)
    dn_t=np.array(dn_t); dn_a=np.array(dn_a); dn_m=np.array(dn_m)
    if len(up_t)==0 or len(dn_t)==0: return []

    cw = {}
    for cs, sides in candles.items():
        w = get_winner(sides)
        if w: cw[cs] = w

    results = []; last=0
    for i in range(lookback, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last < cooldown: continue
        move = (price_1s[i] - price_1s[i-lookback]) / price_1s[i-lookback] * 100
        if abs(move) < move_thresh: continue
        d = 'up' if move > 0 else 'down'
        ui = bisect_right(up_t, t_now) - 1
        di = bisect_right(dn_t, t_now) - 1
        if ui<0 or di<0: continue
        if d=='up': ask=float(up_a[ui]); mid=float(up_m[ui])
        else: ask=float(dn_a[di]); mid=float(dn_m[di])
        if mid > 0.55 or ask < 0.25 or ask > 0.75: continue
        # No entry in last 30s
        cs_start = (int(t_now)//interval)*interval
        if t_now - cs_start > interval - 30: continue
        f = fee(ask)
        cost = ask + f
        # Exit at mid after exit_secs
        t_exit = t_now + exit_secs
        if d=='up':
            ei = bisect_right(up_t, t_exit) - 1
            exit_mid = float(up_m[ei]) if ei>=0 else 0
        else:
            ei = bisect_right(dn_t, t_exit) - 1
            exit_mid = float(dn_m[ei]) if ei>=0 else 0
        pnl = (exit_mid - cost) * SH
        results.append(pnl)
        last = t_now
    return results

def strat_latency_resolution(price_t, price_p, candles, interval, lookback=15, move_thresh=0.05, cooldown=30):
    """S5b: Same as latency arb but hold to candle resolution."""
    if len(price_t) < 20: return []
    sample_t = np.arange(price_t[0], price_t[-1], 1.0)
    price_1s = np.interp(sample_t, price_t, price_p)

    up_t=[]; up_a=[]; up_m=[]; dn_t=[]; dn_a=[]; dn_m=[]
    for cs, sides in candles.items():
        for t,a,b,m in sides.get('Up',[]): up_t.append(t); up_a.append(a); up_m.append(m)
        for t,a,b,m in sides.get('Down',[]): dn_t.append(t); dn_a.append(a); dn_m.append(m)
    up_t=np.array(up_t); up_a=np.array(up_a); up_m=np.array(up_m)
    dn_t=np.array(dn_t); dn_a=np.array(dn_a); dn_m=np.array(dn_m)
    if len(up_t)==0 or len(dn_t)==0: return []

    cw = {}
    for cs, sides in candles.items():
        w = get_winner(sides)
        if w: cw[cs] = w

    results = []; last=0
    for i in range(lookback, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last < cooldown: continue
        move = (price_1s[i] - price_1s[i-lookback]) / price_1s[i-lookback] * 100
        if abs(move) < move_thresh: continue
        d = 'up' if move > 0 else 'down'
        ui = bisect_right(up_t, t_now) - 1
        di = bisect_right(dn_t, t_now) - 1
        if ui<0 or di<0: continue
        if d=='up': ask=float(up_a[ui]); mid=float(up_m[ui])
        else: ask=float(dn_a[di]); mid=float(dn_m[di])
        if mid > 0.55 or ask < 0.25 or ask > 0.75: continue
        cs_now = (int(t_now)//interval)*interval
        if t_now - cs_now > interval - 30: continue
        f = fee(ask)
        cost = ask + f
        w = cw.get(cs_now)
        if w is None: continue
        if (d=='up' and w=='Up') or (d=='down' and w=='Down'):
            pnl = (1.0 - cost) * SH
        else:
            pnl = (0 - cost) * SH
        results.append(pnl)
        last = t_now
    return results

def strat_penny_reversal(candles, interval, max_ask=0.15):
    """Buy when ask <= max_ask, hold to resolution."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        fired = False
        for side_name in ['Up', 'Down']:
            for ts, ask, bid, mid in sides[side_name]:
                if ask <= max_ask and ask > 0:
                    cost = (ask + fee(ask)) * SH
                    if side_name == winner: pnl = SH - cost
                    else: pnl = 0 - cost
                    results.append(pnl)
                    fired = True
                    break
        if not fired: results.append(0.0)
    return results

def strat_expensive_momentum(candles, interval, min_ask=0.80):
    """Buy when ask >= min_ask (winner momentum). First entry only."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        entered=False
        for t,s,a,b,m in all_t:
            if not entered and a >= min_ask:
                cost = a*SH + fee(a)*SH
                results.append((SH-cost) if s==winner else (0-cost))
                entered=True
                break
        if not entered: results.append(0.0)
    return results

def strat_divergence(candles, interval, trigger=0.25):
    """Buy both sides when either mid drops to trigger. Sell loser at 0.20."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        lu=la=ld=lda=0; entered=False; ua=da=0; ue=de=None
        for t,s,a,b,m in all_t:
            if s=='Up': lu=m; la=a
            else: ld=m; lda=a
            if lu==0 or ld==0: continue
            if not entered and (lu<=trigger or ld<=trigger):
                ua=la; da=lda; entered=True
            if entered:
                if s=='Up' and ue is None and m<=0.20: ue=max(0,2*m-a)
                if s=='Down' and de is None and m<=0.20: de=max(0,2*m-a)
        if not entered: results.append(0.0); continue
        cpp = ua+da+fee(ua)+fee(da)
        sh = int(100/cpp) if cpp>0 else 0
        if sh<1: results.append(0.0); continue
        uc=ua*sh+fee(ua)*sh; dc=da*sh+fee(da)*sh
        if winner=='Up':
            wp=(1.0*sh-uc) if ue is None else (ue*sh-uc)
            lp=(de*sh-dc) if de is not None else (0-dc)
        else:
            wp=(1.0*sh-dc) if de is None else (de*sh-dc)
            lp=(ue*sh-uc) if ue is not None else (0-uc)
        results.append(wp+lp)
    return results

def strat_early_leader(candles, interval, entry_window=(15,60)):
    """Buy whichever side is leading at t+15-60s. Hold to resolution."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        lu=la=ld=lda=0; entered=False
        for t,s,a,b,m in all_t:
            if s=='Up': lu=m; la=a
            else: ld=m; lda=a
            offset = t-cs
            if entered or lu==0 or ld==0: continue
            if entry_window[0] <= offset <= entry_window[1]:
                if lu > ld and lu > 0.55:
                    cost=la*SH+fee(la)*SH
                    results.append((SH-cost) if 'Up'==winner else (0-cost))
                    entered=True
                elif ld > lu and ld > 0.55:
                    cost=lda*SH+fee(lda)*SH
                    results.append((SH-cost) if 'Down'==winner else (0-cost))
                    entered=True
        if not entered: results.append(0.0)
    return results

def strat_mid_candle_momentum(candles, interval):
    """At t+150s (midpoint), buy whichever side is leading if mid > 0.60."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        lu=la=ld=lda=0; entered=False
        target = cs + interval*0.5
        for t,s,a,b,m in all_t:
            if s=='Up': lu=m; la=a
            else: ld=m; lda=a
            if entered or lu==0 or ld==0: continue
            if t >= target:
                if lu > ld and lu > 0.60:
                    cost=la*SH+fee(la)*SH
                    results.append((SH-cost) if 'Up'==winner else (0-cost))
                    entered=True
                elif ld > lu and ld > 0.60:
                    cost=lda*SH+fee(lda)*SH
                    results.append((SH-cost) if 'Down'==winner else (0-cost))
                    entered=True
        if not entered: results.append(0.0)
    return results

def strat_spread_arb(candles, interval, min_spread=0.08):
    """Buy when bid-ask spread > min_spread (market maker gap)."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        entered = False
        for side_name in ['Up', 'Down']:
            if entered: break
            for ts, ask, bid, mid in sides[side_name]:
                spread = ask - bid
                if spread >= min_spread and 0.25 <= ask <= 0.75:
                    cost = ask*SH + fee(ask)*SH
                    if side_name==winner: pnl=SH-cost
                    else: pnl=0-cost
                    results.append(pnl)
                    entered=True
                    break
        if not entered: results.append(0.0)
    return results

def strat_first_move(price_t, price_p, candles, interval, move_thresh=0.03):
    """Buy first time CEX moves >= thresh from candle open. Hold to resolution."""
    if len(price_t) < 20: return []
    sample_t = np.arange(price_t[0], price_t[-1], 1.0)
    price_1s = np.interp(sample_t, price_t, price_p)

    candle_opens = {}
    for i, ts in enumerate(price_t):
        cs = (int(ts)//interval)*interval
        if cs not in candle_opens: candle_opens[cs] = float(price_p[i])

    up_t=[]; up_a=[]; up_m=[]; dn_t=[]; dn_a=[]; dn_m=[]
    for cs, sides in candles.items():
        for t,a,b,m in sides.get('Up',[]): up_t.append(t); up_a.append(a); up_m.append(m)
        for t,a,b,m in sides.get('Down',[]): dn_t.append(t); dn_a.append(a); dn_m.append(m)
    up_t=np.array(up_t); up_a=np.array(up_a); up_m=np.array(up_m)
    dn_t=np.array(dn_t); dn_a=np.array(dn_a); dn_m=np.array(dn_m)
    if len(up_t)==0 or len(dn_t)==0: return []

    cw = {}
    for cs, sides in candles.items():
        w = get_winner(sides)
        if w: cw[cs] = w

    candle_entered = set()
    results = []
    for i in range(len(sample_t)):
        t_now = sample_t[i]
        cs = (int(t_now)//interval)*interval
        if cs in candle_entered: continue
        offset = t_now - cs
        if offset < 10 or offset > interval-30: continue
        op = candle_opens.get(cs)
        if op is None: continue
        move = (price_1s[i] - op) / op * 100
        if abs(move) < move_thresh: continue
        d = 'up' if move > 0 else 'down'
        ui = bisect_right(up_t, t_now)-1
        di = bisect_right(dn_t, t_now)-1
        if ui<0 or di<0: continue
        if d=='up': ask=float(up_a[ui]); mid=float(up_m[ui])
        else: ask=float(dn_a[di]); mid=float(dn_m[di])
        if mid > 0.55 or ask<=0 or ask>0.75: continue
        w = cw.get(cs)
        if w is None: continue
        cost = ask + fee(ask)
        if (d=='up' and w=='Up') or (d=='down' and w=='Down'):
            results.append((1.0-cost)*SH)
        else:
            results.append((0-cost)*SH)
        candle_entered.add(cs)
    # Add 0 for candles with no entry
    total_candles = len(candles)
    while len(results) < total_candles:
        results.append(0.0)
    return results

def strat_both_sides_cheap(candles, interval, max_combined=0.90):
    """Buy both Up and Down when combined ask < max_combined."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        lu=la=ld=lda=0; entered=False
        for t,s,a,b,m in all_t:
            if s=='Up': lu=m; la=a
            else: ld=m; lda=a
            if entered or la<=0 or lda<=0: continue
            combined = la + lda
            if combined < max_combined and la > 0.05 and lda > 0.05:
                cost = (la+lda+fee(la)+fee(lda))*SH
                pnl = SH - cost  # guaranteed $1 payout
                results.append(pnl)
                entered=True
        if not entered: results.append(0.0)
    return results

def strat_volatility_dca(price_t, price_p, candles, interval, vol_thresh=0.15):
    """When CEX 1-min volatility > thresh%, buy cheap side. Hold to resolution."""
    if len(price_t) < 60: return []
    sample_t = np.arange(price_t[0], price_t[-1], 1.0)
    price_1s = np.interp(sample_t, price_t, price_p)

    up_t=[]; up_a=[]; up_m=[]; dn_t=[]; dn_a=[]; dn_m=[]
    for cs, sides in candles.items():
        for t,a,b,m in sides.get('Up',[]): up_t.append(t); up_a.append(a); up_m.append(m)
        for t,a,b,m in sides.get('Down',[]): dn_t.append(t); dn_a.append(a); dn_m.append(m)
    up_t=np.array(up_t); up_a=np.array(up_a); up_m=np.array(up_m)
    dn_t=np.array(dn_t); dn_a=np.array(dn_a); dn_m=np.array(dn_m)
    if len(up_t)==0 or len(dn_t)==0: return []

    cw = {}
    for cs, sides in candles.items():
        w = get_winner(sides)
        if w: cw[cs] = w

    results=[]; last=0
    for i in range(60, len(sample_t)):
        t_now = sample_t[i]
        if t_now - last < 30: continue
        # 1-min volatility = max-min / mean
        window = price_1s[i-60:i]
        vol = (np.max(window) - np.min(window)) / np.mean(window) * 100
        if vol < vol_thresh: continue
        # Buy cheapest side
        ui = bisect_right(up_t, t_now)-1
        di = bisect_right(dn_t, t_now)-1
        if ui<0 or di<0: continue
        u_ask=float(up_a[ui]); d_ask=float(dn_a[di])
        if u_ask < d_ask and u_ask > 0.05:
            side='Up'; ask=u_ask
        elif d_ask > 0.05:
            side='Down'; ask=d_ask
        else: continue
        if ask > 0.45: continue  # only buy cheap side
        cs = (int(t_now)//interval)*interval
        w = cw.get(cs)
        if w is None: continue
        cost = ask + fee(ask)
        if side==w: results.append((1.0-cost)*SH)
        else: results.append((0-cost)*SH)
        last = t_now
    return results

def strat_candle_open_bias(candles, interval):
    """If first tick mid > 0.55 for one side at candle open, buy it."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        # Get first tick
        first_up = sides['Up'][0] if sides['Up'] else None
        first_dn = sides['Down'][0] if sides['Down'] else None
        if not first_up or not first_dn: results.append(0.0); continue
        up_mid = first_up[3]; dn_mid = first_dn[3]
        up_ask = first_up[1]; dn_ask = first_dn[1]
        entered = False
        if up_mid > 0.55 and up_mid > dn_mid:
            cost = up_ask*SH+fee(up_ask)*SH
            results.append((SH-cost) if winner=='Up' else (0-cost))
            entered = True
        elif dn_mid > 0.55 and dn_mid > up_mid:
            cost = dn_ask*SH+fee(dn_ask)*SH
            results.append((SH-cost) if winner=='Down' else (0-cost))
            entered = True
        if not entered: results.append(0.0)
    return results

def strat_late_contrarian(candles, interval):
    """At t+240s, if one side is losing (mid < 0.20), buy it. Pure reversal bet."""
    results = []
    for cs, sides in candles.items():
        winner = get_winner(sides)
        if winner is None: results.append(0.0); continue
        all_t = sorted(
            [(t,'Up',a,b,m) for t,a,b,m in sides['Up']] +
            [(t,'Down',a,b,m) for t,a,b,m in sides['Down']]
        )
        lu=la=ld=lda=0; entered=False
        for t,s,a,b,m in all_t:
            if s=='Up': lu=m; la=a
            else: ld=m; lda=a
            if entered or lu==0 or ld==0: continue
            if t-cs >= interval*0.8:
                if lu < 0.20 and la > 0:
                    cost=la*SH+fee(la)*SH
                    results.append((SH-cost) if winner=='Up' else (0-cost))
                    entered=True
                elif ld < 0.20 and lda > 0:
                    cost=lda*SH+fee(lda)*SH
                    results.append((SH-cost) if winner=='Down' else (0-cost))
                    entered=True
        if not entered: results.append(0.0)
    return results

# ═══════════════════════════════════════════════════════════════════════════
# RUN ALL STRATEGIES
# ═══════════════════════════════════════════════════════════════════════════

MARKETS_5m = [
    ('BTC_5m', f'{BASE}/market_btc_5m.db', 300),
    ('ETH_5m', f'{BASE}/market_eth_5m.db', 300),
    ('SOL_5m', f'{BASE}/market_sol_5m.db', 300),
    ('XRP_5m', f'{BASE}/market_xrp_5m.db', 300),
]

def report(name, results):
    if not results: return None
    active = [r for r in results if r != 0]
    if not active: return None
    n = len(results)
    n_active = len(active)
    wins = sum(1 for r in active if r > 0)
    net = sum(results)
    avg_win = np.mean([r for r in active if r > 0]) if wins else 0
    avg_loss = np.mean([r for r in active if r <= 0]) if n_active-wins else 0
    return {
        'name': name, 'n': n, 'active': n_active, 'wins': wins,
        'wr': 100*wins/n_active if n_active else 0, 'net': net,
        'ppt': net/n_active if n_active else 0,
        'avg_win': avg_win, 'avg_loss': avg_loss,
    }

print("=" * 100)
print(f"  COMPREHENSIVE STRATEGY SEARCH — 199h Hetzner data (Apr 8-16)")
print(f"  Testing 15 strategies across 4 markets (5m candles)")
print(f"  100 shares per trade, post-Mar30 fees, 100% candles")
print("=" * 100)

all_results = []

for mkt_label, db_path, interval in MARKETS_5m:
    print(f"\n{'-'*50}")
    print(f"  {mkt_label}")
    print(f"{'-'*50}")

    price_t, price_p, candles = load_market(db_path, interval)
    hours = (price_t[-1] - price_t[0]) / 3600
    print(f"  {hours:.0f}h, {len(candles)} candles\n")

    strats = [
        ("S1  Resolution Scalp (0.85)", lambda: strat_resolution_scalp(candles, interval, 0.85)),
        ("S2  Resolution Scalp (0.90)", lambda: strat_resolution_scalp(candles, interval, 0.90)),
        ("S3  Latency Arb (30s exit)", lambda: strat_latency_arb(price_t, price_p, candles, interval, exit_secs=30)),
        ("S4  Latency Arb (60s exit)", lambda: strat_latency_arb(price_t, price_p, candles, interval, exit_secs=60)),
        ("S5  Latency->Resolution", lambda: strat_latency_resolution(price_t, price_p, candles, interval)),
        ("S6  Penny Reversal (<=0.10)", lambda: strat_penny_reversal(candles, interval, 0.10)),
        ("S7  Penny Reversal (<=0.15)", lambda: strat_penny_reversal(candles, interval, 0.15)),
        ("S8  Expensive Momentum(0.80)", lambda: strat_expensive_momentum(candles, interval, 0.80)),
        ("S9  Divergence (both@0.25)", lambda: strat_divergence(candles, interval, 0.25)),
        ("S10 Early Leader (15-60s)", lambda: strat_early_leader(candles, interval)),
        ("S11 Mid-Candle Momentum", lambda: strat_mid_candle_momentum(candles, interval)),
        ("S12 Both Sides Cheap (<0.90)", lambda: strat_both_sides_cheap(candles, interval, 0.90)),
        ("S13 First CEX Move (0.03%)", lambda: strat_first_move(price_t, price_p, candles, interval, 0.03)),
        ("S14 Candle Open Bias", lambda: strat_candle_open_bias(candles, interval)),
        ("S15 Late Contrarian (<0.20)", lambda: strat_late_contrarian(candles, interval)),
    ]

    print(f"  {'Strategy':<30} {'Active':<8} {'WR%':<7} {'$/trade':<10} {'Net PnL':<12} {'$/day':<10}")
    print(f"  {'-'*80}")

    for name, func in strats:
        try:
            r = func()
            rpt = report(name, r)
            if rpt:
                daily = rpt['net']/hours*24
                print(f"  {rpt['name']:<30} {rpt['active']:<8} {rpt['wr']:<6.1f}% {rpt['ppt']:<+9.2f} {rpt['net']:<+11.0f} {daily:<+9.0f}")
                all_results.append({**rpt, 'market': mkt_label, 'daily': daily})
            else:
                print(f"  {name:<30} {'N/A':<8}")
        except Exception as e:
            print(f"  {name:<30} ERROR: {str(e).encode('ascii','replace').decode()[:50]}")

print(f"\n\n{'='*100}")
print(f"  TOP 15 PROFITABLE STRATEGIES (sorted by $/day)")
print(f"{'='*100}")
profitable = [r for r in all_results if r['net'] > 0]
profitable.sort(key=lambda x: x['daily'], reverse=True)
print(f"\n  {'#':<4} {'Market':<10} {'Strategy':<30} {'Active':<8} {'WR%':<7} {'$/trade':<10} {'Net PnL':<12} {'$/day':<10}")
print(f"  {'-'*95}")
for i, r in enumerate(profitable[:15]):
    print(f"  {i+1:<4} {r['market']:<10} {r['name']:<30} {r['active']:<8} {r['wr']:<6.1f}% {r['ppt']:<+9.2f} {r['net']:<+11.0f} {r['daily']:<+9.0f}")

print(f"\n\n{'='*100}")
print(f"  BOTTOM 5 WORST STRATEGIES")
print(f"{'='*100}")
all_results.sort(key=lambda x: x['daily'])
print(f"\n  {'Market':<10} {'Strategy':<30} {'Active':<8} {'WR%':<7} {'$/trade':<10} {'$/day':<10}")
print(f"  {'-'*80}")
for r in all_results[:5]:
    print(f"  {r['market']:<10} {r['name']:<30} {r['active']:<8} {r['wr']:<6.1f}% {r['ppt']:<+9.2f} {r['daily']:<+9.0f}")
