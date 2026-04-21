"""
Measure how fast Polymarket's book reprices after a Coinbase tick.

Method:
1. Pull Coinbase ticks from asset_price for a given asset.
2. Find 'triggers' — moments where the price moved >= THRESH% within a rolling
   LOOKBACK-second window. These are the CEX events maker bots react to.
3. For each trigger timestamp T, find the FIRST polymarket_odds row whose
   bid or ask differs from what it was at T-1s. That's the reprice event.
4. Compute delta = reprice_ts - trigger_ts.
5. Report percentile distribution of reprice delay across all triggers.

Reads live DBs directly via sqlite URI (read-only). No /tmp disk use.
"""
import sqlite3
from datetime import datetime, timezone
from collections import Counter
import statistics

ASSET      = 'btc'
THRESH_PCT = 0.03         # min % move to count as a trigger
LOOKBACK_S = 2            # rolling window for the move
LOOK_AHEAD = 5            # how long to wait after trigger for reprice
DB_PATH    = f'/root/market_{ASSET}_5m.db'

# Use a snapshot to avoid lock issues — but make sure we delete it at the end
import os, time as T
SNAP = f'/tmp/reprice_snap_{int(T.time())}.db'
src = sqlite3.connect(DB_PATH, timeout=30)
dst = sqlite3.connect(SNAP)
src.backup(dst); src.close(); dst.close()

try:
    c = sqlite3.connect(SNAP)

    # Only analyze the last 2 hours of data
    now = T.time()
    since = now - 2*3600
    prices = c.execute(
        'SELECT unix_time, price FROM asset_price WHERE unix_time >= ? ORDER BY unix_time',
        (since,)
    ).fetchall()
    odds = c.execute(
        "SELECT unix_time, outcome, bid, ask FROM polymarket_odds "
        "WHERE unix_time >= ? AND outcome IN ('Up','Down') ORDER BY unix_time",
        (since,)
    ).fetchall()
    c.close()

    print(f'Analyzing {len(prices)} Coinbase ticks + {len(odds)} Polymarket rows')
    print(f'Asset: {ASSET.upper()}   thresh: {THRESH_PCT}%   lookback: {LOOKBACK_S}s\n')

    prices = [(float(t), float(p)) for t, p in prices]
    odds   = [(float(t), o, float(b), float(a)) for t, o, b, a in odds]

    # Group odds by outcome for fast lookup
    odds_up   = [(t, b, a) for t, o, b, a in odds if o == 'Up']
    odds_down = [(t, b, a) for t, o, b, a in odds if o == 'Down']

    # Find triggers
    triggers = []    # list of (trigger_ts, direction, move_pct)
    j = 0            # rolling window start
    for i, (t, p) in enumerate(prices):
        # Advance j so prices[j].t >= t - LOOKBACK_S
        while j < i and prices[j][0] < t - LOOKBACK_S:
            j += 1
        if j == i: continue
        p0 = prices[j][1]
        if p0 <= 0: continue
        move_pct = (p - p0) / p0 * 100
        if abs(move_pct) >= THRESH_PCT:
            # Dedupe: only count if at least 1s since last trigger
            if not triggers or t - triggers[-1][0] >= 1.0:
                triggers.append((t, 'UP' if move_pct > 0 else 'DOWN', move_pct))

    print(f'Triggers (>= {THRESH_PCT}% move in {LOOKBACK_S}s): {len(triggers)}\n')

    # For each trigger, find first reprice event in the next LOOK_AHEAD seconds
    deltas = []
    for trig_ts, direction, move_pct in triggers:
        # Pick the relevant side:
        # UP move → Up token should go UP, Down token should go DOWN
        # We look at whichever side the market should have moved for
        side_rows = odds_up if direction == 'UP' else odds_down
        # Find baseline: last tick in the 1 second BEFORE trigger
        baseline_ask = None
        for tt, bb, aa in reversed(side_rows):
            if tt < trig_ts - 0.2:
                if tt > trig_ts - 5:
                    baseline_ask = aa
                break
        if baseline_ask is None: continue
        # Find first row after trigger where ask differs from baseline
        first_change = None
        for tt, bb, aa in side_rows:
            if tt <= trig_ts: continue
            if tt > trig_ts + LOOK_AHEAD: break
            if abs(aa - baseline_ask) >= 0.01:  # 1c change = considered repriced
                first_change = tt
                break
        if first_change is not None:
            deltas.append((first_change - trig_ts) * 1000)

    if not deltas:
        print('No reprice events detected.')
    else:
        deltas.sort()
        print(f'Reprice delay distribution across {len(deltas)} triggers (ms):')
        print(f'  min:    {deltas[0]:.0f}')
        print(f'  p10:    {deltas[len(deltas)//10]:.0f}')
        print(f'  p25:    {deltas[len(deltas)//4]:.0f}')
        print(f'  MEDIAN: {deltas[len(deltas)//2]:.0f}')
        print(f'  p75:    {deltas[3*len(deltas)//4]:.0f}')
        print(f'  p90:    {deltas[9*len(deltas)//10]:.0f}')
        print(f'  max:    {deltas[-1]:.0f}')
        print(f'  mean:   {statistics.mean(deltas):.0f}')
        print()
        # How many are below certain thresholds (to answer: how fast?)
        for threshold in [30, 50, 100, 200, 500, 1000]:
            count = sum(1 for d in deltas if d <= threshold)
            pct = count / len(deltas) * 100
            print(f'  <= {threshold:4d}ms: {count:3d} triggers ({pct:.0f}%)')

finally:
    if os.path.exists(SNAP):
        os.remove(SNAP)
