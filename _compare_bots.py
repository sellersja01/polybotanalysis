import re
from datetime import datetime, timezone, timedelta

FILES = {
    'live':     '/root/live_s13.log',
    'paper':    '/root/paper_s13.log',
    'paperapi': '/root/paper_s13api.log',
}

# Only compare trades from after live-s13 NEW code restart (19:50 UTC today)
CUTOFF = datetime(2026,4,20,19,50,tzinfo=timezone.utc)

entry_re_live  = re.compile(r'\[(\d+):(\d+):(\d+)\] ENTRY\[DRY\] \[(\w+)\] (\w+) @([\d.]+)')
entry_re_paper = re.compile(r'\[(\d+):(\d+):(\d+)\] ENTRY \[(\w+)\] (\w+) @([\d.]+)')

def parse(fp, is_live):
    with open(fp) as f:
        lines = f.readlines()
    now = datetime.now(timezone.utc)
    cur_date = now.date()
    prev_h = None
    out = []
    for line in reversed(lines):
        m = re.search(r'\[(\d+):(\d+):(\d+)\]', line)
        if not m: continue
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if prev_h is not None and h > prev_h:
            cur_date = cur_date - timedelta(days=1)
        prev_h = h
        dt = datetime.combine(cur_date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=h, minute=mi, second=s)
        out.append((dt, line))
    out.reverse()
    entries = []
    r = entry_re_live if is_live else entry_re_paper
    for dt, line in out:
        m = r.search(line)
        if m and dt >= CUTOFF:
            asset = m.group(4); side = m.group(5); price = float(m.group(6))
            cs = int(dt.timestamp()) // 300 * 300
            entries.append({'dt': dt, 'cs': cs, 'asset': asset, 'side': side, 'price': price})
    return entries

live      = parse(FILES['live'], True)
paper     = parse(FILES['paper'], False)
paperapi  = parse(FILES['paperapi'], False)

print(f'Entries since {CUTOFF.strftime("%H:%M UTC")}:')
print(f'  live:     {len(live)}')
print(f'  paper:    {len(paper)}')
print(f'  paperapi: {len(paperapi)}')

# Index by (candle_start, asset)
def index(L):
    d = {}
    for e in L:
        d[(e['cs'], e['asset'])] = e
    return d

li, pa, pai = index(live), index(paper), index(paperapi)
all_keys = set(li) | set(pa) | set(pai)

print(f'\n{"CANDLE(UTC)":<16} {"ASSET":<5} | {"LIVE":<15} | {"PAPER":<15} | {"PAPERAPI":<15}')
print('-' * 80)
for key in sorted(all_keys):
    cs, asset = key
    def fmt(e):
        if not e: return '—'
        return f'{e["side"]:<4} @{e["price"]:.3f} ({e["dt"].strftime("%H:%M:%S")})'
    candle_str = datetime.fromtimestamp(cs, tz=timezone.utc).strftime('%m-%d %H:%M')
    print(f'{candle_str:<16} {asset:<5} | {fmt(li.get(key)):<15} | {fmt(pa.get(key)):<15} | {fmt(pai.get(key)):<15}')

# Summary: how many candles did each pair agree on direction?
both_live_paper = [k for k in all_keys if k in li and k in pa]
same_dir_lp = sum(1 for k in both_live_paper if li[k]['side'] == pa[k]['side'])
both_live_pai = [k for k in all_keys if k in li and k in pai]
same_dir_la = sum(1 for k in both_live_pai if li[k]['side'] == pai[k]['side'])
both_pa_pai = [k for k in all_keys if k in pa and k in pai]
same_dir_pp = sum(1 for k in both_pa_pai if pa[k]['side'] == pai[k]['side'])

print(f'\n=== Pairwise agreement ===')
print(f'live vs paper     : {len(both_live_paper)} overlapping candles, {same_dir_lp} same direction')
print(f'live vs paperapi  : {len(both_live_pai)} overlapping candles, {same_dir_la} same direction')
print(f'paper vs paperapi : {len(both_pa_pai)} overlapping candles, {same_dir_pp} same direction')

# Misses: candles where paper/paperapi fired but live didn't
missed_by_live = [k for k in all_keys if k in pa and k not in li]
missed_by_paper = [k for k in all_keys if k in li and k not in pa]
print(f'\nCandles paper entered but live did NOT: {len(missed_by_live)}')
print(f'Candles live entered but paper did NOT: {len(missed_by_paper)}')
