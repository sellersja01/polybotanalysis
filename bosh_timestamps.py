import requests
import time
import csv
from collections import defaultdict

WALLET = "0x29bc82f761749e67fa00d62896bc6855097b683c"
POLYGONSCAN_API = "https://api.polygonscan.com/api"

print(f"Fetching trades for {WALLET}")
trades = []
for offset in range(0, 3600, 100):
    url = f"https://data-api.polymarket.com/activity?user={WALLET}&limit=100&offset={offset}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if not data:
            print(f"  No more data at offset {offset}")
            break
        trades.extend(data)
        print(f"  offset {offset} -> {len(data)} trades (total: {len(trades)})")
        time.sleep(0.25)
    except Exception as e:
        print(f"  Error: {e}")
        break

print(f"\nTotal trades: {len(trades)}")

# Parse each trade using slug for candle ID
parsed = []
for t in trades:
    if not isinstance(t, dict):
        continue
    if t.get('type') == 'REDEEM':
        continue
    
    slug = t.get('slug', t.get('eventSlug', ''))
    # slug format: btc-updown-5m-1774059900
    parts = slug.split('-')
    candle_ts = None
    asset = None
    tf = None
    try:
        candle_ts = int(parts[-1])
        asset = parts[0].upper()  # BTC or ETH
        tf = parts[2]  # 5m or 15m
    except:
        pass
    
    parsed.append({
        'timestamp': t.get('timestamp', 0),
        'tx_hash': t.get('transactionHash', ''),
        'outcome': t.get('outcome', ''),
        'side': t.get('side', ''),
        'price': float(t.get('price', 0) or 0),
        'size': float(t.get('size', 0) or 0),
        'usdc': float(t.get('usdcSize', 0) or 0),
        'asset': asset,
        'tf': tf,
        'candle_ts': candle_ts,
        'market': t.get('title', ''),
        'slug': slug,
    })

# Get block timestamps for unique tx hashes
# Using public Polygon RPC (no API key needed)
print(f"\nFetching block timestamps for unique transactions...")
tx_hashes = list(set(t['tx_hash'] for t in parsed if t['tx_hash']))
print(f"Unique tx hashes: {len(tx_hashes)}")

# Sample first 20 to check timing precision
block_times = {}
sample = tx_hashes[:50]
for i, tx_hash in enumerate(sample):
    try:
        # Use public Polygon RPC
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
            "id": 1
        }
        r = requests.post("https://polygon-rpc.com", json=payload, timeout=8)
        data = r.json()
        if data.get('result') and data['result'].get('blockNumber'):
            block_num = int(data['result']['blockNumber'], 16)
            
            # Get block timestamp
            payload2 = {
                "jsonrpc": "2.0",
                "method": "eth_getBlockByNumber",
                "params": [hex(block_num), False],
                "id": 1
            }
            r2 = requests.post("https://polygon-rpc.com", json=payload2, timeout=8)
            data2 = r2.json()
            if data2.get('result'):
                block_ts = int(data2['result']['timestamp'], 16)
                block_times[tx_hash] = block_ts
                if i < 5:
                    print(f"  {tx_hash[:20]}... block={block_num} ts={block_ts}")
        
        if (i+1) % 10 == 0:
            print(f"  Processed {i+1}/{len(sample)}...")
        time.sleep(0.1)
    except Exception as e:
        pass

print(f"Got timestamps for {len(block_times)} transactions")

# Add block timestamps to trades
for t in parsed:
    t['block_ts'] = block_times.get(t['tx_hash'], t['timestamp'])

# Group by candle and analyze timing
print(f"\n{'='*80}")
print(f"PER-CANDLE ANALYSIS WITH BLOCK TIMESTAMPS")
print(f"{'='*80}")

candles = defaultdict(list)
for t in parsed:
    if t['candle_ts'] and t['asset'] in ['BTC', 'ETH']:
        key = (t['asset'], t['tf'], t['candle_ts'])
        candles[key].append(t)

print(f"\n{'Candle':<45} {'#':<4} {'1st_side':<9} {'1st_ts':<12} {'gap_s':<7} {'up_$':<8} {'dn_$':<8} {'dom'}")
print("-"*110)

for key in sorted(candles.keys(), key=lambda x: x[2]):
    asset, tf, candle_ts = key
    fills = candles[key]
    fills.sort(key=lambda x: x['block_ts'])
    
    up = [f for f in fills if 'up' in f['outcome'].lower()]
    down = [f for f in fills if 'down' in f['outcome'].lower()]
    
    if not up or not down:
        continue
    
    first_up_ts = min(f['block_ts'] for f in up)
    first_down_ts = min(f['block_ts'] for f in down)
    
    first_side = "UP" if first_up_ts <= first_down_ts else "DOWN"
    gap = abs(first_up_ts - first_down_ts)
    
    # seconds into candle when first entry happened
    first_ts = min(first_up_ts, first_down_ts)
    candle_offset = first_ts - candle_ts
    
    up_usdc = sum(f['usdc'] for f in up)
    down_usdc = sum(f['usdc'] for f in down)
    dominant = "UP" if up_usdc > down_usdc else "DOWN"
    
    from datetime import datetime
    candle_time = datetime.utcfromtimestamp(candle_ts).strftime('%H:%M')
    label = f"{asset} {tf} {candle_time}"
    
    print(f"{label:<45} {len(fills):<4} {first_side:<9} +{candle_offset:<11}s {gap:<7}s ${up_usdc:<7.0f} ${down_usdc:<7.0f} {dominant}")

# Save full data
fname = "bosh_with_timestamps.csv"
with open(fname, 'w', newline='') as f:
    keys = ['block_ts', 'timestamp', 'asset', 'tf', 'candle_ts', 'outcome', 'price', 'size', 'usdc', 'tx_hash', 'market']
    w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
    w.writeheader()
    # Sort by block timestamp
    parsed.sort(key=lambda x: x['block_ts'])
    w.writerows(parsed)

print(f"\nSaved to {fname}")
print(f"\nKey stats:")
gaps = []
offsets = []
for key, fills in candles.items():
    fills.sort(key=lambda x: x['block_ts'])
    up = [f for f in fills if 'up' in f['outcome'].lower()]
    down = [f for f in fills if 'down' in f['outcome'].lower()]
    if not up or not down: continue
    first_up_ts = min(f['block_ts'] for f in up)
    first_down_ts = min(f['block_ts'] for f in down)
    gap = abs(first_up_ts - first_down_ts)
    first_ts = min(first_up_ts, first_down_ts)
    candle_offset = first_ts - key[2]
    gaps.append(gap)
    offsets.append(candle_offset)

if gaps:
    print(f"Avg gap between Up/Down first entry: {sum(gaps)/len(gaps):.1f}s")
    print(f"Median gap: {sorted(gaps)[len(gaps)//2]}s")
    print(f"Under 2s: {sum(1 for g in gaps if g <= 2)} ({100*sum(1 for g in gaps if g <= 2)//len(gaps)}%)")
    print(f"Under 5s: {sum(1 for g in gaps if g <= 5)} ({100*sum(1 for g in gaps if g <= 5)//len(gaps)}%)")
    print(f"\nAvg entry offset from candle open: {sum(offsets)/len(offsets):.1f}s")
    print(f"Under 30s: {sum(1 for o in offsets if o <= 30)} ({100*sum(1 for o in offsets if o <= 30)//len(offsets)}%)")
    print(f"Under 60s: {sum(1 for o in offsets if o <= 60)} ({100*sum(1 for o in offsets if o <= 60)//len(offsets)}%)")
