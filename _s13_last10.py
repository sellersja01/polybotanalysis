import re
from datetime import datetime, timezone, timedelta

LOG = '/root/paper_s13.log'
ASSETS = ['BTC','ETH','SOL','XRP']

entry_re = re.compile(r'\[(\d+):(\d+):(\d+)\] ENTRY \[(\w+)\] (\w+) @([\d.]+) mid=([\d.]+) mv=([+-][\d.]+)%')
res_re = re.compile(r'\[(\d+):(\d+):(\d+)\] ([WL]) \[(\w+)\] (\w+) @([\d.]+) pnl=\$([+-][\d.]+)')

entries = {a: [] for a in ASSETS}

with open(LOG) as f:
    lines = f.readlines()

now = datetime.now(timezone.utc)
cur_date = now.date()
parsed = []
prev_h = None
for line in reversed(lines):
    m = re.search(r'\[(\d+):(\d+):(\d+)\]', line)
    if not m: continue
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if prev_h is not None and h > prev_h:
        cur_date = cur_date - timedelta(days=1)
    prev_h = h
    dt = datetime.combine(cur_date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=h, minute=mi, second=s)
    parsed.append((dt, line))
parsed.reverse()

for dt, line in parsed:
    m = entry_re.search(line)
    if m:
        a = m.group(4); side = m.group(5); price = float(m.group(6))
        if a in entries:
            entries[a].append({'dt': dt, 'side': side, 'entry_price': price, 'outcome': None, 'pnl': None})
        continue
    m = res_re.search(line)
    if m:
        a = m.group(5); side = m.group(6); wl = m.group(4); entry_p = float(m.group(7)); pnl = float(m.group(8))
        if a in entries:
            for e in reversed(entries[a]):
                if e['outcome'] is None and e['side'] == side and abs(e['entry_price'] - entry_p) < 0.001:
                    e['outcome'] = wl; e['pnl'] = pnl
                    break

for a in ASSETS:
    print(f'\n==== {a} - last 10 trades ====')
    last10 = entries[a][-10:]
    if not last10:
        print('  (no entries)')
        continue
    print(f'{"TIME (UTC)":<17} {"SIDE":<5} {"UP":>6} {"DN":>6} {"OUT":>4} {"PNL":>9}')
    for e in last10:
        if e['side'] == 'Up':
            up_p, dn_p = e['entry_price'], round(1 - e['entry_price'], 3)
        else:
            dn_p, up_p = e['entry_price'], round(1 - e['entry_price'], 3)
        out = e['outcome'] or 'open'
        pnl = f'${e["pnl"]:+.2f}' if e['pnl'] is not None else '-'
        print(f'{e["dt"].strftime("%m-%d %H:%M:%S"):<17} {e["side"]:<5} {up_p:>6.3f} {dn_p:>6.3f} {out:>4} {pnl:>9}')
