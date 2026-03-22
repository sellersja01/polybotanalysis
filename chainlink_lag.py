"""
Chainlink vs Coinbase BTC price lag analysis.
Fetches historical Chainlink BTC/USD round data via public Ethereum RPC,
compares with our collector's asset_price data to measure lag.
"""

import requests
import sqlite3
import json
import time
import struct
from datetime import datetime, timezone

# ── Chainlink contract ─────────────────────────────────────────────────────────
CHAINLINK_BTC = "0xf4030086522a5beea4988f8ca5b36dbc97bee88c"
ETH_RPC       = "https://eth.llamarpc.com"

# Function selectors
SEL_LATEST = "0xfeaf968c"  # latestRoundData()
SEL_ROUND  = "0x9a6fc8f5"  # getRoundData(uint80)

DB_PATH = r'C:\Users\James\polybotanalysis\market_btc_5m.db'


def eth_call(to, data):
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1
    }
    r = requests.post(ETH_RPC, json=payload, timeout=15)
    return r.json().get("result", "0x")


def decode_round_data(hex_result):
    """Decode latestRoundData/getRoundData response: (roundId, answer, startedAt, updatedAt, answeredInRound)"""
    if not hex_result or hex_result == "0x":
        return None
    data = bytes.fromhex(hex_result[2:])
    if len(data) < 160:
        return None
    round_id    = int.from_bytes(data[0:32],   'big')
    answer      = int.from_bytes(data[32:64],  'big')
    started_at  = int.from_bytes(data[64:96],  'big')
    updated_at  = int.from_bytes(data[96:128], 'big')
    # answer has 8 decimals
    price = answer / 1e8
    return {'round_id': round_id, 'price': price, 'updated_at': updated_at}


def encode_round_id(round_id):
    """Encode getRoundData(uint80) call."""
    # Function selector + uint80 padded to 32 bytes
    rid_hex = hex(round_id)[2:].zfill(64)
    return SEL_ROUND + rid_hex


def get_latest_round():
    result = eth_call(CHAINLINK_BTC, SEL_LATEST)
    return decode_round_data(result)


def get_round(round_id):
    data   = encode_round_id(round_id)
    result = eth_call(CHAINLINK_BTC, data)
    return decode_round_data(result)


def get_round_retry(round_id, retries=4):
    for attempt in range(retries):
        try:
            r = get_round(round_id)
            if r is not None:
                return r
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return None


def fetch_chainlink_history(start_ts, end_ts):
    """
    Binary search to find the first round >= start_ts,
    then walk forward collecting all rounds up to end_ts.
    Much fewer RPC calls than walking back from latest.
    """
    print("  Fetching latest Chainlink round...")
    latest = get_latest_round()
    if not latest:
        print("  Failed to get latest round")
        return []

    print(f"  Latest: round={latest['round_id']}, price=${latest['price']:.2f}, "
          f"ts={datetime.fromtimestamp(latest['updated_at'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    phase_id   = latest['round_id'] >> 64
    latest_agg = latest['round_id'] & 0xFFFFFFFFFFFFFFFF

    # Binary search for round closest to start_ts
    print(f"  Binary searching for round at start_ts...")
    lo, hi = 1, latest_agg
    while hi - lo > 1:
        mid = (lo + hi) // 2
        rid = (phase_id << 64) | mid
        r = get_round_retry(rid)
        time.sleep(0.15)
        if r is None or r['updated_at'] == 0:
            hi = mid
            continue
        if r['updated_at'] < start_ts:
            lo = mid
        else:
            hi = mid

    start_agg = max(1, lo - 2)
    print(f"  Found start agg_round ~{start_agg}, walking forward...")

    # Walk forward from start_agg to end_ts
    rounds = []
    agg = start_agg
    consecutive_fails = 0
    while agg <= latest_agg:
        rid = (phase_id << 64) | agg
        r = get_round_retry(rid)
        time.sleep(0.15)

        if r is None or r['updated_at'] == 0:
            consecutive_fails += 1
            if consecutive_fails > 10:
                break
            agg += 1
            continue

        consecutive_fails = 0

        if r['updated_at'] > end_ts:
            break
        if r['updated_at'] >= start_ts:
            rounds.append(r)
            ts_str = datetime.fromtimestamp(r['updated_at'], tz=timezone.utc).strftime('%H:%M:%S')
            print(f"  Round {agg}: ${r['price']:.2f} @ {ts_str}")

        agg += 1

    rounds.sort(key=lambda x: x['updated_at'])
    return rounds


def load_coinbase_prices(start_ts, end_ts):
    """Load BTC prices from our collector DB."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT unix_time, price FROM asset_price
        WHERE unix_time >= ? AND unix_time <= ?
        ORDER BY unix_time
    """, (start_ts, end_ts)).fetchall()
    conn.close()
    return [(float(ts), float(p)) for ts, p in rows]


def find_price_at(prices, target_ts):
    """Find closest price to target_ts."""
    if not prices:
        return None
    best = min(prices, key=lambda x: abs(x[0] - target_ts))
    if abs(best[0] - target_ts) > 300:  # more than 5 min away
        return None
    return best[1]


# ── Main analysis ──────────────────────────────────────────────────────────────
print("=" * 65)
print("  Chainlink BTC/USD vs Coinbase — Lag Analysis")
print("=" * 65)

# Get time range from our DB
conn = sqlite3.connect(DB_PATH)
row = conn.execute("SELECT MIN(unix_time), MAX(unix_time) FROM asset_price WHERE price > 0").fetchone()
conn.close()
db_start, db_end = float(row[0]), float(row[1])

print(f"\n  DB range: {datetime.fromtimestamp(db_start, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} "
      f"to {datetime.fromtimestamp(db_end, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")

# Use last 6 hours of DB data for analysis (manageable number of rounds)
analysis_end   = db_end
analysis_start = db_end - 6 * 3600

print(f"  Analyzing last 6 hours: "
      f"{datetime.fromtimestamp(analysis_start, tz=timezone.utc).strftime('%H:%M')} to "
      f"{datetime.fromtimestamp(analysis_end, tz=timezone.utc).strftime('%H:%M')} UTC\n")

# Fetch Chainlink rounds
cl_rounds = fetch_chainlink_history(analysis_start, analysis_end)
print(f"\n  Found {len(cl_rounds)} Chainlink rounds in range")

if len(cl_rounds) < 2:
    print("  Not enough rounds — try expanding the time range")
    exit()

# Load Coinbase prices
cb_prices = load_coinbase_prices(analysis_start, analysis_end)
print(f"  Loaded {len(cb_prices)} Coinbase price ticks\n")

# ── Compare at each Chainlink update ─────────────────────────────────────────
print(f"  {'Time':>8} {'CL Price':>10} {'CB Price':>10} {'Diff$':>8} {'Diff%':>7} {'Gap_secs':>10}")
print(f"  {'-'*58}")

lags_secs   = []
price_diffs = []

for i in range(1, len(cl_rounds)):
    prev = cl_rounds[i-1]
    curr = cl_rounds[i]

    gap_secs = curr['updated_at'] - prev['updated_at']
    cl_price = curr['price']

    # What was Coinbase price right before this Chainlink update?
    cb_price = find_price_at(cb_prices, curr['updated_at'] - 5)
    if cb_price is None:
        continue

    diff_usd = cb_price - cl_price
    diff_pct = 100 * diff_usd / cl_price

    ts_str = datetime.fromtimestamp(curr['updated_at'], tz=timezone.utc).strftime('%H:%M:%S')
    print(f"  {ts_str:>8} ${cl_price:>9.2f} ${cb_price:>9.2f} {diff_usd:>+8.2f} {diff_pct:>+6.3f}% {gap_secs:>9.0f}s")

    lags_secs.append(gap_secs)
    price_diffs.append(abs(diff_usd))

if lags_secs:
    import statistics
    print(f"\n  === Summary ===")
    print(f"  Chainlink update interval:  avg={statistics.mean(lags_secs):.1f}s  "
          f"median={statistics.median(lags_secs):.1f}s  "
          f"max={max(lags_secs):.0f}s")
    print(f"  Price diff at update time:  avg=${statistics.mean(price_diffs):.2f}  "
          f"max=${max(price_diffs):.2f}")
    print(f"\n  Key question: during the GAP between updates, how far does CB move?")

    # For each gap, find max CB price deviation
    print(f"\n  {'Gap start':>10} {'Gap_s':>6} {'CL$':>10} {'CB_max_dev$':>12} {'CB_max_dev%':>12}")
    print(f"  {'-'*54}")
    for i in range(len(cl_rounds)-1):
        curr = cl_rounds[i]
        nxt  = cl_rounds[i+1]
        gap  = nxt['updated_at'] - curr['updated_at']
        if gap < 10:
            continue
        # CB prices during this gap
        gap_prices = [p for ts, p in cb_prices if curr['updated_at'] <= ts <= nxt['updated_at']]
        if not gap_prices:
            continue
        cl_p   = curr['price']
        max_dev = max(abs(p - cl_p) for p in gap_prices)
        max_pct = 100 * max_dev / cl_p
        ts_str  = datetime.fromtimestamp(curr['updated_at'], tz=timezone.utc).strftime('%H:%M:%S')
        print(f"  {ts_str:>10} {gap:>6.0f}s ${cl_p:>9.2f} {max_dev:>+12.2f} {max_pct:>+11.3f}%")
