"""
Dual Entry Backtest

Key insight from BoshBashBish:
  If avg_up_entry + avg_dn_entry < $1.00, you profit regardless of outcome.

Strategy: Within each candle, opportunistically buy both sides at different times
when prices are favorable. Check if combined avg < $1 is achievable.

Tests:
1. How often can you get combined avg < $1 within a single candle?
2. What is the achievable combined avg across different entry strategies?
3. Best approach: buy cheap side first, wait for other side to also get cheap.

BTC_5m, 100% candles, real fees.
"""

import sqlite3
from collections import defaultdict

DB       = r'C:\Users\James\polybotanalysis\market_btc_5m.db'
INTERVAL = 300
SHARES   = 100

def calc_fee(shares, price):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

print("Loading BTC_5m...")
conn = sqlite3.connect(DB)
rows = conn.execute(
    'SELECT unix_time, market_id, outcome, bid, ask, mid FROM polymarket_odds '
    'WHERE outcome IN ("Up","Down") AND ask > 0 AND mid > 0 ORDER BY unix_time ASC'
).fetchall()
conn.close()

candles = defaultdict(lambda: {'Up': [], 'Down': []})
for ts, mid_id, out, bid, ask, mid in rows:
    cs = (int(float(ts)) // INTERVAL) * INTERVAL
    bid_val = float(bid) if bid else max(0.0, 2*float(mid) - float(ask))
    candles[(cs, mid_id)][out].append((float(ts), float(ask), bid_val, float(mid)))

print(f"Loaded {len(candles)} candles\n")

# ─────────────────────────────────────────────────────────────
# ANALYSIS: What is the min ask seen per side within each candle?
# ─────────────────────────────────────────────────────────────
combined_mins = []  # min_up_ask + min_dn_ask per candle
sequential_combos = []  # best achievable if buying at different times

for (cs, mid_id), sides in candles.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][3]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    min_up_ask = min(t[0] for t in up_ticks)
    min_dn_ask = min(t[0] for t in dn_ticks)

    combined_mins.append({
        'min_up': min_up_ask,
        'min_dn': min_dn_ask,
        'combined': min_up_ask + min_dn_ask,
        'winner': winner,
    })

print("=" * 70)
print("  CANDLE ANALYSIS: Min ask per side within each candle")
print("=" * 70)

combos = [r['combined'] for r in combined_mins]
print(f"  Total candles analysed: {len(combos)}")
print(f"  Min combined ask ever:  {min(combos):.4f}")
print(f"  Avg combined ask:       {sum(combos)/len(combos):.4f}")
print(f"  Candles where min_up + min_dn < 1.00: {sum(1 for c in combos if c < 1.00)} ({100*sum(1 for c in combos if c < 1.00)/len(combos):.1f}%)")
print(f"  Candles where min_up + min_dn < 0.98: {sum(1 for c in combos if c < 0.98)} ({100*sum(1 for c in combos if c < 0.98)/len(combos):.1f}%)")
print(f"  Candles where min_up + min_dn < 0.95: {sum(1 for c in combos if c < 0.95)} ({100*sum(1 for c in combos if c < 0.95)/len(combos):.1f}%)")
print(f"  Candles where min_up + min_dn < 0.90: {sum(1 for c in combos if c < 0.90)} ({100*sum(1 for c in combos if c < 0.90)/len(combos):.1f}%)")

# ─────────────────────────────────────────────────────────────
# STRATEGY: Buy first cheap side, then wait for other side to dip
# Thresholds: first entry at X, second entry at Y (sequential)
# ─────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  STRATEGY: Buy first side at threshold, wait for other side to dip")
print(f"  Then check: combined avg < $1.00?")
print(f"{'='*70}\n")

ENTRY_THRESH = [0.45, 0.40, 0.35, 0.30, 0.25]

results_seq = {t: [] for t in ENTRY_THRESH}

for (cs, mid_id), sides in candles.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][3]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    all_ticks = sorted(
        [(ts, 'Up',   ask, bid, mid) for ts, ask, bid, mid in up_ticks] +
        [(ts, 'Down', ask, bid, mid) for ts, ask, bid, mid in dn_ticks]
    )

    for thresh in ENTRY_THRESH:
        # State
        first_side     = None
        first_ask      = None
        second_side    = None
        second_ask     = None
        last_up_ask    = None
        last_dn_ask    = None

        for ts, side, ask, bid, mid in all_ticks:
            if side == 'Up':  last_up_ask = ask
            else:             last_dn_ask = ask

            if not last_up_ask or not last_dn_ask:
                continue

            # First entry: whichever side dips to thresh first
            if first_side is None and mid <= thresh:
                first_side = side
                first_ask  = ask
                second_side = 'Down' if side == 'Up' else 'Up'

            # Second entry: wait for other side to also dip to thresh
            if first_side is not None and second_side is not None and second_ask is None:
                other_ask = last_dn_ask if second_side == 'Down' else last_up_ask
                other_mid = mid if side == second_side else (
                    up_ticks[-1][3] if second_side == 'Up' else dn_ticks[-1][3]
                )
                # Check current mid of second side
                if side == second_side and mid <= thresh:
                    second_ask = ask

        if first_side is None:
            continue  # no entry triggered

        first_fee  = calc_fee(SHARES, first_ask)
        first_cost = first_ask * SHARES

        if second_ask is not None:
            # Both sides entered
            second_fee  = calc_fee(SHARES, second_ask)
            second_cost = second_ask * SHARES
            combined_avg = first_ask + second_ask
            total_cost   = first_cost + second_cost + first_fee + second_fee

            # Payout: winner pays $1, loser pays $0
            if first_side == winner:
                pnl = SHARES * 1.0 - first_cost - first_fee - second_cost - second_fee
            else:
                pnl = SHARES * 1.0 - second_cost - second_fee - first_cost - first_fee

            results_seq[thresh].append({
                'pnl':          pnl,
                'cost':         total_cost,
                'win':          pnl > 0,
                'combined_avg': combined_avg,
                'both_entered': True,
            })
        else:
            # Only first side entered — hold to resolution
            if first_side == winner:
                pnl = SHARES * 1.0 - first_cost - first_fee
            else:
                pnl = -first_cost - first_fee

            results_seq[thresh].append({
                'pnl':          pnl,
                'cost':         first_cost,
                'win':          pnl > 0,
                'combined_avg': None,
                'both_entered': False,
            })

def summarize(lst):
    if not lst: return None
    n    = len(lst)
    wins = sum(1 for r in lst if r['win'])
    net  = sum(r['pnl'] for r in lst)
    cost = sum(r['cost'] for r in lst)
    return dict(n=n, wr=100*wins/n, net=net, roi=100*net/cost if cost else 0, ppc=net/n)

for thresh in ENTRY_THRESH:
    res  = results_seq[thresh]
    both = [r for r in res if r['both_entered']]
    one  = [r for r in res if not r['both_entered']]

    sa   = summarize(res)
    sb   = summarize(both)
    so   = summarize(one)

    avg_combined = sum(r['combined_avg'] for r in both) / len(both) if both else 0
    below_1      = sum(1 for r in both if r['combined_avg'] < 1.00)
    below_98     = sum(1 for r in both if r['combined_avg'] < 0.98)

    print(f"  Entry threshold: {thresh}")
    if sa: print(f"    ALL:           n={sa['n']:>4}  WR={sa['wr']:>5.1f}%  ROI={sa['roi']:>+6.2f}%  $/c={sa['ppc']:>+8.2f}")
    if sb:
        print(f"    Both entered:  n={sb['n']:>4}  WR={sb['wr']:>5.1f}%  ROI={sb['roi']:>+6.2f}%  $/c={sb['ppc']:>+8.2f}  avg_combined={avg_combined:.3f}  <$1={below_1}  <$0.98={below_98}")
    if so: print(f"    One side only: n={so['n']:>4}  WR={so['wr']:>5.1f}%  ROI={so['roi']:>+6.2f}%  $/c={so['ppc']:>+8.2f}")
    print()

# ─────────────────────────────────────────────────────────────
# STRATEGY 2: DCA both sides — buy multiple lots as price falls
# Average down on both sides simultaneously
# ─────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  STRATEGY: DCA both sides — buy each level on both sides simultaneously")
print(f"  Levels: 0.45, 0.40, 0.35, 0.30, 0.25 — buy both Up AND Down at each")
print(f"{'='*70}\n")

LEVELS = [0.45, 0.40, 0.35, 0.30, 0.25]
dca_results = []

for (cs, mid_id), sides in candles.items():
    up_ticks = sides['Up']
    dn_ticks = sides['Down']
    if not up_ticks or not dn_ticks:
        continue

    final_mid = up_ticks[-1][3]
    winner    = 'Up' if final_mid >= 0.5 else 'Down'

    all_ticks = sorted(
        [(ts, 'Up',   ask, bid, mid) for ts, ask, bid, mid in up_ticks] +
        [(ts, 'Down', ask, bid, mid) for ts, ask, bid, mid in dn_ticks]
    )

    up_entries  = []
    dn_entries  = []
    triggered   = set()
    last_up_ask = None
    last_dn_ask = None

    for ts, side, ask, bid, mid in all_ticks:
        if side == 'Up':  last_up_ask = ask
        else:             last_dn_ask = ask

        if not last_up_ask or not last_dn_ask:
            continue

        # When EITHER side hits a level, buy BOTH sides at current ask
        for lvl in LEVELS:
            if lvl not in triggered and mid <= lvl:
                triggered.add(lvl)
                up_entries.append(last_up_ask)
                dn_entries.append(last_dn_ask)

    if not up_entries:
        continue

    n_lots    = len(up_entries)
    up_avg    = sum(up_entries) / n_lots
    dn_avg    = sum(dn_entries) / n_lots
    combined  = up_avg + dn_avg

    up_cost   = sum(up_entries) * SHARES
    dn_cost   = sum(dn_entries) * SHARES
    up_fee    = sum(calc_fee(SHARES, p) for p in up_entries)
    dn_fee    = sum(calc_fee(SHARES, p) for p in dn_entries)
    total_cost = up_cost + dn_cost + up_fee + dn_fee

    if winner == 'Up':
        pnl = n_lots * SHARES * 1.0 - up_cost - up_fee - dn_cost - dn_fee
    else:
        pnl = n_lots * SHARES * 1.0 - dn_cost - dn_fee - up_cost - up_fee

    dca_results.append({
        'pnl':       pnl,
        'cost':      up_cost + dn_cost,
        'win':       pnl > 0,
        'combined':  combined,
        'n_lots':    n_lots,
        'up_avg':    up_avg,
        'dn_avg':    dn_avg,
    })

sd = summarize(dca_results)
avg_comb  = sum(r['combined'] for r in dca_results) / len(dca_results)
below_1   = sum(1 for r in dca_results if r['combined'] < 1.00)
below_98  = sum(1 for r in dca_results if r['combined'] < 0.98)
avg_lots  = sum(r['n_lots'] for r in dca_results) / len(dca_results)

if sd:
    print(f"  DCA both sides at each level:")
    print(f"    n={sd['n']}  WR={sd['wr']:.1f}%  ROI={sd['roi']:+.2f}%  $/c={sd['ppc']:+.2f}")
    print(f"    Avg combined entry: {avg_comb:.4f}")
    print(f"    Candles combined < $1.00: {below_1} ({100*below_1/len(dca_results):.1f}%)")
    print(f"    Candles combined < $0.98: {below_98} ({100*below_98/len(dca_results):.1f}%)")
    print(f"    Avg lots per candle: {avg_lots:.1f}")
    print()

    # Break down by combined avg
    print(f"  By combined average entry:")
    buckets = [
        (0.00, 0.90, '< 0.90 (strong arb)'),
        (0.90, 0.95, '0.90-0.95'),
        (0.95, 1.00, '0.95-1.00'),
        (1.00, 1.05, '1.00-1.05'),
        (1.05, 1.20, '> 1.05'),
    ]
    for lo, hi, label in buckets:
        grp = [r for r in dca_results if lo <= r['combined'] < hi]
        s   = summarize(grp)
        if s:
            print(f"    combined {label:<25} n={s['n']:>4}  WR={s['wr']:>5.1f}%  ROI={s['roi']:>+7.2f}%  $/c={s['ppc']:>+8.2f}")
