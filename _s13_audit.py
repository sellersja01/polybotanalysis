import re, asyncio, aiohttp, json
from datetime import datetime, timezone, timedelta

LOG = '/root/paper_s13.log'
ASSETS = ['BTC','ETH','SOL','XRP']
SLUGS  = {'BTC':'btc','ETH':'eth','SOL':'sol','XRP':'xrp'}

entry_re = re.compile(r'\[(\d+):(\d+):(\d+)\] ENTRY \[(\w+)\] (\w+) @([\d.]+)')
res_re   = re.compile(r'\[(\d+):(\d+):(\d+)\] ([WL]) \[(\w+)\] (\w+) @([\d.]+) pnl=\$([+-][\d.]+)')

with open(LOG) as f:
    lines = f.readlines()

# Walk backward to assign dates (VPS time == UTC)
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

# Collect trades: pair ENTRY with its W/L outcome
trades = []   # list of dicts with asset, cs (candle start), side, bot_result
open_trades = {}  # (asset, side, entry_price) -> trade ref

for dt, line in parsed:
    m = entry_re.search(line)
    if m:
        a = m.group(4); side = m.group(5); price = float(m.group(6))
        cs = int(dt.timestamp()) // 300 * 300  # floor to 5-min candle start
        t = {'asset': a, 'cs': cs, 'side': side, 'entry_price': price, 'entry_dt': dt,
             'bot_result': None, 'bot_pnl': None}
        trades.append(t)
        key = (a, side, round(price,3))
        open_trades[key] = t
        continue
    m = res_re.search(line)
    if m:
        a = m.group(5); side = m.group(6); wl = m.group(4); p = round(float(m.group(7)),3); pnl = float(m.group(8))
        key = (a, side, p)
        if key in open_trades:
            open_trades[key]['bot_result'] = wl
            open_trades[key]['bot_pnl'] = pnl
            del open_trades[key]

trades = [t for t in trades if t['bot_result'] is not None]
print(f'Total resolved trades to audit: {len(trades)}')

async def get_api_winner(session, slug, cs):
    url = f'https://gamma-api.polymarket.com/events?slug={slug}-updown-5m-{cs}'
    try:
        async with session.get(url, timeout=15) as r:
            d = await r.json()
        if not d: return None
        m = d[0].get('markets', [{}])[0]
        if not m.get('closed'): return None
        prices = json.loads(m.get('outcomePrices', '[]'))
        outcomes = json.loads(m.get('outcomes', '[]'))
        if len(prices) < 2 or len(outcomes) < 2: return None
        for i, p in enumerate(prices):
            if float(p) >= 0.99:
                return outcomes[i]
    except Exception as e:
        return None
    return None

async def audit():
    conn = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=conn) as session:
        sem = asyncio.Semaphore(10)
        async def do(t):
            async with sem:
                slug = SLUGS[t['asset']]
                w = await get_api_winner(session, slug, t['cs'])
                t['api_winner'] = w
        await asyncio.gather(*[do(t) for t in trades])

asyncio.run(audit())

# Analyze
api_resolved = [t for t in trades if t['api_winner'] is not None]
api_unresolved = [t for t in trades if t['api_winner'] is None]

# Per asset
print(f'\n{"Asset":<6} {"N":>5} {"api_OK":>7} {"match":>7} {"mismatch":>9} {"no_api":>7} {"bot_W":>6} {"true_W":>7} {"bot_PnL":>10} {"true_PnL":>10}')
totals = {'n':0,'match':0,'mismatch':0,'no_api':0,'bot_W':0,'true_W':0,'bot_PnL':0.0,'true_PnL':0.0}
for a in ASSETS:
    ats = [t for t in trades if t['asset']==a]
    if not ats:
        print(f'{a:<6} {"0":>5}')
        continue
    match = sum(1 for t in ats if t['api_winner'] is not None and t['api_winner'] == ('Up' if t['bot_result']=='W' and t['side']=='Up' else ('Down' if t['bot_result']=='W' and t['side']=='Down' else ('Up' if t['bot_result']=='L' and t['side']=='Down' else 'Down'))))
    # Simpler: bot thought it won if api_winner==side; bot correct if (bot_result=='W') == (api_winner==side)
    match = 0; mismatch = 0; no_api = 0; bot_W = 0; true_W = 0
    bot_pnl = 0.0; true_pnl = 0.0
    for t in ats:
        bot_pnl += t['bot_pnl']
        if t['api_winner'] is None:
            no_api += 1
            true_pnl += t['bot_pnl']  # can't verify — keep as-is
            continue
        bot_said_win = (t['bot_result'] == 'W')
        actually_won = (t['api_winner'] == t['side'])
        if bot_said_win == actually_won:
            match += 1
            true_pnl += t['bot_pnl']
        else:
            mismatch += 1
            # Flip the PnL: bot said +X but should be -X (or vice versa)
            true_pnl += -t['bot_pnl']
        if bot_said_win: bot_W += 1
        if actually_won: true_W += 1
    n = len(ats)
    print(f'{a:<6} {n:>5} {(match+mismatch):>7} {match:>7} {mismatch:>9} {no_api:>7} {bot_W:>6} {true_W:>7} {bot_pnl:>10.2f} {true_pnl:>10.2f}')
    totals['n'] += n
    totals['match'] += match
    totals['mismatch'] += mismatch
    totals['no_api'] += no_api
    totals['bot_W'] += bot_W
    totals['true_W'] += true_W
    totals['bot_PnL'] += bot_pnl
    totals['true_PnL'] += true_pnl

print(f'{"TOTAL":<6} {totals["n"]:>5} {(totals["match"]+totals["mismatch"]):>7} {totals["match"]:>7} {totals["mismatch"]:>9} {totals["no_api"]:>7} {totals["bot_W"]:>6} {totals["true_W"]:>7} {totals["bot_PnL"]:>10.2f} {totals["true_PnL"]:>10.2f}')

if totals['match']+totals['mismatch'] > 0:
    pct = 100.0 * totals['match'] / (totals['match']+totals['mismatch'])
    print(f'\nBot correctly classified {totals["match"]}/{totals["match"]+totals["mismatch"]} = {pct:.1f}% of API-verifiable trades')
    print(f'Reported PnL:  ${totals["bot_PnL"]:+.2f}')
    print(f'True PnL:      ${totals["true_PnL"]:+.2f}')
    print(f'Inflation:     ${totals["bot_PnL"]-totals["true_PnL"]:+.2f}')
