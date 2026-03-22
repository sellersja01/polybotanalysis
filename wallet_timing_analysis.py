"""
Deep wallet timing analysis.
For each candle the wallets traded, look at:
1. When within the candle they buy each side (early/mid/late)
2. What price each side was at when they bought
3. Whether they buy both sides or just one
4. The sequence of buys (Up first or Down first)
5. Cross-reference with the odds data to see what price was doing when they entered
"""

import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

# Load wallet data
df = pd.read_csv(r'C:\Users\James\polybotanalysis\all_wallets_weekday.csv')
df['timestamp'] = df['timestamp'].astype(float)
df['time_utc']  = pd.to_datetime(df['time_utc'])

# Only BUY orders
df = df[df['side'] == 'BUY'].copy()

# Parse candle start from market name
# "Bitcoin Up or Down - March 19, 5:55PM-6:00PM ET"
# We'll infer candle_start from timestamp and market timeframe
def get_interval(market):
    # check if it's a 5m or 15m candle by looking at the time range
    import re
    m = re.search(r'(\d+):(\d+)(AM|PM)-(\d+):(\d+)(AM|PM)', market)
    if not m:
        return 300
    h1,mn1,ap1,h2,mn2,ap2 = m.groups()
    h1,mn1,h2,mn2 = int(h1),int(mn1),int(h2),int(mn2)
    if ap1 == 'PM' and h1 != 12: h1 += 12
    if ap2 == 'PM' and h2 != 12: h2 += 12
    diff = (h2*60+mn2) - (h1*60+mn1)
    if diff < 0: diff += 24*60
    return diff * 60

df['interval'] = df['market'].apply(get_interval)
df['candle_start'] = df.apply(lambda r: (int(r['timestamp']) // r['interval']) * r['interval'], axis=1)
df['candle_key']   = df['market'] + '|' + df['wallet']

# Load odds data for cross-reference
DBS = {
    'btc_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'btc_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
}

odds = {}
for key, path in DBS.items():
    conn = sqlite3.connect(path)
    o = pd.read_sql_query("""
        SELECT unix_time, market_id, outcome, ask, mid FROM polymarket_odds
        WHERE outcome IN ('Up','Down') AND ask > 0 AND mid > 0
        ORDER BY unix_time
    """, conn)
    conn.close()
    o['interval']     = 300 if '5m' in key else 900
    o['candle_start'] = (o['unix_time'] // o['interval']) * o['interval']
    odds[key] = o

all_odds = pd.concat(odds.values(), ignore_index=True)

def get_odds_at(candle_start, market_ts, outcome, interval):
    sub = all_odds[
        (all_odds['candle_start'] == candle_start) &
        (all_odds['outcome'] == outcome) &
        (all_odds['unix_time'] <= market_ts) &
        (all_odds['unix_time'] >= candle_start)
    ]
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    return float(row['ask']), float(row['mid'])

# ── Per candle analysis ────────────────────────────────────────────────────────
print("=" * 70)
print("  WALLET TIMING ANALYSIS")
print("=" * 70)

# Group by wallet + market (candle)
groups = df.groupby(['wallet', 'market', 'candle_start', 'interval'])

candle_stats = []
for (wallet, market, cs, interval), grp in groups:
    grp = grp.sort_values('timestamp')

    up_buys = grp[grp['outcome'] == 'Up']
    dn_buys = grp[grp['outcome'] == 'Down']

    if len(up_buys) == 0 and len(dn_buys) == 0:
        continue

    candle_end   = cs + interval
    candle_dur   = interval

    # Timing: where in the candle did they buy? (0=open, 1=close)
    up_positions = [(t - cs) / candle_dur for t in up_buys['timestamp'] if cs <= t <= candle_end]
    dn_positions = [(t - cs) / candle_dur for t in dn_buys['timestamp'] if cs <= t <= candle_end]

    # First buy timing and side
    all_buys = grp.sort_values('timestamp')
    first_buy = all_buys.iloc[0] if len(all_buys) > 0 else None
    first_side = first_buy['outcome'] if first_buy is not None else None
    first_pos  = (first_buy['timestamp'] - cs) / candle_dur if first_buy is not None else None
    first_price = first_buy['price'] if first_buy is not None else None

    # Second side timing (when did they start buying the other side?)
    if len(up_buys) > 0 and len(dn_buys) > 0:
        both_sides = True
        first_up_t = up_buys['timestamp'].min()
        first_dn_t = dn_buys['timestamp'].min()
        second_side_delay = abs(first_up_t - first_dn_t)  # seconds between first up and first down buy
        first_was = 'Up' if first_up_t < first_dn_t else 'Down'
    else:
        both_sides = False
        second_side_delay = None
        first_was = None

    # Average prices
    avg_up = up_buys['price'].mean() if len(up_buys) > 0 else None
    avg_dn = dn_buys['price'].mean() if len(dn_buys) > 0 else None

    candle_stats.append({
        'wallet':              wallet,
        'market':              market,
        'candle_start':        cs,
        'interval':            interval,
        'n_up':                len(up_buys),
        'n_dn':                len(dn_buys),
        'both_sides':          both_sides,
        'avg_up':              avg_up,
        'avg_dn':              avg_dn,
        'combined_avg':        (avg_up + avg_dn) if (avg_up and avg_dn) else None,
        'first_side':          first_side,
        'first_pos':           first_pos,
        'first_price':         first_price,
        'first_was':           first_was,
        'second_side_delay_s': second_side_delay,
        'up_first_pos':        np.mean(up_positions) if up_positions else None,
        'dn_first_pos':        np.mean(dn_positions) if dn_positions else None,
        'up_entry_pos':        min(up_positions) if up_positions else None,
        'dn_entry_pos':        min(dn_positions) if dn_positions else None,
    })

stats = pd.DataFrame(candle_stats)
both = stats[stats['both_sides'] == True]
one  = stats[stats['both_sides'] == False]

print(f"\n  Total candle-wallet pairs: {len(stats)}")
print(f"  Both sides: {len(both)} ({100*len(both)/len(stats):.1f}%)")
print(f"  One side only: {len(one)} ({100*len(one)/len(stats):.1f}%)")

# ── When do they enter? ────────────────────────────────────────────────────────
print(f"\n  === Entry timing (position in candle: 0=open, 1=close) ===")
print(f"  {'':20} {'Mean':>8} {'Median':>8} {'P25':>8} {'P75':>8}")
print(f"  {'-'*52}")
print(f"  {'First buy (any)':20} {stats['first_pos'].mean():>8.3f} {stats['first_pos'].median():>8.3f} "
      f"{stats['first_pos'].quantile(0.25):>8.3f} {stats['first_pos'].quantile(0.75):>8.3f}")
if len(both):
    print(f"  {'Up entry (both)':20} {both['up_entry_pos'].mean():>8.3f} {both['up_entry_pos'].median():>8.3f} "
          f"{both['up_entry_pos'].quantile(0.25):>8.3f} {both['up_entry_pos'].quantile(0.75):>8.3f}")
    print(f"  {'Down entry (both)':20} {both['dn_entry_pos'].mean():>8.3f} {both['dn_entry_pos'].median():>8.3f} "
          f"{both['dn_entry_pos'].quantile(0.25):>8.3f} {both['dn_entry_pos'].quantile(0.75):>8.3f}")

# ── Second side delay ─────────────────────────────────────────────────────────
print(f"\n  === Delay between first and second side entry (both-sides candles) ===")
d = both['second_side_delay_s'].dropna()
print(f"  Mean={d.mean():.1f}s  Median={d.median():.1f}s  "
      f"P25={d.quantile(0.25):.1f}s  P75={d.quantile(0.75):.1f}s  Max={d.max():.1f}s")
print(f"\n  Distribution:")
buckets = [(0,5),(5,15),(15,30),(30,60),(60,120),(120,300),(300,9999)]
for lo, hi in buckets:
    n = ((d >= lo) & (d < hi)).sum()
    print(f"    {lo:4d}-{hi:4d}s: {n:4d} ({100*n/len(d):.1f}%)")

# ── Which side first? ─────────────────────────────────────────────────────────
print(f"\n  === Which side do they buy first? (both-sides candles) ===")
fc = both['first_was'].value_counts()
print(f"  Up first:   {fc.get('Up',0)} ({100*fc.get('Up',0)/len(both):.1f}%)")
print(f"  Down first: {fc.get('Down',0)} ({100*fc.get('Down',0)/len(both):.1f}%)")

# ── First buy price ───────────────────────────────────────────────────────────
print(f"\n  === First buy price (what price do they pay for the first side?) ===")
print(f"  Mean=${stats['first_price'].mean():.3f}  Median=${stats['first_price'].median():.3f}  "
      f"P25=${stats['first_price'].quantile(0.25):.3f}  P75=${stats['first_price'].quantile(0.75):.3f}")

# ── Per wallet breakdown ───────────────────────────────────────────────────────
print(f"\n  === Per wallet breakdown ===")
print(f"  {'Wallet':>10} {'Candles':>8} {'Both%':>7} {'AvgUp':>7} {'AvgDn':>7} "
      f"{'Comb':>7} {'1stPos':>7} {'Delay_s':>9} {'UpFirst%':>9}")
print(f"  {'-'*72}")
for wallet, wgrp in stats.groupby('wallet'):
    wb    = wgrp[wgrp['both_sides']]
    both_pct = 100*len(wb)/len(wgrp) if len(wgrp) else 0
    avg_up   = wgrp['avg_up'].mean()
    avg_dn   = wgrp['avg_dn'].mean()
    comb     = wgrp['combined_avg'].mean()
    first_p  = wgrp['first_pos'].mean()
    delay    = wb['second_side_delay_s'].mean() if len(wb) else 0
    up_first = 100*(wb['first_was']=='Up').mean() if len(wb) else 0
    print(f"  {wallet:>10} {len(wgrp):>8} {both_pct:>6.1f}% {avg_up:>7.3f} {avg_dn:>7.3f} "
          f"{comb:>7.3f} {first_p:>7.3f} {delay:>9.1f} {up_first:>8.1f}%")

# ── Key insight: combined avg when both sides ─────────────────────────────────
print(f"\n  === Combined avg price (Up+Down) — only both-sided candles ===")
print(f"  Mean={both['combined_avg'].mean():.4f}  Median={both['combined_avg'].median():.4f}")
print(f"  % candles where combined < 1.0: {100*(both['combined_avg']<1.0).mean():.1f}%")
print(f"  % candles where combined < 0.9: {100*(both['combined_avg']<0.9).mean():.1f}%")
print(f"  % candles where combined < 0.8: {100*(both['combined_avg']<0.8).mean():.1f}%")

# ── What is the market doing when they enter? ─────────────────────────────────
print(f"\n  === Price at first entry vs candle open price ===")
print(f"  (Do they buy when price has already moved, or at the open?)")
# Look at first_pos distribution more carefully
early = stats[stats['first_pos'] < 0.1]
mid   = stats[(stats['first_pos'] >= 0.1) & (stats['first_pos'] < 0.5)]
late  = stats[stats['first_pos'] >= 0.5]
print(f"  First buy in first 10% of candle: {len(early)} ({100*len(early)/len(stats):.1f}%)")
print(f"  First buy in middle 10-50%:       {len(mid)}  ({100*len(mid)/len(stats):.1f}%)")
print(f"  First buy in last 50%:            {len(late)} ({100*len(late)/len(stats):.1f}%)")
print(f"\n  Early buyers avg first price: {early['first_price'].mean():.3f}")
print(f"  Mid buyers avg first price:   {mid['first_price'].mean():.3f}")
print(f"  Late buyers avg first price:  {late['first_price'].mean():.3f}")
