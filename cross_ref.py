import csv
import sqlite3
from collections import defaultdict
from datetime import datetime

# Parse candle time from market name to unix timestamp
# e.g. "Bitcoin Up or Down - March 20, 5:10PM-5:15PM ET"
import re
from datetime import datetime
import calendar

def parse_candle_start(market_name):
    """Extract start unix timestamp from market name"""
    # Match "March 20, 5:10PM-5:15PM"
    m = re.search(r'(\w+ \d+), (\d+:\d+)(AM|PM)', market_name)
    if not m:
        return None
    date_str = m.group(1)  # "March 20"
    time_str = m.group(2)  # "5:10"
    ampm = m.group(3)      # "PM"
    
    hour, minute = map(int, time_str.split(":"))
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    
    # 2026
    dt = datetime(2026, 3, 20, hour, minute, 0)
    # Convert to UTC (ET is UTC-4 in March)
    import time
    ts = calendar.timegm(dt.timetuple()) + (4 * 3600)
    return ts

# Load bosh trades and get per-candle dominant side
bosh_candles = {}
with open('bosh_trades.csv') as f:
    reader = csv.DictReader(f)
    trades = list(reader)

candles = defaultdict(list)
for t in trades:
    if t['type'] == 'REDEEM': continue
    market = t['market']
    if 'Up or Down' not in market: continue
    candles[market].append(t)

for market, fills in candles.items():
    up = [f for f in fills if 'up' in f['outcome'].lower()]
    down = [f for f in fills if 'down' in f['outcome'].lower()]
    if not up or not down: continue
    
    up_usdc = sum(float(f['price'])*float(f['size']) for f in up)
    down_usdc = sum(float(f['price'])*float(f['size']) for f in down)
    dominant = "Up" if up_usdc > down_usdc else "Down"
    
    # Get first entry timestamp and price
    all_fills = sorted(fills, key=lambda x: float(x['timestamp']))
    first_ts = float(all_fills[0]['timestamp'])
    first_outcome = all_fills[0]['outcome']
    first_price = float(all_fills[0]['price'])
    
    candle_start = parse_candle_start(market)
    
    asset = "BTC" if "Bitcoin" in market else "ETH"
    
    bosh_candles[market] = {
        'asset': asset,
        'candle_start': candle_start,
        'first_ts': first_ts,
        'first_outcome': first_outcome,
        'first_price': first_price,
        'dominant': dominant,
        'up_usdc': up_usdc,
        'down_usdc': down_usdc,
    }

print(f"Loaded {len(bosh_candles)} candles from bosh trades")

# Load price data from collector databases
def get_price_at(db_path, ts_start, ts_end, asset):
    """Get opening and closing mid price for a candle window"""
    try:
        conn = sqlite3.connect(db_path)
        # Get first and last mid price in window
        rows = conn.execute('''
            SELECT unix_time, mid, outcome FROM polymarket_odds
            WHERE unix_time >= ? AND unix_time <= ? AND outcome = 'Up'
            ORDER BY unix_time ASC
        ''', (ts_start, ts_end)).fetchall()
        conn.close()
        if len(rows) < 2:
            return None, None
        open_mid = rows[0][1]
        close_mid = rows[-1][1]
        return float(open_mid), float(close_mid)
    except Exception as e:
        return None, None

# Map asset to database
db_map = {
    'BTC': r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'ETH': r'C:\Users\James\polybotanalysis\market_eth_5m.db',
}

print("\nCross-referencing with price data...")
print(f"\n{'Market':<50} {'Dom':<5} {'1st':<5} {'Open':<6} {'Close':<6} {'Move':<7} {'Match?'}")
print("-"*100)

correct = 0
wrong = 0
no_data = 0

for market, info in sorted(bosh_candles.items(), key=lambda x: x[1]['candle_start'] or 0):
    if not info['candle_start']:
        continue
    
    db = db_map.get(info['asset'])
    if not db:
        continue
    
    ts_start = info['candle_start']
    ts_end = ts_start + 300  # 5 min candle
    
    open_mid, close_mid = get_price_at(db, ts_start, ts_end, info['asset'])
    
    if open_mid is None:
        no_data += 1
        market_short = market[-48:]
        print(f"{market_short:<50} {info['dominant']:<5} {info['first_outcome']:<5} {'N/A':<6} {'N/A':<6} {'N/A':<7} NO DATA")
        continue
    
    move = close_mid - open_mid
    price_direction = "Up" if move > 0 else "Down"
    match = "✓" if price_direction == info['dominant'] else "✗"
    
    if price_direction == info['dominant']:
        correct += 1
    else:
        wrong += 1
    
    market_short = market[-48:]
    print(f"{market_short:<50} {info['dominant']:<5} {info['first_outcome']:<5} {open_mid:<6.3f} {close_mid:<6.3f} {move:+.3f}  {match}")

total = correct + wrong
print(f"\n{'='*60}")
print(f"Dominant side matched actual resolution: {correct}/{total} ({100*correct//max(total,1)}%)")
print(f"Wrong: {wrong}  No data: {no_data}")
if total > 0:
    print(f"\nConclusion: Their dominant side predicted the winner {100*correct//total}% of the time")
    if correct/total > 0.65:
        print("→ They have a real directional signal, NOT just random arb")
    elif correct/total < 0.45:
        print("→ Their dominant side is CONTRARIAN — they bet against the move")
    else:
        print("→ No strong directional signal detected, might be pure arb")
