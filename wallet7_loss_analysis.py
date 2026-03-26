"""
wallet7_loss_analysis.py
========================
For each candle wallet_7 traded:
  1. Calculate their net position (Up-heavy vs Down-heavy)
  2. Resolve via Polymarket API
  3. Flag wins vs losses
  4. For ALL candles, pull odds trajectory from market_btc_5m.db
  5. Compare patterns between winning and losing candles

Run on VPS: python3 wallet7_loss_analysis.py
"""

import sqlite3
import requests
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

WALLET_DB  = '/home/opc/wallet_trades.db'
ODDS_DB    = '/home/opc/market_btc_5m.db'
WALLET     = 'wallet_7'

# ── 1. Get all wallet_7 candles with net position ─────────────────────────────
print("Loading wallet_7 candle positions...")
conn_w = sqlite3.connect(WALLET_DB)

rows = conn_w.execute("""
    SELECT
        market,
        outcome,
        SUM(size)  as total_shares,
        SUM(usdc)  as total_usdc,
        AVG(price) as avg_price,
        MIN(timestamp) as first_ts,
        MAX(timestamp) as last_ts,
        COUNT(*) as n_trades
    FROM trades
    WHERE wallet_name=? AND side='BUY'
    GROUP BY market, outcome
    ORDER BY market, outcome
""", (WALLET,)).fetchall()
conn_w.close()

# Group by market
candles = defaultdict(dict)
for market, outcome, shares, usdc, avg_p, first_ts, last_ts, n in rows:
    candles[market][outcome] = {
        'shares': shares, 'usdc': usdc, 'avg_price': avg_p,
        'first_ts': first_ts, 'last_ts': last_ts, 'n_trades': n
    }

# Only keep candles where wallet_7 traded BOTH sides
both_sides = {m: d for m, d in candles.items()
              if 'Up' in d and 'Down' in d}
one_side   = {m: d for m, d in candles.items()
              if not ('Up' in d and 'Down' in d)}

print(f"  Candles with both sides : {len(both_sides)}")
print(f"  Candles with one side   : {len(one_side)}")
print(f"  Total candles           : {len(candles)}")

# ── 2. Resolve via Polymarket API ─────────────────────────────────────────────
print("\nResolving candles via Polymarket API...")

def resolve_market(market_name):
    """Returns ('Up'|'Down'|None, market_id)"""
    try:
        slug = market_name.lower().replace(' ', '-').replace(',', '').replace('.', '').replace(':', '')
        # Try gamma API search by question
        r = requests.get(
            'https://gamma-api.polymarket.com/markets',
            params={'question': market_name, 'limit': 5},
            timeout=10
        )
        data = r.json()
        for m in data:
            if m.get('question', '').strip() == market_name.strip():
                outcomes = json.loads(m.get('outcomePrices', '[0,0]'))
                market_id = str(m.get('id', ''))
                if float(outcomes[0]) >= 0.99:
                    return 'Up', market_id
                elif float(outcomes[1]) >= 0.99:
                    return 'Down', market_id
                else:
                    # Try clobTokenIds resolution
                    tokens = json.loads(m.get('clobTokenIds', '[]'))
                    if tokens:
                        return None, market_id  # unresolved
    except Exception as e:
        pass
    return None, None

# Use the approach from previous session - check outcome prices
def resolve_via_clob(market_name):
    try:
        r = requests.get(
            'https://gamma-api.polymarket.com/markets',
            params={'question': market_name, 'limit': 3},
            timeout=10
        )
        data = r.json()
        for m in data:
            q = m.get('question', '').strip()
            if q == market_name.strip() or market_name.strip() in q:
                prices = json.loads(m.get('outcomePrices', '[0.5,0.5]'))
                market_id = str(m.get('id', ''))
                cond_id = m.get('conditionId', '')
                p_up = float(prices[0])
                p_dn = float(prices[1])
                if p_up > 0.99:
                    return 'Up', market_id, cond_id
                elif p_dn > 0.99:
                    return 'Down', market_id, cond_id
                elif p_up < 0.01:
                    return 'Down', market_id, cond_id
                elif p_dn < 0.01:
                    return 'Up', market_id, cond_id
                else:
                    return None, market_id, cond_id
    except:
        pass
    return None, None, None

resolved   = {}  # market_name -> ('Up'|'Down', market_id)
unresolved = []

market_list = list(both_sides.keys())
for i, market in enumerate(market_list):
    if i % 20 == 0:
        print(f"  [{i}/{len(market_list)}] resolving...")
    outcome, mid, cond = resolve_via_clob(market)
    if outcome:
        resolved[market] = (outcome, mid)
    else:
        unresolved.append((market, mid))
    time.sleep(0.15)

print(f"  Resolved: {len(resolved)} | Unresolved: {len(unresolved)}")

# ── 3. Classify wins/losses ───────────────────────────────────────────────────
wins   = []
losses = []

for market, (true_outcome, market_id) in resolved.items():
    d = both_sides[market]
    up_shares = d['Up']['shares']
    dn_shares = d['Down']['shares']
    net_dir = 'Up' if up_shares > dn_shares else 'Down'
    wallet7_wins = (net_dir == true_outcome)

    info = {
        'market':       market,
        'market_id':    market_id,
        'true_outcome': true_outcome,
        'net_dir':      net_dir,
        'up_shares':    up_shares,
        'dn_shares':    dn_shares,
        'up_usdc':      d['Up']['usdc'],
        'dn_usdc':      d['Down']['usdc'],
        'up_avg':       d['Up']['avg_price'],
        'dn_avg':       d['Down']['avg_price'],
        'first_ts':     min(d['Up']['first_ts'], d['Down']['first_ts']),
        'last_ts':      max(d['Up']['last_ts'], d['Down']['last_ts']),
        'n_trades':     d['Up']['n_trades'] + d['Down']['n_trades'],
    }
    if wallet7_wins:
        wins.append(info)
    else:
        losses.append(info)

print(f"\n  Wins  : {len(wins)}")
print(f"  Losses: {len(losses)}")
total = len(wins) + len(losses)
if total:
    print(f"  WR    : {len(wins)/total*100:.1f}%")

# ── 4. Pull odds trajectory from market_btc_5m.db ────────────────────────────
print("\nPulling odds trajectories from market_btc_5m.db...")
conn_o = sqlite3.connect(ODDS_DB)

def get_odds_trajectory(market_name):
    """Returns list of (unix_time, up_mid, dn_mid) for a market."""
    rows = conn_o.execute("""
        SELECT o1.unix_time, o1.mid as up_mid, o2.mid as dn_mid
        FROM polymarket_odds o1
        JOIN polymarket_odds o2
          ON ABS(o1.unix_time - o2.unix_time) < 2
         AND o1.market_id = o2.market_id
         AND o1.outcome = 'Up'
         AND o2.outcome = 'Down'
        WHERE o1.question = ?
        ORDER BY o1.unix_time
    """, (market_name,)).fetchall()
    return rows

def traj_stats(traj, net_dir):
    """Extract key stats from an odds trajectory."""
    if not traj:
        return None
    times    = [r[0] for r in traj]
    up_mids  = [r[1] for r in traj]
    dn_mids  = [r[2] for r in traj]

    start_up = up_mids[0]
    start_dn = dn_mids[0]
    end_up   = up_mids[-1]
    end_dn   = dn_mids[-1]
    duration = times[-1] - times[0]

    # Min price of net_dir side (how cheap did our side get?)
    if net_dir == 'Up':
        our_mids   = up_mids
        their_mids = dn_mids
    else:
        our_mids   = dn_mids
        their_mids = up_mids

    min_our   = min(our_mids)
    max_their = max(their_mids)
    start_our = our_mids[0]
    end_our   = our_mids[-1]

    # Time to min (when did our side hit its lowest?)
    min_idx = our_mids.index(min_our)
    time_to_min = (times[min_idx] - times[0]) / max(duration, 1)

    # Max divergence reached
    max_div = max(abs(u - d) for u, d in zip(up_mids, dn_mids))

    return {
        'start_up': start_up, 'start_dn': start_dn,
        'end_up': end_up, 'end_dn': end_dn,
        'start_our': start_our, 'end_our': end_our,
        'min_our': min_our, 'max_their': max_their,
        'max_div': max_div,
        'time_to_min': time_to_min,
        'duration': duration,
        'n_ticks': len(traj),
    }

# Compute stats for wins and losses
win_stats  = []
loss_stats = []

for info in wins:
    traj = get_odds_trajectory(info['market'])
    st = traj_stats(traj, info['net_dir'])
    if st:
        win_stats.append(st)

for info in losses:
    traj = get_odds_trajectory(info['market'])
    st = traj_stats(traj, info['net_dir'])
    if st:
        loss_stats.append(st)

conn_o.close()

print(f"  Win trajectories  : {len(win_stats)}")
print(f"  Loss trajectories : {len(loss_stats)}")

# ── 5. Compare patterns ───────────────────────────────────────────────────────
def avg(lst): return sum(lst)/len(lst) if lst else 0

def report(stats, label):
    if not stats:
        print(f"  No data for {label}")
        return
    print(f"\n  {'='*55}")
    print(f"  {label}  (n={len(stats)})")
    print(f"  {'='*55}")
    print(f"  Start odds (our side)    : {avg([s['start_our'] for s in stats]):.4f}")
    print(f"  End odds   (our side)    : {avg([s['end_our'] for s in stats]):.4f}")
    print(f"  Min odds   (our side)    : {avg([s['min_our'] for s in stats]):.4f}")
    print(f"  Max divergence reached   : {avg([s['max_div'] for s in stats]):.4f}")
    print(f"  Time to min (0=start,1=end): {avg([s['time_to_min'] for s in stats]):.3f}")
    print(f"  Avg duration (s)         : {avg([s['duration'] for s in stats]):.0f}")
    print(f"  Avg ticks in DB          : {avg([s['n_ticks'] for s in stats]):.0f}")

    # Distribution of start odds
    buckets = [0]*5
    for s in stats:
        p = s['start_our']
        if p < 0.35: buckets[0] += 1
        elif p < 0.42: buckets[1] += 1
        elif p < 0.48: buckets[2] += 1
        elif p < 0.52: buckets[3] += 1
        else: buckets[4] += 1
    n = len(stats)
    print(f"\n  Start-odds distribution (our side):")
    print(f"    <0.35  : {buckets[0]:>4} ({buckets[0]/n*100:>5.1f}%)")
    print(f"    0.35-0.42: {buckets[1]:>4} ({buckets[1]/n*100:>5.1f}%)")
    print(f"    0.42-0.48: {buckets[2]:>4} ({buckets[2]/n*100:>5.1f}%)")
    print(f"    0.48-0.52: {buckets[3]:>4} ({buckets[3]/n*100:>5.1f}%)")
    print(f"    >0.52  : {buckets[4]:>4} ({buckets[4]/n*100:>5.1f}%)")

    # How often did our side recover vs stay down?
    recovered = sum(1 for s in stats if s['end_our'] > s['min_our'] + 0.05)
    print(f"\n  Side recovered >5c from min : {recovered}/{len(stats)} ({recovered/len(stats)*100:.0f}%)")

print(f"\n{'='*60}")
print(f"  WALLET_7 WINNING vs LOSING CANDLE ANALYSIS")
print(f"{'='*60}")
report(win_stats,  "WINNING CANDLES")
report(loss_stats, "LOSING CANDLES")

# ── 6. Loss candle details ────────────────────────────────────────────────────
print(f"\n  {'='*55}")
print(f"  LOSS CANDLE DETAILS (net_dir → true_outcome)")
print(f"  {'='*55}")
print(f"  {'Market':<55} {'Net':>4} {'True':>5} {'Up$':>8} {'Dn$':>8}")
print(f"  {'-'*85}")
for info in sorted(losses, key=lambda x: x['first_ts']):
    print(f"  {info['market'][:54]:<54} {info['net_dir']:>4} {info['true_outcome']:>5}  ${info['up_usdc']:>7,.0f}  ${info['dn_usdc']:>7,.0f}")

print(f"\n{'='*60}\n")
