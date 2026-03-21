import requests
import time
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime
import calendar
import re

WALLET = "0x29bc82f761749e67fa00d62896bc6855097b683c"

# Load trades from the CSV we already pulled
trades = []
with open('bosh_trades.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        trades.append(row)

print(f"Loaded {len(trades)} trades from bosh_trades.csv")

def parse_candle_start(market_name):
    m = re.search(r'(\w+ \d+), (\d+:\d+)(AM|PM)', market_name)
    if not m: return None
    time_str = m.group(2)
    ampm = m.group(3)
    hour, minute = map(int, time_str.split(":"))
    if ampm == "PM" and hour != 12: hour += 12
    elif ampm == "AM" and hour == 12: hour = 0
    dt = datetime(2026, 3, 20, hour, minute, 0)
    return calendar.timegm(dt.timetuple()) + (4 * 3600)  # ET = UTC-4

# Group trades by candle
candles = defaultdict(list)
for t in trades:
    if t.get('type') == 'REDEEM': continue
    market = t.get('market', '')
    if 'Up or Down' not in market: continue
    candles[market].append(t)

# Use TEST databases
db_map = {
    'BTC': r'C:\Users\James\polybotanalysis\market_btc_5m_test.db',
    'ETH': r'C:\Users\James\polybotanalysis\market_eth_5m_test.db',
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
        if len(rows) < 5: return None, None, None
        open_mid = float(rows[0][1])
        close_mid = float(rows[-1][1])
        # mid at 30s and 60s for momentum
        mid30 = next((float(r[1]) for r in rows if float(r[0]) >= ts_start + 30), None)
        return open_mid, close_mid, mid30
    except Exception as e:
        return None, None, None

print(f"\n{'Market':<52} {'Dom':<5} {'1st':<5} {'Open':<6} {'Mid30':<7} {'Close':<6} {'Mom30':<7} {'Match?'}")
print("-"*110)

correct = wrong = no_data = 0
results = []

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
    first_ts = float(fills[0].get('timestamp', 0))

    asset = "BTC" if "Bitcoin" in market else "ETH"
    db = db_map.get(asset)
    ts_start = parse_candle_start(market)
    if not ts_start or not db: continue

    open_mid, close_mid, mid30 = get_candle_data(db, ts_start, ts_start + 300)
    m_short = market[-50:]

    if open_mid is None:
        no_data += 1
        print(f"{m_short:<52} {dominant:<5} {first_outcome:<5} {'N/A':<6} {'N/A':<7} {'N/A':<6} {'N/A':<7} NO DATA")
        continue

    mom30 = (mid30 - open_mid) if mid30 else 0
    resolution = "Up" if close_mid >= 0.85 else ("Down" if close_mid <= 0.15 else "OPEN")
    
    # Check if dominant matches resolution
    match = "✓" if resolution == dominant else ("✗" if resolution != "OPEN" else "?")
    # Check if first entry matches resolution  
    first_match = "✓" if resolution == first_outcome else ("✗" if resolution != "OPEN" else "?")
    # Check if 30s momentum predicts resolution
    mom_direction = "Up" if mom30 > 0.02 else ("Down" if mom30 < -0.02 else "FLAT")
    mom_match = "✓" if mom_direction == resolution else ("✗" if resolution != "OPEN" else "?")

    if resolution == dominant: correct += 1
    elif resolution != "OPEN": wrong += 1
    else: no_data += 1

    results.append({
        'market': m_short, 'dominant': dominant, 'first': first_outcome,
        'open': open_mid, 'mid30': mid30, 'close': close_mid,
        'mom30': mom30, 'resolution': resolution,
        'dom_match': match, 'first_match': first_match, 'mom_match': mom_match
    })

    print(f"{m_short:<52} {dominant:<5} {first_outcome:<5} {open_mid:<6.3f} {str(round(mid30,3)) if mid30 else 'N/A':<7} {close_mid:<6.3f} {mom30:+.3f}   dom={match} 1st={first_match} mom={mom_match}")

total = correct + wrong
first_correct = sum(1 for r in results if r['first_match'] == '✓')
mom_correct = sum(1 for r in results if r['mom_match'] == '✓' and r['resolution'] != 'OPEN')

print(f"\n{'='*70}")
print(f"Candles analyzed: {len(results)} | No data: {no_data}")
print(f"\nDominant side matched resolution:  {correct}/{total} ({100*correct//max(total,1)}%)")
print(f"First entry matched resolution:    {first_correct}/{len(results)} ({100*first_correct//max(len(results),1)}%)")
print(f"30s momentum matched resolution:   {mom_correct}/{total} ({100*mom_correct//max(total,1)}%)")
print(f"\nConclusion:")
if total > 5:
    dom_pct = correct/total
    first_pct = first_correct/max(len(results),1)
    mom_pct = mom_correct/max(total,1)
    if dom_pct > 0.65:
        print(f"→ Dominant side signal is REAL ({dom_pct*100:.0f}% accuracy) — they bet on the winner")
    elif dom_pct < 0.45:
        print(f"→ Dominant side is CONTRARIAN ({dom_pct*100:.0f}%) — they fade the move")
    else:
        print(f"→ No dominant side signal ({dom_pct*100:.0f}%) — pure arb, direction doesn't matter")
    
    if first_pct > 0.65:
        print(f"→ First entry predicts winner ({first_pct*100:.0f}%) — they enter winning side first")
    if mom_pct > 0.65:
        print(f"→ 30s momentum predicts winner ({mom_pct*100:.0f}%) — momentum is the signal")
