import sqlite3
from datetime import datetime, timezone
import csv, sys

DB_DIR = '/root'

# 14:45-14:50 UTC (10:45-10:50 AM ET) candle for each asset
WINDOWS = [
    ('ETH', 'eth', datetime(2026,4,21,14,45,tzinfo=timezone.utc), datetime(2026,4,21,14,50,tzinfo=timezone.utc)),
    ('XRP', 'xrp', datetime(2026,4,21,14,45,tzinfo=timezone.utc), datetime(2026,4,21,14,50,tzinfo=timezone.utc)),
]

# Mark our entries
ENTRIES = {
    'ETH': {'ts': datetime(2026,4,21,14,45,17,143000,tzinfo=timezone.utc), 'side':'Down', 'ask':0.550, 'fill_price':2.0/3.125},
    'XRP': {'ts': datetime(2026,4,21,14,45,17,466000,tzinfo=timezone.utc), 'side':'Down', 'ask':0.540, 'fill_price':2.0/3.448},
}

def backup(src):
    import time as t
    dst_path = f'/tmp/snap_{int(t.time())}_{src.split("/")[-1]}'
    a = sqlite3.connect(src, timeout=30)
    b = sqlite3.connect(dst_path)
    a.backup(b)
    a.close(); b.close()
    return dst_path

for label, slug, cs, ce in WINDOWS:
    db_path = f'{DB_DIR}/market_{slug}_5m.db'
    snap = backup(db_path)
    c = sqlite3.connect(snap)
    rows = c.execute(
        "SELECT unix_time, outcome, bid, ask FROM polymarket_odds "
        "WHERE unix_time >= ? AND unix_time < ? AND outcome IN ('Up','Down') "
        "ORDER BY unix_time, outcome",
        (cs.timestamp(), ce.timestamp())).fetchall()
    c.close()

    # Pivot: one row per unique timestamp with both Up and Down in columns
    pivot = {}
    for ut, o, b, a in rows:
        key = float(ut)
        if key not in pivot:
            pivot[key] = {'Up_bid':None,'Up_ask':None,'Down_bid':None,'Down_ask':None}
        pivot[key][f'{o}_bid'] = float(b)
        pivot[key][f'{o}_ask'] = float(a)

    entry = ENTRIES.get(label)
    entry_unix = entry['ts'].timestamp() if entry else None

    print(f'\n========== {label}  candle {cs.strftime("%H:%M")}-{ce.strftime("%H:%M")} UTC ==========')
    print(f'Total ticks captured: {len(pivot)}')
    if entry:
        print(f'OUR ENTRY: {entry["ts"].strftime("%H:%M:%S.%f")[:-3]} UTC, {entry["side"]} @ask=${entry["ask"]:.3f}, real fill=${entry["fill_price"]:.3f}')
    print()
    print(f'{"time (UTC)":<15} {"up_bid":>7} {"up_ask":>7} {"up_mid":>7} {"dn_bid":>7} {"dn_ask":>7} {"dn_mid":>7}  marker')
    print('-'*95)
    marked = False
    for k in sorted(pivot.keys()):
        v = pivot[k]
        def f(x): return f'{x:.3f}' if x is not None else '  —  '
        def m(b,a):
            if b is None or a is None: return '  —  '
            return f'{(b+a)/2:.3f}'
        ts = datetime.fromtimestamp(k, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]
        marker = ''
        if entry_unix is not None and not marked and k >= entry_unix:
            marker = f'  ← OUR {entry["side"]} ENTRY (log ask {entry["ask"]:.3f}, REAL FILL {entry["fill_price"]:.3f})'
            marked = True
        print(f'{ts:<15} {f(v["Up_bid"]):>7} {f(v["Up_ask"]):>7} {m(v["Up_bid"],v["Up_ask"]):>7} {f(v["Down_bid"]):>7} {f(v["Down_ask"]):>7} {m(v["Down_bid"],v["Down_ask"]):>7}{marker}')
