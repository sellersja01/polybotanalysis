import requests
import time
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime
import calendar
import re

WALLET = "0x29bc82f761749e67fa00d62896bc6855097b683c"

# 2PM-4PM ET on March 20 = 6PM-8PM UTC = unix 1774040400 to 1774047600
# ET is UTC-4
START_TS = 1774033200  # 2:00PM ET March 20
END_TS   = 1774040400  # 4:00PM ET March 20

print(f"Fetching trades for {WALLET}")
print(f"Window: 2:00PM - 4:00PM ET March 20")

trades = []
for offset in range(0, 3000, 100):
    url = f"https://data-api.polymarket.com/activity?user={WALLET}&limit=100&offset={offset}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if not data:
            break
        # Filter to our time window
        window = [t for t in data if START_TS <= float(t.get('timestamp', t.get('createdAt', 0))) <= END_TS]
        trades.extend(window)
        # If all trades in this batch are before our window, stop
        earliest = min(float(t.get('timestamp', t.get('createdAt', 0))) for t in data)
        if earliest < START_TS:
            break
        time.sleep(0.3)
    except Exception as e:
        print(f"  Error at offset {offset}: {e}")
        break

print(f"Trades in window: {len(trades)}")

if not trades:
    print("No trades found in 2-4PM window. Try a wider window.")
    print("Fetching last 500 trades to check timestamps...")
    trades_check = []
    for offset in [0, 100, 200, 300, 400]:
        url = f"https://data-api.polymarket.com/activity?user={WALLET}&limit=100&offset={offset}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not data: break
        trades_check.extend(data)
        time.sleep(0.3)
    if trades_check:
        ts_list = sorted([float(t.get('timestamp', 0)) for t in trades_check])
        print(f"Earliest trade ts: {ts_list[0]} = {datetime.utcfromtimestamp(ts_list[0])}")
        print(f"Latest trade ts:   {ts_list[-1]} = {datetime.utcfromtimestamp(ts_list[-1])}")
        print(f"Your local DB range ends around: {datetime.utcfromtimestamp(1773876276)} (BTC 5M)")
    exit()

# Save CSV
fname = "bosh_2to4pm.csv"
with open(fname, "w", newline="") as f:
    keys = ["timestamp", "type", "outcome", "price", "size", "market"]
    w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for t in trades:
        row = {
            "timestamp": t.get("timestamp", t.get("createdAt", "")),
            "type": t.get("type", "TRADE"),
            "outcome": t.get("outcome", ""),
            "price": float(t.get("price", t.get("pricePerShare", 0)) or 0),
            "size": float(t.get("size", t.get("shares", 0)) or 0),
            "market": t.get("market", t.get("title", "")),
        }
        w.writerow(row)
print(f"Saved to {fname}")

# Group by candle
def parse_candle_start(market_name):
    m = re.search(r'(\w+ \d+), (\d+:\d+)(AM|PM)', market_name)
    if not m: return None
    date_str = m.group(1)
    time_str = m.group(2)
    ampm = m.group(3)
    hour, minute = map(int, time_str.split(":"))
    if ampm == "PM" and hour != 12: hour += 12
    elif ampm == "AM" and hour == 12: hour = 0
    dt = datetime(2026, 3, 20, hour, minute, 0)
    return calendar.timegm(dt.timetuple()) + (4 * 3600)

candles = defaultdict(list)
for t in trades:
    if t.get('type') == 'REDEEM': continue
    market = t.get('market', t.get('title', ''))
    if 'Up or Down' not in market: continue
    candles[market].append(t)

# Cross reference with local databases
db_map = {
    'BTC': r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'ETH': r'C:\Users\James\polybotanalysis\market_eth_5m.db',
}

def get_candle_data(db_path, ts_start, ts_end):
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute('''
            SELECT unix_time, mid FROM polymarket_odds
            WHERE unix_time >= ? AND unix_time <= ? AND outcome = 'Up'
            ORDER BY unix_time ASC
        ''', (ts_start, ts_end)).fetchall()
        conn.close()
        if len(rows) < 2: return None, None
        return float(rows[0][1]), float(rows[-1][1])
    except:
        return None, None

print(f"\n{'Market':<52} {'Dom':<5} {'1st':<5} {'Open':<6} {'Close':<6} {'Move':<7} {'Match?'}")
print("-"*105)

correct = wrong = no_data = 0
for market, fills in sorted(candles.items()):
    if not fills: continue
    fills.sort(key=lambda x: float(x.get('timestamp', 0)))

    up = [f for f in fills if 'up' in f.get('outcome','').lower()]
    down = [f for f in fills if 'down' in f.get('outcome','').lower()]
    if not up or not down: continue

    up_usdc = sum(float(f['price'])*float(f['size']) for f in up)
    down_usdc = sum(float(f['price'])*float(f['size']) for f in down)
    dominant = "Up" if up_usdc > down_usdc else "Down"
    first_outcome = fills[0].get('outcome', '')

    asset = "BTC" if "Bitcoin" in market else "ETH"
    db = db_map.get(asset)
    ts_start = parse_candle_start(market)
    if not ts_start or not db:
        continue

    open_mid, close_mid = get_candle_data(db, ts_start, ts_start + 300)
    m_short = market[-50:]

    if open_mid is None:
        no_data += 1
        print(f"{m_short:<52} {dominant:<5} {first_outcome:<5} {'N/A':<6} {'N/A':<6} {'N/A':<7} NO DATA")
        continue

    move = close_mid - open_mid
    resolution = "Up" if close_mid >= 0.85 else ("Down" if close_mid <= 0.15 else "OPEN")
    match = "✓" if resolution == dominant else ("✗" if resolution != "OPEN" else "?")
    if resolution == dominant: correct += 1
    elif resolution != "OPEN": wrong += 1
    else: no_data += 1

    print(f"{m_short:<52} {dominant:<5} {first_outcome:<5} {open_mid:<6.3f} {close_mid:<6.3f} {move:+.3f}  {match} ({resolution})")

total = correct + wrong
print(f"\n{'='*60}")
print(f"Dominant matched resolution: {correct}/{total} ({100*correct//max(total,1)}%)")
print(f"Wrong: {wrong}  Unresolved/No data: {no_data}")
if total > 5:
    if correct/total > 0.65:
        print("→ They have a REAL directional signal")
    elif correct/total < 0.45:
        print("→ They are CONTRARIAN — betting against the move")
    else:
        print("→ No clear directional signal — likely pure arb")
