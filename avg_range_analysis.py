"""
Average Range Analysis
For each candle, find the min and max Up mid price during that candle.
Group by starting price bucket and find the average oscillation range.
This tells us: "when Up opens at ~0.50, how much does it typically swing?"
"""

import sqlite3
import pandas as pd
import numpy as np

DB_PATHS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}

INTERVALS = {
    'BTC_5m': 300, 'ETH_5m': 300,
    'BTC_15m': 900, 'ETH_15m': 900,
}

def load_market(name, db_path, interval):
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("""
        SELECT unix_time, outcome, mid
        FROM polymarket_odds
        WHERE mid > 0 AND mid < 1
        ORDER BY unix_time
    """, conn)
    conn.close()

    df['candle_start'] = (df['unix_time'] // interval) * interval
    up = df[df['outcome'] == 'Up'][['unix_time', 'candle_start', 'mid']].copy()
    up.columns = ['unix_time', 'candle_start', 'up_mid']
    return up

def analyze(name, df, interval):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    # Per-candle stats
    grp = df.groupby('candle_start')['up_mid'].agg(
        open_price  = 'first',
        close_price = 'last',
        candle_min  = 'min',
        candle_max  = 'max',
        tick_count  = 'count'
    ).reset_index()

    grp['range']       = grp['candle_max'] - grp['candle_min']
    grp['open_bucket'] = (grp['open_price'] * 10).round() / 10  # round to nearest 0.10

    # Filter: only candles with enough data
    grp = grp[grp['tick_count'] >= 10]

    # Determine winner from close price
    grp['winner'] = grp['close_price'].apply(
        lambda x: 'Up' if x >= 0.85 else ('Down' if x <= 0.15 else 'Unknown')
    )

    print(f"\n  Total candles analyzed: {len(grp)}")
    print(f"  Overall avg range: {grp['range'].mean():.4f}")
    print(f"  Overall median range: {grp['range'].median():.4f}")

    print(f"\n  {'OpenBucket':>12} {'Candles':>8} {'AvgRange':>10} {'MedRange':>10} {'P25Range':>10} {'P75Range':>10}")
    print(f"  {'-'*60}")

    for bucket in sorted(grp['open_bucket'].unique()):
        sub = grp[grp['open_bucket'] == bucket]
        if len(sub) < 3:
            continue
        avg = sub['range'].mean()
        med = sub['range'].median()
        p25 = sub['range'].quantile(0.25)
        p75 = sub['range'].quantile(0.75)
        print(f"  {'~'+str(round(bucket,1)):>12} {len(sub):>8} {avg:>10.4f} {med:>10.4f} {p25:>10.4f} {p75:>10.4f}")

    # Also show: how much does Up oscillate mid-candle (not just open-to-close range)
    print(f"\n  === Intra-candle oscillation by open bucket ===")
    print(f"  (range = max - min within candle, regardless of direction)")
    print(f"\n  Key question: if Up opens at X, how far does it typically swing?")

    # Show range as % of open price
    grp['range_pct'] = grp['range'] / grp['open_price']
    print(f"\n  {'OpenBucket':>12} {'AvgRange':>10} {'AvgRange%':>10} {'WinRate_Up':>12}")
    print(f"  {'-'*50}")
    for bucket in sorted(grp['open_bucket'].unique()):
        sub = grp[grp['open_bucket'] == bucket]
        if len(sub) < 3:
            continue
        avg_r  = sub['range'].mean()
        avg_rp = sub['range_pct'].mean() * 100
        wr_up  = (sub['winner'] == 'Up').mean() * 100
        print(f"  {'~'+str(round(bucket,1)):>12} {avg_r:>10.4f} {avg_rp:>9.1f}% {wr_up:>11.1f}%")

    # Show distribution of how often price revisits both sides of center
    print(f"\n  === Both-sides candles (max>=0.55 AND min<=0.45) ===")
    both = grp[(grp['candle_max'] >= 0.55) & (grp['candle_min'] <= 0.45)]
    print(f"  {len(both)}/{len(grp)} candles ({100*len(both)/len(grp):.1f}%) crossed both 0.45 and 0.55")
    if len(both):
        print(f"  Avg range in these candles: {both['range'].mean():.4f}")

    return grp


all_results = {}
for name, path in DB_PATHS.items():
    try:
        df = load_market(name, path, INTERVALS[name])
        result = analyze(name, df, INTERVALS[name])
        all_results[name] = result
    except Exception as e:
        print(f"\n[{name}] Error: {e}")

print(f"\n\n{'='*60}")
print(f"  COMBINED SUMMARY — Average range by open price bucket")
print(f"{'='*60}")
print(f"  {'Market':>10} {'~0.2':>8} {'~0.3':>8} {'~0.4':>8} {'~0.5':>8} {'~0.6':>8} {'~0.7':>8} {'~0.8':>8}")
print(f"  {'-'*66}")
for name, grp in all_results.items():
    row = f"  {name:>10}"
    for b in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        sub = grp[grp['open_bucket'] == b]
        if len(sub) >= 3:
            row += f" {sub['range'].mean():>8.4f}"
        else:
            row += f" {'N/A':>8}"
    print(row)
