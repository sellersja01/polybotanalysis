"""
Analyzes how often both Up and Down sides visit a "cheap" price threshold
within the same candle — testing if the wallet strategy is replicable.

Key addition: CONDITIONAL probability — given side A goes cheap, what %
of the time does side B ALSO go cheap LATER in the same candle?
This is the number that matters for a live bot.
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}

CANDLE_INTERVALS = {'5m': 300, '15m': 900}
THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30]


def analyze(db_path, label):
    tf = '15m' if '15m' in label else '5m'
    interval = CANDLE_INTERVALS[tf]

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute('''
            SELECT unix_time, market_id, outcome, ask, mid
            FROM polymarket_odds
            WHERE outcome IN ('Up','Down') AND mid IS NOT NULL AND mid > 0
            ORDER BY unix_time ASC
        ''').fetchall()
        conn.close()
    except Exception as e:
        print(f"  {label}: SKIP — {e}")
        return

    # Group ticks by candle, preserving time order
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, market_id, outcome, ask, mid in rows:
        candle_start = (int(float(ts)) // interval) * interval
        key = (candle_start, market_id)
        candles[key][outcome].append({
            'ts': float(ts),
            'mid': float(mid),
            'ask': float(ask) if ask else float(mid),
        })

    resolved_candles = 0

    stats = {t: {
        # Baseline: both sides hit threshold (anywhere in candle)
        'both': 0, 'up_only': 0, 'dn_only': 0, 'neither': 0,

        # Conditional: given Up hit threshold first, did Down also hit later?
        'up_first_dn_followed': 0,
        'up_first_dn_never':    0,

        # Conditional: given Down hit threshold first, did Up also hit later?
        'dn_first_up_followed': 0,
        'dn_first_up_never':    0,

        # Of candles where both went cheap: how long after the first dip
        # did the second dip occur? (seconds remaining in candle)
        'time_gaps': [],

        # Combined ask when buying at cheapest point on each side
        'combined_mins': [],
    } for t in THRESHOLDS}

    for (candle_start, market_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        final_up = up_ticks[-1]['mid']
        if final_up >= 0.85:
            resolved = 'Up'
        elif final_up <= 0.15:
            resolved = 'Down'
        else:
            continue

        resolved_candles += 1
        candle_end = candle_start + interval

        up_mids = [(t['ts'], t['mid'], t['ask']) for t in up_ticks]
        dn_mids = [(t['ts'], t['mid'], t['ask']) for t in dn_ticks]

        for thresh in THRESHOLDS:
            s = stats[thresh]

            # Find first time each side hits the threshold
            up_hit_ts = next((ts for ts, mid, ask in up_mids if mid <= thresh), None)
            dn_hit_ts = next((ts for ts, mid, ask in dn_mids if mid <= thresh), None)

            up_hit = up_hit_ts is not None
            dn_hit = dn_hit_ts is not None

            if up_hit and dn_hit:
                s['both'] += 1

                first_hit_ts = min(up_hit_ts, dn_hit_ts)
                time_remaining = candle_end - first_hit_ts
                s['time_gaps'].append(max(time_remaining, 0))

                # Best ask when each side was cheap
                cheap_up_asks = [ask for ts, mid, ask in up_mids if mid <= thresh]
                cheap_dn_asks = [ask for ts, mid, ask in dn_mids if mid <= thresh]
                combined = min(cheap_up_asks) + min(cheap_dn_asks)
                s['combined_mins'].append(combined)

            elif up_hit:
                s['up_only'] += 1
                # Did Down ever get cheap AFTER Up went cheap?
                dn_after = any(mid <= thresh for ts, mid, ask in dn_mids if ts > up_hit_ts)
                if dn_after:
                    s['up_first_dn_followed'] += 1  # shouldn't happen since dn_hit is False
                else:
                    s['up_first_dn_never'] += 1

            elif dn_hit:
                s['dn_only'] += 1
                # Did Up ever get cheap AFTER Down went cheap?
                up_after = any(mid <= thresh for ts, mid, ask in up_mids if ts > dn_hit_ts)
                if up_after:
                    s['dn_first_up_followed'] += 1
                else:
                    s['dn_first_up_never'] += 1
            else:
                s['neither'] += 1

    print(f"\n{'='*80}")
    print(f"  {label}  |  {resolved_candles} resolved candles")
    print(f"{'='*80}")

    print(f"\n  --- BASELINE: How often do both sides go cheap? ---")
    print(f"  {'Thresh':>7} {'Both%':>7} {'UpOnly%':>9} {'DnOnly%':>9} {'Neither%':>9} {'AvgComb':>9} {'AvgPnL':>8}")
    print(f"  {'-'*68}")
    for thresh in THRESHOLDS:
        s = stats[thresh]
        total = s['both'] + s['up_only'] + s['dn_only'] + s['neither']
        if not total: continue
        both_pct = 100 * s['both'] / total
        up_pct   = 100 * s['up_only'] / total
        dn_pct   = 100 * s['dn_only'] / total
        nei_pct  = 100 * s['neither'] / total
        if s['combined_mins']:
            avg_comb = sum(s['combined_mins']) / len(s['combined_mins'])
            avg_pnl  = 1.0 - avg_comb
        else:
            avg_comb = avg_pnl = 0
        print(f"  {thresh:>7.2f} {both_pct:>7.1f}% {up_pct:>8.1f}% {dn_pct:>8.1f}% {nei_pct:>8.1f}% {avg_comb:>9.3f} {avg_pnl:>+8.3f}")

    print(f"\n  --- CONDITIONAL: Given one side goes cheap, does the OTHER side go cheap later? ---")
    print(f"  {'Thresh':>7} {'GivenUp->DnFollows%':>22} {'GivenDn->UpFollows%':>22} {'AvgTimeLeft(s)':>16}")
    print(f"  {'-'*72}")
    for thresh in THRESHOLDS:
        s = stats[thresh]
        # "up only" candles: up went cheap but dn never did
        # "dn only" candles: dn went cheap but up never did
        # "both" candles: both went cheap — in these, one necessarily followed the other

        # Of candles where Up went cheap AND Down went cheap:
        # what fraction had Up going cheap first vs Down going cheap first?
        both = s['both']
        up_first_both = sum(
            1 for _ in range(both)  # placeholder — need to recompute
        )

        # Simpler: conditional = both / (both + one_side_only)
        # "Given Up went cheap, did Down also go cheap?" = both / (both + up_only)
        up_given_up = 100 * s['both'] / max(s['both'] + s['up_only'], 1)
        dn_given_dn = 100 * s['both'] / max(s['both'] + s['dn_only'], 1)

        avg_time = sum(s['time_gaps']) / len(s['time_gaps']) if s['time_gaps'] else 0
        print(f"  {thresh:>7.2f} {up_given_up:>21.1f}% {dn_given_dn:>21.1f}% {avg_time:>15.1f}s")

    print(f"\n  --- TIMING: When the first dip occurs, how much candle time is left? ---")
    print(f"  {'Thresh':>7} {'AvgTimeLeft':>13} {'<60s%':>8} {'<120s%':>9} {'>120s%':>9}")
    print(f"  {'-'*50}")
    for thresh in THRESHOLDS:
        s = stats[thresh]
        gaps = s['time_gaps']
        if not gaps: continue
        avg_t = sum(gaps) / len(gaps)
        lt60  = 100 * sum(1 for g in gaps if g < 60)  / len(gaps)
        lt120 = 100 * sum(1 for g in gaps if g < 120) / len(gaps)
        gt120 = 100 * sum(1 for g in gaps if g >= 120) / len(gaps)
        print(f"  {thresh:>7.2f} {avg_t:>12.1f}s {lt60:>7.1f}% {lt120:>8.1f}% {gt120:>8.1f}%")


print("CHEAP THRESHOLD ANALYSIS — WITH CONDITIONAL PROBABILITIES")
print("="*80)

for label, db_path in DBS.items():
    analyze(db_path, label)

print("\nDone.")
