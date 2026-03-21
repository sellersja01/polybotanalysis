import csv
from datetime import datetime, timezone

with open('bosh_with_timestamps.csv') as f:
    rows = list(csv.DictReader(f))

for row in rows:
    try:
        ts = float(row['block_ts'])
        row['block_ts'] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except: pass
    try:
        ts = float(row['timestamp'])
        row['timestamp'] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except: pass
    try:
        ts = float(row['candle_ts'])
        row['candle_ts'] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except: pass

with open('bosh_readable.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)

print(f"Saved to bosh_readable.csv ({len(rows)} rows)")
