import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB = "/home/opc/market_btc_5m.db"

print("Loading BTC 5M data...")
conn = sqlite3.connect(DB)

# Get last 24 hours of data
max_ts = conn.execute("SELECT MAX(unix_time) FROM polymarket_odds").fetchone()[0]
min_ts = max_ts - 86400  # 24 hours

rows = conn.execute("""
    SELECT unix_time, market_id, outcome, bid, ask, mid
    FROM polymarket_odds
    WHERE unix_time >= ?
    ORDER BY unix_time ASC
""", (min_ts,)).fetchall()
conn.close()

print(f"Rows loaded: {len(rows):,}")

# Group by market_id (each = one candle)
candles = defaultdict(lambda: {"Up": [], "Down": []})
for unix_time, market_id, outcome, bid, ask, mid in rows:
    if outcome in ("Up", "Down") and ask and ask > 0:
        candles[market_id][outcome].append((unix_time, ask, mid))

print(f"Unique candles: {len(candles)}")

# Thresholds to test
THRESHOLDS = [0.35, 0.40, 0.45, 0.50]

print(f"\n--- How often does BOTH sides dip below threshold at ANY point in the candle? ---\n")

for thresh in THRESHOLDS:
    both_dipped = 0
    up_only = 0
    down_only = 0
    neither = 0
    
    combined_edges = []
    time_gaps = []  # seconds between first Up dip and first Down dip
    
    for market_id, sides in candles.items():
        up_rows = sides["Up"]
        down_rows = sides["Down"]
        
        if not up_rows or not down_rows:
            continue
        
        # Find minimum ask on each side during candle
        up_min_ask = min(ask for _, ask, _ in up_rows)
        down_min_ask = min(ask for _, ask, _ in down_rows)
        
        up_dipped = up_min_ask < thresh
        down_dipped = down_min_ask < thresh
        
        if up_dipped and down_dipped:
            both_dipped += 1
            
            # Find when each side first dipped below threshold
            up_first_dip = min(t for t, ask, _ in up_rows if ask < thresh)
            down_first_dip = min(t for t, ask, _ in down_rows if ask < thresh)
            gap = abs(up_first_dip - down_first_dip)
            time_gaps.append(gap)
            
            # What would combined cost be at best prices?
            combined = up_min_ask + down_min_ask
            combined_edges.append(1.0 - combined)
            
        elif up_dipped:
            up_only += 1
        elif down_dipped:
            down_only += 1
        else:
            neither += 1
    
    total = both_dipped + up_only + down_only + neither
    if total == 0:
        continue
        
    print(f"Threshold: {thresh:.2f}")
    print(f"  Both sides dipped: {both_dipped}/{total} ({100*both_dipped//total}%)")
    print(f"  Up only:           {up_only}/{total} ({100*up_only//total}%)")
    print(f"  Down only:         {down_only}/{total} ({100*down_only//total}%)")
    print(f"  Neither:           {neither}/{total} ({100*neither//total}%)")
    
    if combined_edges:
        avg_edge = sum(combined_edges)/len(combined_edges)
        print(f"  Avg combined edge when both dip: {avg_edge*100:.2f}¢")
        print(f"  Max edge: {max(combined_edges)*100:.2f}¢")
    
    if time_gaps:
        avg_gap = sum(time_gaps)/len(time_gaps)
        under_30s = sum(1 for g in time_gaps if g < 30)
        under_60s = sum(1 for g in time_gaps if g < 60)
        over_120s = sum(1 for g in time_gaps if g > 120)
        print(f"  Avg time between Up/Down dips: {avg_gap:.0f}s")
        print(f"  Gap under 30s: {under_30s} ({100*under_30s//(len(time_gaps)+1)}%)")
        print(f"  Gap under 60s: {under_60s} ({100*under_60s//(len(time_gaps)+1)}%)")
        print(f"  Gap over 120s: {over_120s} ({100*over_120s//(len(time_gaps)+1)}%)")
    print()

# Deeper dive at 0.45 threshold
print(f"\n--- Detailed breakdown at 0.45 threshold ---")
thresh = 0.45
profitable_candles = []

for market_id, sides in candles.items():
    up_rows = sides["Up"]
    down_rows = sides["Down"]
    if not up_rows or not down_rows:
        continue
    
    up_min = min(ask for _, ask, _ in up_rows)
    down_min = min(ask for _, ask, _ in down_rows)
    
    if up_min < thresh and down_min < thresh:
        combined = up_min + down_min
        edge = 1.0 - combined
        
        up_first = min(t for t, ask, _ in up_rows if ask < thresh)
        down_first = min(t for t, ask, _ in down_rows if ask < thresh)
        gap = down_first - up_first  # positive = up dipped first
        
        dt = datetime.fromtimestamp(max_ts - 86400 + 1, tz=timezone.utc)
        
        profitable_candles.append({
            "market": market_id,
            "up_min": up_min,
            "down_min": down_min,
            "combined": combined,
            "edge": edge,
            "gap": gap,
        })

profitable_candles.sort(key=lambda x: x["edge"], reverse=True)

print(f"Total profitable candles: {len(profitable_candles)}")
print(f"\nTop 15 by edge:")
print(f"{'Up min':<10} {'Down min':<10} {'Combined':<10} {'Edge':<8} {'Gap (s)':<10}")
print("-"*50)
for c in profitable_candles[:15]:
    gap_str = f"{c['gap']:+.0f}s"
    print(f"{c['up_min']:<10.3f} {c['down_min']:<10.3f} {c['combined']:<10.3f} {c['edge']*100:<8.2f}¢ {gap_str}")
