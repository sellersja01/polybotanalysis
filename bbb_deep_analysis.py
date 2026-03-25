"""
BoshBashBish Deep Analysis
- Cross-references his win/loss candles with actual odds movement from our DB
- Looks for patterns in orderbook/price movement that predict his losing candles
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
import re

# ── Load BBB trades ───────────────────────────────────────────────────────────
conn = sqlite3.connect('/home/opc/wallet_trades.db')
rows = conn.execute("""
    SELECT time_utc, timestamp, side, outcome, price, size, market
    FROM trades
    WHERE wallet_name='boshbashbish'
      AND side='BUY' AND price > 0 AND size > 0
    ORDER BY timestamp ASC
""").fetchall()
conn.close()

candles = defaultdict(lambda: {'Up': [], 'Down': [], 'market_type': ''})
for time_utc, ts, side, outcome, price, size, market in rows:
    if outcome in ('Up', 'Down') and market:
        mtype = 'btc' if 'Bitcoin' in market else 'eth'
        candles[market]['market_type'] = mtype
        candles[market][outcome].append((price, size, int(ts)))

# Build result per candle
bbb_candles = []
for market, data in candles.items():
    up = data['Up']
    dn = data['Down']
    if not up or not dn:
        continue
    up_sh  = sum(s for p,s,t in up)
    dn_sh  = sum(s for p,s,t in dn)
    avg_up = sum(p*s for p,s,t in up) / up_sh
    avg_dn = sum(p*s for p,s,t in dn) / dn_sh
    combined = avg_up + avg_dn
    all_ts = sorted([t for p,s,t in up] + [t for p,s,t in dn])
    bbb_candles.append({
        'market': market,
        'mtype': data['market_type'],
        'combined': combined,
        'win': combined < 1.0,
        'avg_up': avg_up, 'avg_dn': avg_dn,
        'up_sh': up_sh, 'dn_sh': dn_sh,
        'n_trades': len(up)+len(dn),
        'first_ts': all_ts[0],
        'last_ts': all_ts[-1],
        'all_up': up, 'all_dn': dn,
    })

print(f"Candles with both sides: {len(bbb_candles)}")
print(f"  Win (combined<1): {sum(1 for c in bbb_candles if c['win'])}")
print(f"  Loss (combined>=1): {sum(1 for c in bbb_candles if not c['win'])}")

# ── Load odds data for each candle ────────────────────────────────────────────
def get_odds_for_candle(mtype, candle_start_ts, candle_end_ts, interval=300):
    db = f'/home/opc/market_{mtype}_5m.db'
    cs = (candle_start_ts // interval) * interval
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute("""
            SELECT unix_time, outcome, bid, ask, mid
            FROM polymarket_odds
            WHERE unix_time >= ? AND unix_time < ? AND outcome IN ('Up','Down')
            ORDER BY unix_time ASC
        """, (cs, cs + interval + 60)).fetchall()
        conn.close()
        return rows
    except:
        return []

# ── Analyse each candle ───────────────────────────────────────────────────────
enriched = []
for c in bbb_candles:
    odds = get_odds_for_candle(c['mtype'], c['first_ts'], c['last_ts'])
    if not odds:
        continue

    up_mids = [(float(ts), float(mid)) for ts, out, bid, ask, mid in odds if out == 'Up' and mid]
    dn_mids = [(float(ts), float(mid)) for ts, out, bid, ask, mid in odds if out == 'Down' and mid]

    if not up_mids:
        continue

    all_mids = [m for _, m in up_mids]
    candle_start = min(t for t, m in up_mids)
    candle_end   = max(t for t, m in up_mids)

    # Mid at candle open
    open_mid = up_mids[0][1]
    close_mid = up_mids[-1][1]

    # Max deviation from 0.50 (how far did it go in one direction)
    max_move = max(abs(m - 0.50) for m in all_mids)

    # Range of mid (volatility)
    mid_range = max(all_mids) - min(all_mids)

    # Did BOTH sides ever get below 0.50 during candle? (oscillation)
    min_up_mid = min(all_mids)
    max_up_mid = max(all_mids)
    both_sides_cheap = min_up_mid < 0.45 and max_up_mid > 0.55  # went both ways

    # How fast did it move (first time mid hit 0.35 or lower)
    time_to_extreme = None
    for ts, mid in up_mids:
        if mid <= 0.35 or mid >= 0.65:
            time_to_extreme = ts - candle_start
            break

    # Direction: did it trend or oscillate?
    # Count direction changes in mid
    direction_changes = 0
    for i in range(1, len(all_mids)-1):
        if (all_mids[i] - all_mids[i-1]) * (all_mids[i+1] - all_mids[i]) < 0:
            direction_changes += 1
    oscillation_score = direction_changes / max(len(all_mids)-2, 1)

    # Avg price of his buys over time - did he get better prices later?
    # Split his trades into first half and second half
    all_buy_ts = sorted([t for p,s,t in c['all_up']] + [t for p,s,t in c['all_dn']])
    if len(all_buy_ts) > 1:
        mid_ts = all_buy_ts[len(all_buy_ts)//2]
        early_up = [(p,s) for p,s,t in c['all_up'] if t <= mid_ts]
        late_up  = [(p,s) for p,s,t in c['all_up'] if t > mid_ts]
        early_dn = [(p,s) for p,s,t in c['all_dn'] if t <= mid_ts]
        late_dn  = [(p,s) for p,s,t in c['all_dn'] if t > mid_ts]

        def wavg(lst):
            if not lst: return None
            cost = sum(p*s for p,s in lst)
            sh   = sum(s for p,s in lst)
            return cost/sh if sh else None

        early_comb = (wavg(early_up) or 0) + (wavg(early_dn) or 0) if early_up and early_dn else None
        late_comb  = (wavg(late_up)  or 0) + (wavg(late_dn)  or 0) if late_up  and late_dn  else None
    else:
        early_comb = late_comb = None

    enriched.append({
        **c,
        'open_mid': open_mid,
        'close_mid': close_mid,
        'max_move': max_move,
        'mid_range': mid_range,
        'both_sides_cheap': both_sides_cheap,
        'time_to_extreme': time_to_extreme,
        'oscillation': oscillation_score,
        'direction_changes': direction_changes,
        'n_ticks': len(up_mids),
        'early_comb': early_comb,
        'late_comb': late_comb,
    })

wins  = [c for c in enriched if c['win']]
losses = [c for c in enriched if not c['win']]

def avg(lst, key):
    vals = [c[key] for c in lst if c[key] is not None]
    return sum(vals)/len(vals) if vals else 0

print(f"\n{'='*70}")
print(f"  ODDS MOVEMENT ANALYSIS — WIN vs LOSS candles")
print(f"{'='*70}")
print(f"\n  {'Metric':<35} {'WINS (n=%d)' % len(wins):>15} {'LOSSES (n=%d)' % len(losses):>15}")
print(f"  {'-'*65}")
metrics = [
    ('Combined avg entry',    'combined',          '%.4f'),
    ('Open mid (Up)',         'open_mid',           '%.3f'),
    ('Max move from 0.50',    'max_move',           '%.3f'),
    ('Mid range (volatility)','mid_range',          '%.3f'),
    ('Oscillation score',     'oscillation',        '%.3f'),
    ('Direction changes',     'direction_changes',  '%.1f'),
    ('N ticks observed',      'n_ticks',            '%.0f'),
    ('N trades (BBB)',        'n_trades',           '%.0f'),
    ('Both sides cheap?',     'both_sides_cheap',   '%.2f'),
    ('Early combined',        'early_comb',         '%.4f'),
    ('Late combined',         'late_comb',          '%.4f'),
]
for label, key, fmt in metrics:
    wv = avg(wins, key)
    lv = avg(losses, key)
    print(f"  {label:<35} {fmt % wv:>15} {fmt % lv:>15}")

print(f"\n{'='*70}")
print(f"  INDIVIDUAL LOSING CANDLES — what happened?")
print(f"{'='*70}")
for c in sorted(losses, key=lambda x: x['combined'], reverse=True):
    osc = c['oscillation']
    both = 'YES' if c['both_sides_cheap'] else 'NO'
    print(f"\n  {c['market'][-45:]}")
    print(f"    combined={c['combined']:.4f}  open_mid={c['open_mid']:.3f}  close_mid={c['close_mid']:.3f}")
    print(f"    max_move={c['max_move']:.3f}  mid_range={c['mid_range']:.3f}  oscillation={osc:.3f}  both_cheap={both}")
    print(f"    up={c['up_sh']:.0f}sh@{c['avg_up']:.3f}  dn={c['dn_sh']:.0f}sh@{c['avg_dn']:.3f}  trades={c['n_trades']}")
    if c['early_comb'] and c['late_comb']:
        print(f"    early_comb={c['early_comb']:.4f}  late_comb={c['late_comb']:.4f}  improvement={c['early_comb']-c['late_comb']:+.4f}")
