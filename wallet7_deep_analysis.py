"""
Deep analysis of wallet_7 — all done in SQL for speed.
Run on VPS: python3 wallet7_deep_analysis.py
"""
import sqlite3
from datetime import datetime, timezone

WALLET = 'wallet_7'
DB     = '/home/opc/wallet_trades.db'

conn = sqlite3.connect(DB)

# ── 1. Overall stats ──────────────────────────────────────────────────────────
row = conn.execute("""
    SELECT COUNT(*), MIN(timestamp), MAX(timestamp), SUM(usdc), AVG(usdc), SUM(size)
    FROM trades WHERE wallet_name=? AND side='BUY'
""", (WALLET,)).fetchone()
n, first_ts, last_ts, total_usdc, avg_usdc, total_size = row
days = (last_ts - first_ts) / 86400

print(f"\n{'='*60}")
print(f"  WALLET_7 DEEP ANALYSIS  ({n:,} BUY trades)")
print(f"{'='*60}")
print(f"  Period         : {datetime.fromtimestamp(first_ts,tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(last_ts,tz=timezone.utc).strftime('%Y-%m-%d')} ({days:.0f} days)")
print(f"  Total USDC     : ${total_usdc:>14,.2f}")
print(f"  USDC/day       : ${total_usdc/days:>14,.2f}")
print(f"  Trades/day     : {n/days:>14,.0f}")
print(f"  Avg trade size : ${avg_usdc:>14,.2f}")

# ── 2. By market (using LIKE in SQL) ─────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  BY MARKET")
print(f"{'='*60}")
market_queries = [
    ('BTC_5m',  "LOWER(market) LIKE '%bitcoin%' AND (market LIKE '%:__AM%' OR market LIKE '%:__PM%') AND market NOT LIKE '%:__AM-__:__AM%'"),
    ('BTC_5m',  "LOWER(market) LIKE '%bitcoin%'"),
    ('ETH_5m',  "LOWER(market) LIKE '%ethereum%'"),
    ('SOL',     "LOWER(market) LIKE '%solana%'"),
    ('XRP',     "LOWER(market) LIKE '%xrp%'"),
]
# Simpler: group by first word of market
rows = conn.execute("""
    SELECT
        CASE
            WHEN LOWER(market) LIKE '%bitcoin%'  THEN 'BTC'
            WHEN LOWER(market) LIKE '%ethereum%' THEN 'ETH'
            WHEN LOWER(market) LIKE '%solana%'   THEN 'SOL'
            WHEN LOWER(market) LIKE '%xrp%'      THEN 'XRP'
            ELSE 'OTHER'
        END as asset,
        COUNT(*) as trades,
        COUNT(DISTINCT market) as candles,
        SUM(usdc) as vol,
        AVG(price) as avg_price
    FROM trades WHERE wallet_name=? AND side='BUY'
    GROUP BY asset ORDER BY vol DESC
""", (WALLET,)).fetchall()

print(f"  {'Asset':<8} {'Trades':>8} {'Candles':>8} {'USDC vol':>13} {'USDC/candle':>12} {'Avg price':>10}")
print(f"  {'-'*63}")
for asset, trades, candles, vol, avg_price in rows:
    upc = vol/candles if candles else 0
    print(f"  {asset:<8} {trades:>8,} {candles:>8,} {vol:>13,.2f} {upc:>12,.2f} {avg_price:>10.4f}")

# ── 3. Up vs Down ─────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  UP vs DOWN")
print(f"{'='*60}")
rows = conn.execute("""
    SELECT outcome, COUNT(*), SUM(usdc), AVG(price), SUM(size)
    FROM trades WHERE wallet_name=? AND side='BUY'
    GROUP BY outcome
""", (WALLET,)).fetchall()
for out, cnt, vol, avg_p, tot_size in rows:
    print(f"  {out:<6} {cnt:>8,} trades | ${vol:>12,.2f} USDC | avg_price={avg_p:.4f} | shares={tot_size:,.0f}")

# ── 4. Entry price distribution ───────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  ENTRY PRICE DISTRIBUTION")
print(f"{'='*60}")
rows = conn.execute("""
    SELECT
        CAST(price*10 AS INTEGER) as bucket,
        COUNT(*) as cnt,
        SUM(usdc) as vol
    FROM trades WHERE wallet_name=? AND side='BUY'
    GROUP BY bucket ORDER BY bucket
""", (WALLET,)).fetchall()
max_cnt = max(r[1] for r in rows)
for bucket, cnt, vol in rows:
    lo = bucket / 10
    hi = lo + 0.1
    bar = '#' * (cnt * 40 // max_cnt)
    print(f"  {lo:.1f}-{hi:.1f}  {cnt:>7,}  ${vol:>10,.0f}  {bar}")

# ── 5. Position sizing per candle ─────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  POSITION SIZING PER CANDLE")
print(f"{'='*60}")
rows = conn.execute("""
    SELECT
        AVG(candle_cost) as avg_cost,
        MIN(candle_cost) as min_cost,
        MAX(candle_cost) as max_cost,
        COUNT(*) as n_candles,
        AVG(n_trades) as avg_trades_per_candle
    FROM (
        SELECT market, SUM(usdc) as candle_cost, COUNT(*) as n_trades
        FROM trades WHERE wallet_name=? AND side='BUY'
        GROUP BY market
    )
""", (WALLET,)).fetchone()
avg_cost, min_cost, max_cost, n_candles, avg_tpc = rows
print(f"  Candles traded     : {n_candles:,}")
print(f"  Avg USDC/candle    : ${avg_cost:,.2f}")
print(f"  Min USDC/candle    : ${min_cost:,.2f}")
print(f"  Max USDC/candle    : ${max_cost:,.2f}")
print(f"  Avg trades/candle  : {avg_tpc:.1f}")

# Percentile distribution of candle costs
rows2 = conn.execute("""
    SELECT candle_cost FROM (
        SELECT market, SUM(usdc) as candle_cost
        FROM trades WHERE wallet_name=? AND side='BUY'
        GROUP BY market
    ) ORDER BY candle_cost
""", (WALLET,)).fetchall()
costs = [r[0] for r in rows2]
n_c = len(costs)
for pct, label in [(10,'P10'),(25,'P25'),(50,'P50'),(75,'P75'),(90,'P90'),(95,'P95')]:
    idx = int(pct/100 * n_c)
    print(f"  {label}                 : ${costs[idx]:,.2f}")

# ── 6. Trades per candle distribution ────────────────────────────────────────
print(f"\n  Trades per candle distribution:")
rows3 = conn.execute("""
    SELECT n_trades, COUNT(*) as freq FROM (
        SELECT market, COUNT(*) as n_trades
        FROM trades WHERE wallet_name=? AND side='BUY'
        GROUP BY market
    ) GROUP BY n_trades ORDER BY n_trades
""", (WALLET,)).fetchall()
max_freq = max(r[1] for r in rows3)
for n_trades, freq in rows3:
    bar = '#' * (freq * 30 // max_freq)
    print(f"  {n_trades:>4} trades/candle: {freq:>5,}  {bar}")

# ── 7. Hourly activity (UTC) ──────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  ACTIVITY BY HOUR (UTC)")
print(f"{'='*60}")
rows = conn.execute("""
    SELECT
        CAST((timestamp % 86400) / 3600 AS INTEGER) as hour,
        COUNT(*) as trades,
        SUM(usdc) as vol
    FROM trades WHERE wallet_name=? AND side='BUY'
    GROUP BY hour ORDER BY hour
""", (WALLET,)).fetchall()
max_t = max(r[1] for r in rows)
for hour, trades, vol in rows:
    bar = '#' * (trades * 35 // max_t)
    print(f"  {hour:02d}:00  {trades:>7,}  ${vol:>10,.0f}  {bar}")

# ── 8. Day of week ────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  ACTIVITY BY DAY OF WEEK")
print(f"{'='*60}")
# SQLite: strftime('%w') = 0 Sunday, 1 Monday...
rows = conn.execute("""
    SELECT
        CAST(strftime('%w', datetime(timestamp, 'unixepoch')) AS INTEGER) as dow,
        COUNT(*) as trades,
        SUM(usdc) as vol
    FROM trades WHERE wallet_name=? AND side='BUY'
    GROUP BY dow ORDER BY dow
""", (WALLET,)).fetchall()
days_name = {0:'Sun',1:'Mon',2:'Tue',3:'Wed',4:'Thu',5:'Fri',6:'Sat'}
max_t = max(r[1] for r in rows)
for dow, trades, vol in rows:
    bar = '#' * (trades * 35 // max_t)
    print(f"  {days_name[dow]}  {trades:>7,}  ${vol:>12,.0f}  {bar}")

# ── 9. Recent activity (last 7 days) ─────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  RECENT ACTIVITY (last 7 days by day)")
print(f"{'='*60}")
rows = conn.execute("""
    SELECT
        date(datetime(timestamp,'unixepoch')) as day,
        COUNT(*) as trades,
        COUNT(DISTINCT market) as candles,
        SUM(usdc) as vol
    FROM trades WHERE wallet_name=? AND side='BUY'
      AND timestamp >= strftime('%s','now','-7 days')
    GROUP BY day ORDER BY day
""", (WALLET,)).fetchall()
for day, trades, candles, vol in rows:
    print(f"  {day}  {trades:>6,} trades | {candles:>4} candles | ${vol:>10,.2f}")

conn.close()
print(f"\n{'='*60}\n")
