"""
Zigzag Limit Order Backtest
Strategy: buy when ask drops DIP_THRESHOLD cents from the last fill price (or candle open).
Tests different dip thresholds and price caps across all 4 markets.
"""

import sqlite3
import pandas as pd
import numpy as np
from itertools import product

DB_PATHS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}
INTERVALS = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900, 'ETH_15m': 900}

SHARES       = 100
MIN_INTERVAL = 5      # min seconds between fills on same side


def load_candles(db_path, interval):
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("""
        SELECT unix_time, outcome, ask, mid
        FROM polymarket_odds
        WHERE ask > 0 AND ask < 1 AND mid > 0 AND mid < 1
        ORDER BY unix_time
    """, conn)
    conn.close()
    df['candle_start'] = (df['unix_time'] // interval) * interval
    return df


def simulate_candle(up_df, dn_df, dip, price_cap):
    """
    Simulate one candle.
    Buy when ask drops DIP cents from last fill (or from candle-open ask).
    Returns (up_fills, dn_fills, winner) where fills = list of ask prices.
    """
    up_fills, dn_fills = [], []

    # Determine winner from final Up mid
    if len(up_df) < 5:
        return [], [], None
    final_mid = up_df['mid'].iloc[-10:].mean()
    if   final_mid >= 0.85: winner = 'Up'
    elif final_mid <= 0.15: winner = 'Down'
    else:                   return [], [], None

    for side, tdf, fills in [('Up', up_df, up_fills), ('Down', dn_df, dn_fills)]:
        if len(tdf) == 0:
            continue
        times  = tdf['unix_time'].values
        asks   = tdf['ask'].values

        # Initial trigger: last fill price starts at candle-open ask
        last_fill_price = asks[0]
        last_fill_time  = times[0] - MIN_INTERVAL  # allow first fill immediately

        for i in range(len(asks)):
            a = asks[i]
            t = times[i]

            if a > price_cap:
                # Price is above cap — reset trigger to current ask
                last_fill_price = a
                continue

            # Buy when ask drops DIP below last fill price
            if a <= last_fill_price - dip and t - last_fill_time >= MIN_INTERVAL:
                fills.append(a)
                last_fill_price = a
                last_fill_time  = t

    return up_fills, dn_fills, winner


def calc_pnl(up_fills, dn_fills, winner):
    if not up_fills and not dn_fills:
        return 0.0, 0.0
    up_sh  = len(up_fills) * SHARES
    dn_sh  = len(dn_fills) * SHARES
    up_c   = sum(up_fills) * SHARES
    dn_c   = sum(dn_fills) * SHARES
    if winner == 'Up':
        pnl = (1.0 * up_sh - up_c) + (0.0 * dn_sh - dn_c)
    else:
        pnl = (0.0 * up_sh - up_c) + (1.0 * dn_sh - dn_c)
    cost = up_c + dn_c
    return pnl, cost


def backtest_market(name, db_path, interval, dip, price_cap):
    df = load_candles(db_path, interval)
    up = df[df['outcome'] == 'Up']
    dn = df[df['outcome'] == 'Down']

    candles = df['candle_start'].unique()
    results = []

    for cs in candles:
        up_c = up[up['candle_start'] == cs]
        dn_c = dn[dn['candle_start'] == cs]
        up_fills, dn_fills, winner = simulate_candle(up_c, dn_c, dip, price_cap)
        if winner is None or (not up_fills and not dn_fills):
            continue
        pnl, cost = calc_pnl(up_fills, dn_fills, winner)
        results.append({
            'winner':   winner,
            'n_up':     len(up_fills),
            'n_dn':     len(dn_fills),
            'cost':     cost,
            'pnl':      pnl,
            'win':      pnl > 0,
            'both':     len(up_fills) > 0 and len(dn_fills) > 0,
        })

    if not results:
        return None
    r = pd.DataFrame(results)
    return {
        'market':     name,
        'dip':        dip,
        'cap':        price_cap,
        'candles':    len(r),
        'wr':         r['win'].mean() * 100,
        'both_pct':   r['both'].mean() * 100,
        'avg_fills':  (r['n_up'] + r['n_dn']).mean(),
        'avg_cost':   r['cost'].mean(),
        'avg_pnl':    r['pnl'].mean(),
        'net_pnl':    r['pnl'].sum(),
        'roi':        r['pnl'].sum() / r['cost'].sum() * 100 if r['cost'].sum() > 0 else 0,
    }


# ── Run all configs ────────────────────────────────────────────────────────────
DIPS      = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10]
CAPS      = [0.35, 0.40, 0.45]

all_results = []
for name, path in DB_PATHS.items():
    print(f"  Running {name}...", end='', flush=True)
    for dip, cap in product(DIPS, CAPS):
        r = backtest_market(name, path, INTERVALS[name], dip, cap)
        if r:
            all_results.append(r)
    print(" done")

res = pd.DataFrame(all_results)

# ── Print results by dip+cap (summed across all markets) ──────────────────────
print(f"\n{'='*80}")
print(f"  ZIGZAG BACKTEST — All 4 Markets Combined")
print(f"  Shares={SHARES}/fill  MinInterval={MIN_INTERVAL}s")
print(f"{'='*80}")
print(f"  {'Dip':>6} {'Cap':>5} {'Candles':>8} {'WR%':>6} {'Both%':>7} "
      f"{'AvgFills':>9} {'AvgCost':>9} {'AvgPnL':>9} {'NetPnL':>10} {'ROI%':>7}")
print(f"  {'-'*78}")

for (dip, cap), grp in res.groupby(['dip', 'cap']):
    net    = grp['net_pnl'].sum()
    cost   = (grp['candles'] * grp['avg_cost']).sum()
    roi    = net / cost * 100 if cost > 0 else 0
    wr     = grp['wr'].mean()
    both   = grp['both_pct'].mean()
    fills  = grp['avg_fills'].mean()
    ac     = grp['avg_cost'].mean()
    ap     = grp['avg_pnl'].mean()
    n      = grp['candles'].sum()
    print(f"  {dip:>6.2f} {cap:>5.2f} {n:>8} {wr:>6.1f} {both:>7.1f} "
          f"{fills:>9.1f} {ac:>9.2f} {ap:>9.2f} {net:>10.2f} {roi:>7.1f}")

# ── Best configs ───────────────────────────────────────────────────────────────
print(f"\n  === Top 10 configs by ROI ===")
summary = []
for (dip, cap), grp in res.groupby(['dip', 'cap']):
    net  = grp['net_pnl'].sum()
    cost = (grp['candles'] * grp['avg_cost']).sum()
    roi  = net / cost * 100 if cost > 0 else 0
    summary.append({'dip': dip, 'cap': cap, 'net_pnl': net, 'roi': roi,
                    'candles': grp['candles'].sum(), 'wr': grp['wr'].mean()})

top = pd.DataFrame(summary).sort_values('roi', ascending=False).head(10)
print(f"  {'Dip':>6} {'Cap':>5} {'Candles':>8} {'WR%':>6} {'NetPnL':>10} {'ROI%':>7}")
print(f"  {'-'*46}")
for _, row in top.iterrows():
    print(f"  {row['dip']:>6.2f} {row['cap']:>5.2f} {row['candles']:>8.0f} "
          f"{row['wr']:>6.1f} {row['net_pnl']:>10.2f} {row['roi']:>7.1f}")

# ── Per-market breakdown for best dip ─────────────────────────────────────────
best_dip = top.iloc[0]['dip']
best_cap = top.iloc[0]['cap']
print(f"\n  === Best config: dip={best_dip}, cap={best_cap} — per market ===")
print(f"  {'Market':>10} {'Candles':>8} {'WR%':>6} {'Both%':>7} {'AvgFills':>9} "
      f"{'AvgCost':>9} {'NetPnL':>10} {'ROI%':>7}")
print(f"  {'-'*66}")
sub = res[(res['dip'] == best_dip) & (res['cap'] == best_cap)]
for _, row in sub.iterrows():
    print(f"  {row['market']:>10} {row['candles']:>8.0f} {row['wr']:>6.1f} "
          f"{row['both_pct']:>7.1f} {row['avg_fills']:>9.1f} {row['avg_cost']:>9.2f} "
          f"{row['net_pnl']:>10.2f} {row['roi']:>7.1f}")
