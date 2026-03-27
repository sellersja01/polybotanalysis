"""
wallet7_loss_analysis.py v3
============================
Efficient version — only queries the 659 markets wallet_7 actually traded.
Uses proper GROUP BY instead of correlated subqueries.

Run on VPS: python3 wallet7_loss_analysis.py
"""

import sqlite3
from collections import defaultdict

WALLET_DB  = '/home/opc/wallet_trades.db'
ODDS_DB    = '/home/opc/market_btc_5m.db'
WALLET     = 'wallet_7'
WIN_THRESH = 0.75

# ── 1. Load wallet_7 positions per candle ─────────────────────────────────────
print("Loading wallet_7 positions...", flush=True)
conn_w = sqlite3.connect(WALLET_DB)
rows = conn_w.execute("""
    SELECT market, outcome, SUM(size) as shares, SUM(usdc) as usdc, AVG(price) as avg_p
    FROM trades
    WHERE wallet_name=? AND side='BUY'
    GROUP BY market, outcome
""", (WALLET,)).fetchall()
conn_w.close()

candles = defaultdict(dict)
for market, outcome, shares, usdc, avg_p in rows:
    candles[market][outcome] = {'shares': shares, 'usdc': usdc, 'avg_p': avg_p}

both_sides = {m: d for m, d in candles.items() if 'Up' in d and 'Down' in d}
market_list = list(both_sides.keys())
print(f"  Candles with both sides: {len(both_sides)}", flush=True)

# ── 2. Pull odds data for ONLY wallet_7's markets ────────────────────────────
print("Pulling odds for wallet_7 markets only...", flush=True)
conn_o = sqlite3.connect(ODDS_DB)

# Build placeholder string for IN clause
placeholders = ','.join(['?' for _ in market_list])

# Aggregate stats per market+outcome in one shot
agg_rows = conn_o.execute(f"""
    SELECT question, outcome,
           AVG(mid) as avg_mid,
           MIN(mid) as min_mid,
           MAX(mid) as max_mid,
           MAX(unix_time) as max_t,
           MIN(unix_time) as min_t,
           COUNT(*) as n_ticks
    FROM polymarket_odds
    WHERE question IN ({placeholders})
    GROUP BY question, outcome
""", market_list).fetchall()
print(f"  Agg rows loaded: {len(agg_rows)}", flush=True)

# Get last-tick mid using efficient GROUP BY + JOIN
last_tick_rows = conn_o.execute(f"""
    SELECT o.question, o.outcome, o.mid
    FROM polymarket_odds o
    INNER JOIN (
        SELECT question, outcome, MAX(unix_time) as max_t
        FROM polymarket_odds
        WHERE question IN ({placeholders})
        GROUP BY question, outcome
    ) mx ON o.question = mx.question
         AND o.outcome  = mx.outcome
         AND o.unix_time = mx.max_t
""", market_list).fetchall()
print(f"  Last tick rows loaded: {len(last_tick_rows)}", flush=True)

conn_o.close()

# Build lookups
agg = defaultdict(dict)  # agg[question][outcome] = {...}
for q, out, avg_m, min_m, max_m, max_t, min_t, n in agg_rows:
    agg[q][out] = {'avg': avg_m, 'min': min_m, 'max': max_m,
                   'max_t': max_t, 'min_t': min_t, 'n': n}

last_mid = defaultdict(dict)
for q, out, mid in last_tick_rows:
    last_mid[q][out] = mid

# ── 3. Classify wins / losses ──────────────────────────────────────────────────
print("Classifying wins/losses...", flush=True)
wins   = []
losses = []
skipped = 0

for market, d in both_sides.items():
    lm = last_mid.get(market, {})
    if 'Up' not in lm or 'Down' not in lm:
        skipped += 1
        continue

    last_up = lm['Up']
    last_dn = lm['Down']

    if last_up >= WIN_THRESH:
        true_outcome = 'Up'
    elif last_dn >= WIN_THRESH:
        true_outcome = 'Down'
    else:
        skipped += 1
        continue

    up_sh = d['Up']['shares']
    dn_sh = d['Down']['shares']
    net_dir = 'Up' if up_sh >= dn_sh else 'Down'
    wallet7_wins = (net_dir == true_outcome)

    info = {
        'market':       market,
        'true_outcome': true_outcome,
        'net_dir':      net_dir,
        'up_shares':    up_sh,
        'dn_shares':    dn_sh,
        'up_usdc':      d['Up']['usdc'],
        'dn_usdc':      d['Down']['usdc'],
        'up_avg_p':     d['Up']['avg_p'],
        'dn_avg_p':     d['Down']['avg_p'],
        'agg':          agg.get(market, {}),
        'last_up':      last_up,
        'last_dn':      last_dn,
    }
    if wallet7_wins:
        wins.append(info)
    else:
        losses.append(info)

n = len(wins) + len(losses)
print(f"\n  Wins   : {len(wins)}")
print(f"  Losses : {len(losses)}")
print(f"  Skipped: {skipped} (no odds data or ambiguous last tick)")
if n:
    print(f"  WR     : {len(wins)/n*100:.1f}%")

# ── 4. Pattern analysis ───────────────────────────────────────────────────────
def avg(lst): return sum(lst)/len(lst) if lst else 0

def extract(infos):
    out = []
    for info in infos:
        nd = info['net_dir']
        ag = info['agg']
        if nd not in ag or ('Up' not in ag and 'Down' not in ag):
            continue
        opp = 'Down' if nd == 'Up' else 'Up'
        if opp not in ag:
            continue

        our   = ag[nd]
        their = ag[opp]
        up_sh = info['up_shares']
        dn_sh = info['dn_shares']
        our_sh  = up_sh if nd == 'Up' else dn_sh
        thr_sh  = dn_sh if nd == 'Up' else up_sh
        our_u   = info['up_usdc'] if nd == 'Up' else info['dn_usdc']
        thr_u   = info['dn_usdc'] if nd == 'Up' else info['up_usdc']
        our_p   = info['up_avg_p'] if nd == 'Up' else info['dn_avg_p']
        thr_p   = info['dn_avg_p'] if nd == 'Up' else info['up_avg_p']

        out.append({
            'avg_our':    our['avg'],
            'avg_their':  their['avg'],
            'min_our':    our['min'],
            'min_their':  their['min'],
            'max_our':    our['max'],
            'max_their':  their['max'],
            'duration':   (our['max_t'] - our['min_t']) if our.get('max_t') else 0,
            'our_avg_p':  our_p,
            'thr_avg_p':  thr_p,
            'share_ratio': our_sh / thr_sh if thr_sh > 0 else 1.0,
            'usdc_ratio':  our_u  / thr_u  if thr_u  > 0 else 1.0,
            'our_usdc':   our_u,
            'thr_usdc':   thr_u,
        })
    return out

win_ex  = extract(wins)
loss_ex = extract(losses)

def report(ex, label):
    if not ex:
        print(f"\n  No data for {label}")
        return
    print(f"\n  {'='*58}")
    print(f"  {label}  (n={len(ex)})")
    print(f"  {'='*58}")
    print(f"  Entry price — our side          : {avg([e['our_avg_p']  for e in ex]):.4f}")
    print(f"  Entry price — their side        : {avg([e['thr_avg_p']  for e in ex]):.4f}")
    print(f"  Share ratio (our/their)         : {avg([e['share_ratio'] for e in ex]):.3f}")
    print(f"  USDC ratio  (our/their)         : {avg([e['usdc_ratio']  for e in ex]):.3f}")
    print(f"  Avg mid — our side (candle)     : {avg([e['avg_our']   for e in ex]):.4f}")
    print(f"  Avg mid — their side (candle)   : {avg([e['avg_their'] for e in ex]):.4f}")
    print(f"  Min mid — our side              : {avg([e['min_our']   for e in ex]):.4f}")
    print(f"  Min mid — their side            : {avg([e['min_their'] for e in ex]):.4f}")
    print(f"  Max mid — our side              : {avg([e['max_our']   for e in ex]):.4f}")
    print(f"  Max mid — their side            : {avg([e['max_their'] for e in ex]):.4f}")
    print(f"  Avg duration (s)                : {avg([e['duration']  for e in ex]):.0f}")

    strong = sum(1 for e in ex if e['share_ratio'] > 1.5)
    deep   = sum(1 for e in ex if e['min_our'] < 0.30)
    print(f"  Bet >1.5× on our side          : {strong}/{len(ex)} ({strong/len(ex)*100:.0f}%)")
    print(f"  Our side dipped below 0.30      : {deep}/{len(ex)} ({deep/len(ex)*100:.0f}%)")

print(f"\n{'='*62}")
print(f"  WALLET_7 WIN vs LOSS PATTERN COMPARISON")
print(f"{'='*62}")
report(win_ex,  "WINNING CANDLES")
report(loss_ex, "LOSING CANDLES")

# ── 5. Share ratio WR by bucket ───────────────────────────────────────────────
print(f"\n  {'='*58}")
print(f"  WIN RATE BY SHARE RATIO (directional conviction)")
print(f"  {'='*58}")
print(f"  {'Ratio':>12}  {'W':>5}  {'L':>5}  {'WR%':>6}")
print(f"  {'-'*35}")
buckets = [('<0.75', 0, 0.75), ('0.75-0.90', 0.75, 0.90), ('0.90-1.10', 0.90, 1.10),
           ('1.10-1.33', 1.10, 1.33), ('1.33-2.0', 1.33, 2.0), ('>2.0', 2.0, 99)]
for lbl, lo, hi in buckets:
    w = sum(1 for e in win_ex  if lo <= e['share_ratio'] < hi)
    l = sum(1 for e in loss_ex if lo <= e['share_ratio'] < hi)
    t = w + l
    print(f"  {lbl:>12}  {w:>5}  {l:>5}  {w/t*100:>6.1f}%" if t else f"  {lbl:>12}  {w:>5}  {l:>5}  {'N/A':>6}")

# ── 6. Loss candle table ───────────────────────────────────────────────────────
print(f"\n  {'='*58}")
print(f"  LOSS CANDLES — net direction was WRONG")
print(f"  {'='*58}")
print(f"  {'Market':<50} {'Net':>4} {'True':>5} {'Ratio':>6} {'Our$':>8}")
print(f"  {'-'*80}")
for info in sorted(losses, key=lambda x: x['market']):
    ex = extract([info])
    ratio = f"{ex[0]['share_ratio']:.2f}" if ex else '?'
    our_u = info['up_usdc'] if info['net_dir'] == 'Up' else info['dn_usdc']
    print(f"  {info['market'][:49]:<49} {info['net_dir']:>4} {info['true_outcome']:>5} {ratio:>6} ${our_u:>7,.0f}")

print(f"\n{'='*62}\n")
