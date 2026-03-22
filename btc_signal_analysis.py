"""
BTC Price Signal Analysis — Entry Signal Discovery

For each wallet buy order, compute:
  - BTC actual price momentum (30s, 60s, 120s lookback)
  - Polymarket odds momentum (how fast the side's mid was moving)
  - Time within candle when entry occurred
  - Whether buy was contrarian (buy Up when BTC falling, buy Down when BTC rising)

Goal: Find the signal that predicts WHEN to buy each side.
"""

import sqlite3
import csv
import bisect
from collections import defaultdict
from datetime import datetime, timezone

BTC_15M_DB = r'C:\Users\James\polybotanalysis\market_btc_15m.db'
BTC_5M_DB  = r'C:\Users\James\polybotanalysis\market_btc_5m.db'

CANDLE_INTERVAL = 900  # 15m

# ── Load BTC actual price series ─────────────────────────────────────────────
def load_btc_prices(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT unix_time, price FROM asset_price ORDER BY unix_time'
    ).fetchall()
    conn.close()
    times  = [r[0] for r in rows]
    prices = [r[1] for r in rows]
    return times, prices

def btc_price_at(times, prices, ts):
    """Return nearest BTC price at or just before ts."""
    idx = bisect.bisect_right(times, ts) - 1
    if idx < 0:
        return None
    return prices[idx]

def btc_momentum(times, prices, ts, lookback_s):
    """Return (price_now - price_lookback) / price_lookback as a fraction."""
    p_now = btc_price_at(times, prices, ts)
    p_old = btc_price_at(times, prices, ts - lookback_s)
    if p_now is None or p_old is None or p_old == 0:
        return None
    return (p_now - p_old) / p_old

# ── Load Polymarket odds series ───────────────────────────────────────────────
def load_odds_series(db_path):
    """Returns dict: market_id -> sorted list of (unix_time, outcome, mid)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT unix_time, market_id, outcome, mid FROM polymarket_odds ORDER BY unix_time'
    ).fetchall()
    conn.close()
    series = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, mid_id, outcome, mid in rows:
        if outcome in ('Up', 'Down'):
            series[mid_id][outcome].append((float(ts), float(mid)))
    return series

def odds_momentum(series_list, ts, lookback_s):
    """mid change over last lookback_s seconds."""
    if not series_list:
        return None
    times = [x[0] for x in series_list]
    idx_now = bisect.bisect_right(times, ts) - 1
    idx_old = bisect.bisect_right(times, ts - lookback_s) - 1
    if idx_now < 0 or idx_old < 0:
        return None
    return series_list[idx_now][1] - series_list[idx_old][1]

# ── Parse timestamp from wallet CSVs ─────────────────────────────────────────
def parse_ts(val):
    val = str(val).strip()
    # Unix timestamp (wallet_8_fresh style)
    try:
        f = float(val)
        if f > 1e9:
            return f
    except ValueError:
        pass
    # "2026-03-19 22:32:15.000" style
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    return None

# ── Load wallet CSV ───────────────────────────────────────────────────────────
def load_wallet_csv(path):
    trades = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # detect timestamp column
            ts_col = 'timestamp' if 'timestamp' in row else None
            if ts_col is None:
                continue
            ts = parse_ts(row[ts_col])
            if ts is None:
                continue
            side    = row.get('side', '').upper()
            outcome = row.get('outcome', '')
            price   = float(row.get('price', 0) or 0)
            market  = row.get('market', '')
            trades.append({
                'ts': ts, 'side': side, 'outcome': outcome,
                'price': price, 'market': market,
            })
    return trades

# ── Find market_id for a trade by fuzzy timestamp+question match ─────────────
def build_market_lookup(db_path):
    """Return dict: candle_start -> list of market_ids for BTC markets."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT DISTINCT market_id, question, MIN(unix_time) as first_tick '
        'FROM polymarket_odds GROUP BY market_id'
    ).fetchall()
    conn.close()
    lookup = {}
    for mid, question, first_tick in rows:
        if 'Bitcoin' in question or 'BTC' in question:
            candle_start = (int(first_tick) // CANDLE_INTERVAL) * CANDLE_INTERVAL
            lookup[candle_start] = mid
    return lookup

# ──────────────────────────────────────────────────────────────────────────────
# MAIN ANALYSIS
# ──────────────────────────────────────────────────────────────────────────────

print("Loading BTC actual price data...")
btc_times, btc_prices = load_btc_prices(BTC_15M_DB)
print(f"  {len(btc_times)} price ticks, "
      f"{datetime.utcfromtimestamp(btc_times[0]):%Y-%m-%d} to "
      f"{datetime.utcfromtimestamp(btc_times[-1]):%Y-%m-%d}")

print("Loading Polymarket odds series...")
odds = load_odds_series(BTC_15M_DB)
market_lookup = build_market_lookup(BTC_15M_DB)
print(f"  {len(odds)} market_ids | {len(market_lookup)} BTC candle slots")

WALLETS = {
    'wallet_1': 'wallet_1_trades.csv',
    'wallet_8': 'wallet_8_fresh.csv',
    'wallet_9': 'wallet_9_trades.csv',
}

LOOKBACKS = [15, 30, 60, 120, 300]   # seconds

for wallet_name, csv_file in WALLETS.items():
    print(f"\n{'='*72}")
    print(f"WALLET: {wallet_name}  ({csv_file})")
    print(f"{'='*72}")

    trades = load_wallet_csv(csv_file)
    buys = [t for t in trades if t['side'] == 'BUY' and
            ('Bitcoin' in t['market'] or 'BTC' in t['market'])]
    # Filter to weekdays only (Mon-Fri)
    buys = [t for t in buys if datetime.fromtimestamp(t['ts'], tz=timezone.utc).weekday() < 5]
    print(f"  {len(buys)} BTC buy orders on weekdays")

    if not buys:
        print("  No data — skipping")
        continue

    # For each buy, compute signals
    rows = []
    for t in buys:
        ts    = t['ts']
        out   = t['outcome']   # 'Up' or 'Down'
        price = t['price']

        candle_start = (int(ts) // CANDLE_INTERVAL) * CANDLE_INTERVAL
        time_in_candle = ts - candle_start

        # BTC actual momentum at various lookbacks
        mom = {lb: btc_momentum(btc_times, btc_prices, ts, lb) for lb in LOOKBACKS}

        # BTC actual price at entry
        p_btc = btc_price_at(btc_times, btc_prices, ts)

        # Odds momentum for THIS side
        mid_id = market_lookup.get(candle_start)
        if mid_id and mid_id in odds:
            odds_mom_30 = odds_momentum(odds[mid_id][out], ts, 30)
            odds_mom_60 = odds_momentum(odds[mid_id][out], ts, 60)
        else:
            odds_mom_30 = odds_mom_60 = None

        # Is this contrarian? contrarian = buy Up when BTC falling, buy Down when BTC rising
        m60 = mom.get(60)
        if m60 is not None:
            if out == 'Up' and m60 < 0:
                direction = 'CONTRARIAN'
            elif out == 'Down' and m60 > 0:
                direction = 'CONTRARIAN'
            else:
                direction = 'MOMENTUM'
        else:
            direction = 'UNKNOWN'

        rows.append({
            'ts': ts, 'outcome': out, 'price': price,
            'time_in_candle': time_in_candle,
            'btc_price': p_btc,
            'mom15': mom[15], 'mom30': mom[30], 'mom60': mom[60],
            'mom120': mom[120], 'mom300': mom[300],
            'odds_mom_30': odds_mom_30, 'odds_mom_60': odds_mom_60,
            'direction': direction,
        })

    if not rows:
        continue

    # ── Directional analysis ──────────────────────────────────────────────────
    contrarian = [r for r in rows if r['direction'] == 'CONTRARIAN']
    momentum   = [r for r in rows if r['direction'] == 'MOMENTUM']
    unknown    = [r for r in rows if r['direction'] == 'UNKNOWN']
    pct_contra = 100 * len(contrarian) / max(len(rows), 1)
    print(f"\n  Direction breakdown (based on 60s BTC momentum):")
    print(f"    Contrarian: {len(contrarian):>5} ({pct_contra:.1f}%)")
    print(f"    Momentum:   {len(momentum):>5} ({100*len(momentum)/max(len(rows),1):.1f}%)")
    print(f"    Unknown:    {len(unknown):>5}")

    # ── By outcome: Up vs Down buys ───────────────────────────────────────────
    up_buys = [r for r in rows if r['outcome'] == 'Up']
    dn_buys = [r for r in rows if r['outcome'] == 'Down']

    print(f"\n  BTC momentum WHEN buying Up (n={len(up_buys)}):")
    for lb_key in ['mom15', 'mom30', 'mom60', 'mom120']:
        vals = [r[lb_key] for r in up_buys if r[lb_key] is not None]
        if vals:
            avg = sum(vals) / len(vals) * 100
            pos = sum(1 for v in vals if v > 0)
            neg = sum(1 for v in vals if v < 0)
            lb = lb_key[3:]  # '15', '30', etc.
            print(f"    {lb:>5}s: avg={avg:+.4f}%  pos={pos}({100*pos/len(vals):.0f}%)  neg={neg}({100*neg/len(vals):.0f}%)")

    print(f"\n  BTC momentum WHEN buying Down (n={len(dn_buys)}):")
    for lb_key in ['mom15', 'mom30', 'mom60', 'mom120']:
        vals = [r[lb_key] for r in dn_buys if r[lb_key] is not None]
        if vals:
            avg = sum(vals) / len(vals) * 100
            pos = sum(1 for v in vals if v > 0)
            neg = sum(1 for v in vals if v < 0)
            lb = lb_key[3:]
            print(f"    {lb:>5}s: avg={avg:+.4f}%  pos={pos}({100*pos/len(vals):.0f}%)  neg={neg}({100*neg/len(vals):.0f}%)")

    # ── Entry timing within candle ────────────────────────────────────────────
    tic_vals = [r['time_in_candle'] for r in rows]
    print(f"\n  Time-in-candle distribution (0=candle start, 900=end):")
    buckets = [0, 60, 180, 300, 450, 600, 750, 900]
    counts  = [0] * (len(buckets) - 1)
    for v in tic_vals:
        for i in range(len(buckets) - 1):
            if buckets[i] <= v < buckets[i+1]:
                counts[i] += 1
                break
    for i, c in enumerate(counts):
        bar = '#' * (c // max(max(counts)//30, 1))
        print(f"    {buckets[i]:>4}s-{buckets[i+1]:>4}s: {c:>5} {bar}")

    # ── Odds momentum at entry ────────────────────────────────────────────────
    print(f"\n  Polymarket odds momentum at entry:")
    for side_label, side_rows in [('Up buys', up_buys), ('Down buys', dn_buys)]:
        vals30 = [r['odds_mom_30'] for r in side_rows if r['odds_mom_30'] is not None]
        vals60 = [r['odds_mom_60'] for r in side_rows if r['odds_mom_60'] is not None]
        if vals30:
            avg30 = sum(vals30) / len(vals30)
            neg30 = sum(1 for v in vals30 if v < -0.01)
            pos30 = sum(1 for v in vals30 if v > 0.01)
            print(f"    {side_label:10} odds_mom_30: avg={avg30:+.4f}  "
                  f"falling(<-0.01)={neg30}({100*neg30/len(vals30):.0f}%)  "
                  f"rising(>+0.01)={pos30}({100*pos30/len(vals30):.0f}%)")
        if vals60:
            avg60 = sum(vals60) / len(vals60)
            neg60 = sum(1 for v in vals60 if v < -0.01)
            pos60 = sum(1 for v in vals60 if v > 0.01)
            print(f"    {side_label:10} odds_mom_60: avg={avg60:+.4f}  "
                  f"falling(<-0.01)={neg60}({100*neg60/len(vals60):.0f}%)  "
                  f"rising(>+0.01)={pos60}({100*pos60/len(vals60):.0f}%)")

    # ── Entry price vs BTC momentum (scatter summary) ─────────────────────────
    print(f"\n  Entry price buckets vs 60s BTC momentum:")
    price_buckets = [(0.0, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 1.0)]
    print(f"    {'Price':^12}  {'N':>5}  {'AvgMom60':>10}  {'%Contra':>9}")
    for lo, hi in price_buckets:
        bucket_rows = [r for r in rows if lo <= r['price'] < hi and r['mom60'] is not None]
        if not bucket_rows:
            continue
        moms = [r['mom60'] for r in bucket_rows]
        contra = sum(1 for r in bucket_rows if r['direction'] == 'CONTRARIAN')
        avg_mom = sum(moms) / len(moms) * 100
        print(f"    [{lo:.2f}-{hi:.2f}):  {len(bucket_rows):>5}  {avg_mom:>+10.4f}%  {100*contra/len(bucket_rows):>8.1f}%")

    # ── Magnitude threshold search ────────────────────────────────────────────
    print(f"\n  Momentum magnitude at entry (|mom60| distribution):")
    mom60_vals = [abs(r['mom60']) * 100 for r in rows if r['mom60'] is not None]
    mom60_vals.sort()
    n = len(mom60_vals)
    if n:
        pcts = [25, 50, 75, 90, 95]
        for p in pcts:
            idx = int(p / 100 * n)
            print(f"    p{p:2d}: {mom60_vals[min(idx, n-1)]:.4f}%")

print("\nDone.")
