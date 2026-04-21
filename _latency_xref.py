"""
Cross-reference live_s13 trades with the collector DB.
For each live fill captured in the bot log, pull collector tick history in
a +/- 3 second window around the trade. Tells us:
- What the book looked like at ENTRY decision
- What the book looked like at FILL confirmation
- How fast did the market move between the two?
"""
import re, sqlite3
from datetime import datetime, timezone, timedelta

LOG = '/root/live_s13.log'
DB_DIR = '/root'

entry_re  = re.compile(r'\[(\d+):(\d+):(\d+)(?:\.(\d+))?\] ENTRY\[LIVE\] \[(\w+)\] (\w+) @([\d.]+) mid=([\d.]+) mv=([+-][\d.]+)%')
filled_re = re.compile(r'\[(\d+):(\d+):(\d+)(?:\.(\d+))?\] FILLED \[(\w+)\] (\w+) shares=([\d.]+) spent=\$([\d.]+)(?: lat: pre=(\d+)ms sign=(\d+)ms post=(\d+)ms TOTAL=(\d+)ms)?')

def to_ts(h, m, s, frac, date=None):
    if date is None: date = datetime.now(timezone.utc).date()
    micro = int((frac or '0').ljust(6, '0')[:6]) if frac else 0
    dt = datetime.combine(date, datetime.min.time(), tzinfo=timezone.utc)
    return dt.replace(hour=int(h), minute=int(m), second=int(s), microsecond=micro)

def snapshot_db(asset):
    src = sqlite3.connect(f'{DB_DIR}/market_{asset.lower()}_5m.db', timeout=30)
    dst = sqlite3.connect(f'/tmp/xref_{asset.lower()}.db')
    src.backup(dst); src.close(); dst.close()
    return f'/tmp/xref_{asset.lower()}.db'

with open(LOG) as f:
    lines = f.readlines()

# Walk backward to assign dates
now = datetime.now(timezone.utc)
cur_date = now.date()
parsed = []
prev_h = None
for line in reversed(lines):
    m = re.search(r'\[(\d+):(\d+):(\d+)', line)
    if not m: continue
    h = int(m.group(1))
    if prev_h is not None and h > prev_h:
        cur_date = cur_date - timedelta(days=1)
    prev_h = h
    parsed.append((cur_date, line))
parsed.reverse()

entries = []
fills = []
for date, line in parsed:
    em = entry_re.search(line)
    if em:
        h,mi,s,fr = em.group(1), em.group(2), em.group(3), em.group(4)
        dt = to_ts(h,mi,s,fr,date)
        entries.append({'dt': dt, 'asset': em.group(5), 'side': em.group(6),
                        'log_ask': float(em.group(7)), 'mid': float(em.group(8)),
                        'mv': float(em.group(9))})
        continue
    fm = filled_re.search(line)
    if fm:
        h,mi,s,fr = fm.group(1), fm.group(2), fm.group(3), fm.group(4)
        dt = to_ts(h,mi,s,fr,date)
        fills.append({'dt': dt, 'asset': fm.group(5), 'side': fm.group(6),
                      'shares': float(fm.group(7)), 'spent': float(fm.group(8)),
                      'pre_ms': int(fm.group(9)) if fm.group(9) else None,
                      'sign_ms': int(fm.group(10)) if fm.group(10) else None,
                      'post_ms': int(fm.group(11)) if fm.group(11) else None,
                      'total_ms': int(fm.group(12)) if fm.group(12) else None})

# Pair ENTRY with FILLED (same asset, same side, fill within 3s)
pairs = []
used_fills = set()
for e in entries:
    best = None
    for i, f in enumerate(fills):
        if i in used_fills: continue
        if f['asset'] != e['asset'] or f['side'] != e['side']: continue
        delta = (f['dt'] - e['dt']).total_seconds()
        if 0 <= delta <= 3:
            if best is None or delta < (fills[best]['dt'] - e['dt']).total_seconds():
                best = i
    if best is not None:
        pairs.append((e, fills[best]))
        used_fills.add(best)

# Only keep the 5 most recent that have latency data
recent = [p for p in pairs if p[1]['total_ms'] is not None][-5:]
print(f'Found {len(pairs)} ENTRY/FILLED pairs total. Analyzing the {len(recent)} most recent with latency data.\n')

for e, f in recent:
    asset = e['asset']
    db = snapshot_db(asset)
    c = sqlite3.connect(db)
    # +/- 3 seconds around entry
    lo = e['dt'].timestamp() - 3
    hi = f['dt'].timestamp() + 1
    rows = c.execute(
        "SELECT unix_time, outcome, bid, ask FROM polymarket_odds WHERE unix_time >= ? AND unix_time < ? ORDER BY unix_time",
        (lo, hi)).fetchall()
    c.close()
    # Pivot by timestamp
    pivot = {}
    for ut, o, b, a in rows:
        t = float(ut)
        if t not in pivot:
            pivot[t] = {'Up':(None,None),'Down':(None,None)}
        pivot[t][o] = (float(b), float(a))
    ordered = sorted(pivot.keys())

    print(f'╔══════════════════════════════════════════════════════════════════════════════════╗')
    print(f'║  {asset} {e["side"]}  ENTRY {e["dt"].strftime("%H:%M:%S.%f")[:-3]}  FILL {f["dt"].strftime("%H:%M:%S.%f")[:-3]}')
    print(f'║  log ask={e["log_ask"]:.3f}  real fill=${f["spent"]/f["shares"]:.3f}/sh  lat pre/sign/post/total={f["pre_ms"]}/{f["sign_ms"]}/{f["post_ms"]}/{f["total_ms"]}ms')
    print(f'╚══════════════════════════════════════════════════════════════════════════════════╝')
    print(f'{"tick time":<13} {"delta":>8} {"up_bid":>7} {"up_ask":>7} {"dn_bid":>7} {"dn_ask":>7}  marker')
    entry_ts = e['dt'].timestamp()
    fill_ts  = f['dt'].timestamp()
    marked_e = marked_f = False
    for t in ordered:
        d_entry = (t - entry_ts) * 1000
        u = pivot[t]['Up']; dn = pivot[t]['Down']
        def fmt(x): return f'{x:.3f}' if x is not None else '  —  '
        marker = ''
        if not marked_e and t >= entry_ts:
            marker = '  ← ENTRY'; marked_e = True
        elif not marked_f and t >= fill_ts:
            marker = '  ← FILL';  marked_f = True
        print(f'{datetime.fromtimestamp(t,tz=timezone.utc).strftime("%H:%M:%S.%f")[3:-3]:<13} {d_entry:+6.0f}ms  {fmt(u[0])} {fmt(u[1])} {fmt(dn[0])} {fmt(dn[1])}{marker}')
    if not ordered:
        print('  (no collector data in window)')
    print()
