"""
Cross-Timeframe Confirmation Backtest

Tests whether BTC_5m entries that coincide with BTC_15m also showing
divergence perform better than BTC_5m entries where 15m is still flat.

Logic:
- For each BTC_5m candle that triggers (mid <= 0.25), find the specific
  BTC_15m market that is ACTIVE at that timestamp (same time window)
- Split into:
    "CONFIRMED" = BTC_15m also diverged (mid <= threshold)
    "UNCONFIRMED" = BTC_15m still near 0.50 (lagging, hasn't caught up)
- Compare WR and ROI between the two groups
"""

import sqlite3
from collections import defaultdict
from bisect import bisect_right

DB_5M  = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
DB_15M = r'C:\Users\James\polybotanalysis\market_btc_15m.db'

INTERVAL_5M  = 300
INTERVAL_15M = 900
SHARES       = 100
ENTRY_THRESH = 0.25
EXIT_THRESH  = 0.20

def calc_fee(shares, price):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

# ── Load 15m data: build per-market tick history ──────────────────────────────
print("Loading BTC_15m data...")
conn15 = sqlite3.connect(DB_15M)
rows15 = conn15.execute(
    'SELECT unix_time, market_id, outcome, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn15.close()

# Group ticks by market_id
market_ticks_15m = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, mid in rows15:
    market_ticks_15m[mid_id][out].append((float(ts), float(mid)))

# For each 15m market: find its start time (first tick) and build a
# timeline of (ts, up_mid, dn_mid) for lookup
market_start_15m = {}   # market_id -> first tick ts
market_timeline  = {}   # market_id -> (ts_list, up_mid_list, dn_mid_list)

for mid_id, sides in market_ticks_15m.items():
    up_ticks = sorted(sides['Up'])
    dn_ticks = sorted(sides['Down'])
    if not up_ticks or not dn_ticks:
        continue

    start = min(up_ticks[0][0], dn_ticks[0][0])
    market_start_15m[mid_id] = start

    # Merge into unified timeline
    all_t = sorted(set(t for t, _ in up_ticks) | set(t for t, _ in dn_ticks))
    up_dict = {t: m for t, m in up_ticks}
    dn_dict = {t: m for t, m in dn_ticks}

    ts_list, up_list, dn_list = [], [], []
    last_up, last_dn = 0.5, 0.5
    for t in all_t:
        if t in up_dict: last_up = up_dict[t]
        if t in dn_dict: last_dn = dn_dict[t]
        ts_list.append(t)
        up_list.append(last_up)
        dn_list.append(last_dn)

    market_timeline[mid_id] = (ts_list, up_list, dn_list)

# Sort market_ids by start time for fast lookup
sorted_15m_markets = sorted(market_start_15m.items(), key=lambda x: x[1])
sorted_15m_starts  = [s for _, s in sorted_15m_markets]
sorted_15m_ids     = [m for m, _ in sorted_15m_markets]

def get_active_15m_mid(timestamp):
    """
    Find the 15m market active at `timestamp` and return its min(up_mid, dn_mid).
    The active market is the one whose start time is closest to (and before) timestamp,
    aligned to 15m candle slots.
    """
    slot = (int(timestamp) // INTERVAL_15M) * INTERVAL_15M

    # Find markets that started within this 15m slot (slot to slot+900)
    best_mid_id = None
    best_start  = -1
    for mid_id, start in sorted_15m_markets:
        if slot - 60 <= start <= slot + INTERVAL_15M:
            if start > best_start:
                best_start  = start
                best_mid_id = mid_id

    if best_mid_id is None:
        return None

    ts_list, up_list, dn_list = market_timeline[best_mid_id]
    idx = bisect_right(ts_list, timestamp) - 1
    if idx < 0:
        return None

    return min(up_list[idx], dn_list[idx])

# ── Load 5m candles ───────────────────────────────────────────────────────────
print("Loading BTC_5m data...")
conn5 = sqlite3.connect(DB_5M)
rows5 = conn5.execute(
    'SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn5.close()

candles_5m = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, ask, mid in rows5:
    cs = (int(float(ts)) // INTERVAL_5M) * INTERVAL_5M
    candles_5m[(cs, mid_id)][out].append((float(ts), float(ask), float(mid)))

# ── Run backtest ──────────────────────────────────────────────────────────────
print("Running backtest...\n")

all_candle_results = []

for (cs, mid_id), sides in candles_5m.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][2]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    all_ticks = sorted(
        [(ts, 'Up',   ask, mid) for ts, ask, mid in up_ticks] +
        [(ts, 'Down', ask, mid) for ts, ask, mid in dn_ticks]
    )

    triggered    = False
    trigger_ts   = None
    up_entry_ask = None
    dn_entry_ask = None
    last_up_ask  = None
    last_dn_ask  = None
    up_exit_bid  = None
    dn_exit_bid  = None

    for ts, side, ask, mid in all_ticks:
        if side == 'Up':  last_up_ask = ask
        else:             last_dn_ask = ask

        if not triggered and last_up_ask and last_dn_ask and mid <= ENTRY_THRESH:
            triggered    = True
            trigger_ts   = ts
            up_entry_ask = last_up_ask
            dn_entry_ask = last_dn_ask

        if triggered and mid <= EXIT_THRESH:
            if side == 'Up' and up_exit_bid is None:
                up_exit_bid = max(0.0, 2 * mid - ask)
            elif side == 'Down' and dn_exit_bid is None:
                dn_exit_bid = max(0.0, 2 * mid - ask)

    if not triggered:
        continue

    up_cost = up_entry_ask * SHARES
    dn_cost = dn_entry_ask * SHARES
    up_fee  = calc_fee(SHARES, up_entry_ask)
    dn_fee  = calc_fee(SHARES, dn_entry_ask)

    if winner == 'Up':
        up_resolve = up_exit_bid if up_exit_bid is not None else 1.0
        dn_resolve = dn_exit_bid if dn_exit_bid is not None else 0.0
    else:
        dn_resolve = dn_exit_bid if dn_exit_bid is not None else 1.0
        up_resolve = up_exit_bid if up_exit_bid is not None else 0.0

    pnl  = (up_resolve * SHARES - up_cost - up_fee) + \
           (dn_resolve * SHARES - dn_cost - dn_fee)
    cost = up_cost + dn_cost

    # What is the 15m market mid at the moment this 5m entry triggered?
    mid_15m_at_trigger = get_active_15m_mid(trigger_ts)

    all_candle_results.append({
        'pnl':    pnl,
        'cost':   cost,
        'win':    pnl > 0,
        'mid15':  mid_15m_at_trigger,
    })

# ── Print results ─────────────────────────────────────────────────────────────
def summarize(lst):
    if not lst: return None
    n    = len(lst)
    wins = sum(1 for r in lst if r['win'])
    net  = sum(r['pnl'] for r in lst)
    cost = sum(r['cost'] for r in lst)
    return dict(n=n, wr=100*wins/n, net=net, roi=100*net/cost, ppc=net/n)

def pr(label, s):
    if s:
        print(f"  {label:<30} n={s['n']:>4}  WR={s['wr']:>5.1f}%  ROI={s['roi']:>+6.2f}%  $/candle={s['ppc']:>+7.2f}")

has_data  = [r for r in all_candle_results if r['mid15'] is not None]
no_data   = [r for r in all_candle_results if r['mid15'] is None]

print(f"{'='*75}")
print(f"  CROSS-TIMEFRAME CONFIRMATION  (BTC_5m @ 0.25, matched to active 15m)")
print(f"{'='*75}\n")

pr("ALL BTC_5m triggers",    summarize(all_candle_results))
pr("  With 15m data",        summarize(has_data))
pr("  No matching 15m data", summarize(no_data))

print(f"\n  15m mid breakdown at moment of 5m trigger:")
buckets = [(0.00,0.25,'<= 0.25 (15m also diverged)'),
           (0.25,0.30,'0.25-0.30'),
           (0.30,0.35,'0.30-0.35'),
           (0.35,0.40,'0.35-0.40'),
           (0.40,0.45,'0.40-0.45'),
           (0.45,0.55,'0.45-0.55 (15m flat / lagging)')]

for lo, hi, label in buckets:
    grp = [r for r in has_data if lo <= r['mid15'] < hi]
    s   = summarize(grp)
    if s:
        pr(f"  15m={label}", s)

print(f"\n  CONFIRMATION FILTER RESULTS:")
for thresh in [0.35, 0.30, 0.25]:
    conf   = [r for r in has_data if r['mid15'] <= thresh]
    unconf = [r for r in has_data if r['mid15'] >  thresh]
    print(f"\n  -- 15m threshold <= {thresh} --")
    pr(f"  TRADE  (15m <= {thresh})", summarize(conf))
    pr(f"  SKIP   (15m >  {thresh})", summarize(unconf))
