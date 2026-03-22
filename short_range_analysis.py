"""
Short-Term Oscillation Analysis — fast version
Find the typical short-window swings within candles.
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

INTERVALS = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900, 'ETH_15m': 900}
WINDOWS   = [5, 10, 15, 30, 60]  # seconds


def load_up(name, db_path, interval):
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("""
        SELECT unix_time, mid FROM polymarket_odds
        WHERE outcome='Up' AND mid > 0.05 AND mid < 0.95
        ORDER BY unix_time
    """, conn)
    conn.close()
    df['candle_start'] = (df['unix_time'] // interval) * interval
    df['candle_pos']   = (df['unix_time'] - df['candle_start']) / interval
    # exclude first and last 10% of candle
    df = df[(df['candle_pos'] >= 0.1) & (df['candle_pos'] <= 0.9)].reset_index(drop=True)
    return df


def window_ranges_fast(df, window_sec):
    """
    Sample every 5th tick for speed.
    For each sampled tick, grab all ticks within ±window_sec and compute range.
    """
    times = df['unix_time'].values
    mids  = df['mid'].values
    step  = max(1, len(times) // 5000)  # sample ~5000 points
    results = []
    for i in range(0, len(times), step):
        t  = times[i]
        lo = np.searchsorted(times, t - window_sec)
        hi = np.searchsorted(times, t + window_sec)
        w  = mids[lo:hi]
        if len(w) >= 3:
            results.append(w.max() - w.min())
    return np.array(results)


def zigzag(mids, min_move=0.01):
    """Find zigzag swing sizes."""
    swings = []
    direction = None
    extreme = mids[0]
    for m in mids[1:]:
        if direction is None:
            if   m > extreme + min_move: direction = 'up';   extreme = m
            elif m < extreme - min_move: direction = 'down'; extreme = m
        elif direction == 'up':
            if   m > extreme:            extreme = m
            elif m < extreme - min_move: swings.append(extreme - m); direction = 'down'; extreme = m
        else:
            if   m < extreme:            extreme = m
            elif m > extreme + min_move: swings.append(m - extreme); direction = 'up';   extreme = m
    return np.array(swings) if swings else np.array([])


def analyze(name, df):
    print(f"\n{'='*65}")
    print(f"  {name}  ({len(df):,} ticks, {df['candle_start'].nunique()} candles)")
    print(f"{'='*65}")

    mids  = df['mid'].values
    times = df['unix_time'].values

    # ── Rolling window ranges ─────────────────────────────────────────────────
    print(f"\n  Rolling window range (max-min within ±N seconds around each tick):")
    print(f"  {'Window':>8} {'AvgRange':>10} {'Median':>10} {'P25':>8} {'P75':>8} {'P90':>8}")
    print(f"  {'-'*58}")
    for w in WINDOWS:
        r = window_ranges_fast(df, w)
        if len(r):
            print(f"  {f'±{w}s':>8} {r.mean():>10.4f} {np.median(r):>10.4f} "
                  f"{np.percentile(r,25):>8.4f} {np.percentile(r,75):>8.4f} "
                  f"{np.percentile(r,90):>8.4f}")

    # ── Tick-to-tick ──────────────────────────────────────────────────────────
    diffs = np.abs(np.diff(mids))
    diffs = diffs[diffs > 0]
    print(f"\n  Tick-to-tick changes:  mean={diffs.mean():.5f}  "
          f"med={np.median(diffs):.5f}  p90={np.percentile(diffs,90):.5f}  "
          f"p99={np.percentile(diffs,99):.5f}")

    # ── Zigzag swings ─────────────────────────────────────────────────────────
    print(f"\n  Zigzag swing sizes (reversal must be >= min_move):")
    print(f"  {'min_move':>10} {'#swings':>8} {'avg':>8} {'med':>8} {'p75':>8} {'p90':>8}")
    print(f"  {'-'*50}")
    for mm in [0.005, 0.01, 0.02, 0.03, 0.05, 0.10]:
        s = zigzag(mids, min_move=mm)
        if len(s) > 10:
            print(f"  {mm:>10.3f} {len(s):>8} {s.mean():>8.4f} "
                  f"{np.median(s):>8.4f} {np.percentile(s,75):>8.4f} "
                  f"{np.percentile(s,90):>8.4f}")

    # ── By price bucket ───────────────────────────────────────────────────────
    print(f"\n  ±30s range by price bucket (where is the price when the swing happens):")
    print(f"  {'Bucket':>8} {'Ticks':>8} {'AvgRange':>10} {'Med':>8}")
    print(f"  {'-'*38}")
    df2 = df.copy()
    df2['bucket'] = (df2['mid'] * 10).round() / 10
    for b in [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        sub = df2[df2['bucket'] == b]
        if len(sub) < 100:
            continue
        r = window_ranges_fast(sub, 30)
        if len(r):
            print(f"  {'~'+str(b):>8} {len(sub):>8} {r.mean():>10.4f} {np.median(r):>8.4f}")


for name, path in DB_PATHS.items():
    try:
        df = load_up(name, path, INTERVALS[name])
        analyze(name, df)
    except Exception as e:
        print(f"\n[{name}] Error: {e}")
