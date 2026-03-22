"""
Chainlink Signal Backtest
At each candle open, compare Coinbase price vs stale Chainlink price.
If Coinbase is X dollars above Chainlink -> bet Up.
If Coinbase is X dollars below Chainlink -> bet Down.
Test how well this predicts candle outcome.
"""

import requests
import sqlite3
import time
from datetime import datetime, timezone
from collections import defaultdict

ETH_RPC       = "https://eth.llamarpc.com"
CHAINLINK_BTC = "0xf4030086522a5beea4988f8ca5b36dbc97bee88c"
SEL_LATEST    = "0xfeaf968c"
SEL_ROUND     = "0x9a6fc8f5"

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}
INTERVALS = {'BTC_5m': 300, 'ETH_5m': 300, 'BTC_15m': 900, 'ETH_15m': 900}


# ── Chainlink RPC helpers ──────────────────────────────────────────────────────
def eth_call(data):
    payload = {"jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": CHAINLINK_BTC, "data": data}, "latest"], "id": 1}
    r = requests.post(ETH_RPC, json=payload, timeout=15)
    return r.json().get("result", "0x")


def decode_round(hex_result):
    if not hex_result or hex_result == "0x" or len(hex_result) < 10:
        return None
    data = bytes.fromhex(hex_result[2:])
    if len(data) < 128:
        return None
    round_id   = int.from_bytes(data[0:32],   'big')
    answer     = int.from_bytes(data[32:64],  'big')
    updated_at = int.from_bytes(data[96:128], 'big')
    if updated_at == 0:
        return None
    return {'round_id': round_id, 'price': answer / 1e8, 'updated_at': updated_at}


def get_round(round_id):
    for attempt in range(4):
        try:
            rid_hex = hex(round_id)[2:].zfill(64)
            result  = eth_call(SEL_ROUND + rid_hex)
            r = decode_round(result)
            if r:
                return r
        except Exception:
            pass
        time.sleep(0.4 * (attempt + 1))
    return None


def get_latest():
    for attempt in range(4):
        try:
            r = decode_round(eth_call(SEL_LATEST))
            if r:
                return r
        except Exception:
            pass
        time.sleep(0.5)
    return None


def fetch_all_rounds(start_ts, end_ts):
    """Binary search to start, then walk forward."""
    latest = get_latest()
    if not latest:
        return []

    phase_id   = latest['round_id'] >> 64
    latest_agg = latest['round_id'] & 0xFFFFFFFFFFFFFFFF

    # Binary search for start
    lo, hi = 1, latest_agg
    while hi - lo > 2:
        mid = (lo + hi) // 2
        r = get_round((phase_id << 64) | mid)
        time.sleep(0.15)
        if r is None:
            hi = mid
        elif r['updated_at'] < start_ts:
            lo = mid
        else:
            hi = mid

    # Walk forward from lo-5
    rounds = []
    agg    = max(1, lo - 5)
    fails  = 0
    while agg <= latest_agg:
        r = get_round((phase_id << 64) | agg)
        time.sleep(0.15)
        if r is None:
            fails += 1
            if fails > 8:
                break
            agg += 1
            continue
        fails = 0
        if r['updated_at'] > end_ts:
            break
        if r['updated_at'] >= start_ts - 7200:  # include 2h before start
            rounds.append(r)
        agg += 1

    return sorted(rounds, key=lambda x: x['updated_at'])


def chainlink_price_at(cl_rounds, ts):
    """Return the Chainlink price that was active at timestamp ts (most recent round before ts)."""
    active = None
    for r in cl_rounds:
        if r['updated_at'] <= ts:
            active = r
        else:
            break
    return active['price'] if active else None


# ── Load candle data ───────────────────────────────────────────────────────────
def load_candles(label, db_path, interval):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT unix_time, outcome, ask, mid FROM polymarket_odds
        WHERE outcome IN ('Up','Down') AND ask > 0 AND mid > 0
        ORDER BY unix_time
    """).fetchall()
    btc_rows = conn.execute("""
        SELECT unix_time, price FROM asset_price
        WHERE price > 0 ORDER BY unix_time
    """).fetchall()
    conn.close()

    btc_prices = [(float(ts), float(p)) for ts, p in btc_rows]

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, out, ask, mid in rows:
        cs = (int(float(ts)) // interval) * interval
        candles[cs][out].append((float(ts), float(ask), float(mid)))

    return candles, btc_prices


def btc_at(prices, ts):
    if not prices:
        return None
    lo, hi = 0, len(prices) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if prices[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    best = min(lo, len(prices)-1)
    if abs(prices[best][0] - ts) > 120:
        return None
    return prices[best][1]


# ── Main ───────────────────────────────────────────────────────────────────────
print("=" * 70)
print("  Chainlink Signal Backtest — BTC 5m + 15m")
print("=" * 70)

# Get DB time range
conn = sqlite3.connect(DBS['BTC_5m'])
row = conn.execute("SELECT MIN(unix_time), MAX(unix_time) FROM asset_price WHERE price > 0").fetchone()
conn.close()
db_start, db_end = float(row[0]), float(row[1])
print(f"\n  DB range: {datetime.fromtimestamp(db_start, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} "
      f"to {datetime.fromtimestamp(db_end, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

# Fetch Chainlink rounds for entire DB range
print(f"\n  Fetching Chainlink rounds for full DB range...")
cl_rounds = fetch_all_rounds(db_start, db_end)
print(f"  Got {len(cl_rounds)} Chainlink rounds")
if not cl_rounds:
    print("  No rounds found — exiting")
    exit()

for r in cl_rounds[:3]:
    ts = datetime.fromtimestamp(r['updated_at'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
    print(f"  Round: ${r['price']:.2f} @ {ts}")

# ── Backtest per market ────────────────────────────────────────────────────────
THRESHOLDS = [50, 100, 150, 200, 250, 300]  # USD discrepancy to trigger signal

print(f"\n  {'Market':>8} {'Thresh$':>8} {'Signals':>8} {'WR%':>6} {'Coverage%':>11}")
print(f"  {'-'*50}")

all_candle_data = {}

for label in ['BTC_5m', 'BTC_15m']:
    candles, btc_prices = load_candles(label, DBS[label], INTERVALS[label])
    all_candle_data[label] = (candles, btc_prices)

    candle_results = []
    for cs, sides in candles.items():
        up = sides['Up']
        dn = sides['Down']
        if not up or not dn:
            continue
        final_mid = up[-1][2]
        if   final_mid >= 0.85: winner = 'Up'
        elif final_mid <= 0.15: winner = 'Down'
        else:                   continue

        # Coinbase price at candle open
        cb_open = btc_at(btc_prices, cs)
        if cb_open is None:
            continue

        # Chainlink price active at candle open
        cl_open = chainlink_price_at(cl_rounds, cs)
        if cl_open is None:
            continue

        discrepancy = cb_open - cl_open  # positive = CB above CL = bullish signal

        candle_results.append({
            'cs':           cs,
            'winner':       winner,
            'cb_open':      cb_open,
            'cl_open':      cl_open,
            'discrepancy':  discrepancy,
        })

    total = len(candle_results)
    for thresh in THRESHOLDS:
        signals = [r for r in candle_results if abs(r['discrepancy']) >= thresh]
        if not signals:
            continue
        correct = sum(
            1 for r in signals if
            (r['discrepancy'] > 0 and r['winner'] == 'Up') or
            (r['discrepancy'] < 0 and r['winner'] == 'Down')
        )
        wr       = 100 * correct / len(signals)
        coverage = 100 * len(signals) / total
        print(f"  {label:>8} {thresh:>7}$ {len(signals):>8} {wr:>6.1f} {coverage:>10.1f}%")

# ── Detailed breakdown for best threshold ─────────────────────────────────────
print(f"\n  === Discrepancy distribution (BTC_5m) ===")
candles, btc_prices = all_candle_data['BTC_5m']
results = []
for cs, sides in candles.items():
    up = sides['Up']; dn = sides['Down']
    if not up or not dn: continue
    final_mid = up[-1][2]
    if   final_mid >= 0.85: winner = 'Up'
    elif final_mid <= 0.15: winner = 'Down'
    else: continue
    cb_open = btc_at(btc_prices, cs)
    cl_open = chainlink_price_at(cl_rounds, cs)
    if cb_open is None or cl_open is None: continue
    results.append({'winner': winner, 'disc': cb_open - cl_open})

buckets = [(-999,-200),(-200,-100),(-100,-50),(-50,0),(0,50),(50,100),(100,200),(200,999)]
print(f"  {'Disc range':>18} {'N':>5} {'Up wins':>8} {'WR_Up%':>8}")
print(f"  {'-'*44}")
for lo, hi in buckets:
    sub = [r for r in results if lo <= r['disc'] < hi]
    if not sub: continue
    up_wins = sum(1 for r in sub if r['winner'] == 'Up')
    wr = 100 * up_wins / len(sub)
    print(f"  {f'${lo} to ${hi}':>18} {len(sub):>5} {up_wins:>8} {wr:>7.1f}%")
