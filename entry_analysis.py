"""
Deep analysis of wallet entry behavior:
1. Cross-reference trade prices with live orderbook to see combined ask at entry
2. Check if wallets buy same side / correlate with each other
3. Identify the likely entry trigger
"""

import sqlite3, csv
from collections import defaultdict
from datetime import datetime, timezone

# ── Load orderbook DBs indexed by question text ─────────────────────────────

def load_db_by_question(db_path):
    """Returns: {question -> {outcome -> [(ts, bid, ask, mid)]}}"""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute('''
            SELECT unix_time, question, outcome, bid, ask, mid
            FROM polymarket_odds
            WHERE outcome IN ('Up','Down') AND ask > 0 AND mid > 0
            ORDER BY unix_time ASC
        ''').fetchall()
        conn.close()
    except:
        return {}
    idx = defaultdict(lambda: defaultdict(list))
    for ts, q, outcome, bid, ask, mid in rows:
        idx[q][outcome].append((float(ts), float(bid) if bid else 0, float(ask), float(mid)))
    return idx

print("Loading orderbook data...")
ob = {}
for label, path in [
    ('btc5',  r'market_btc_5m.db'),
    ('btc15', r'market_btc_15m.db'),
    ('eth5',  r'market_eth_5m.db'),
    ('eth15', r'market_eth_15m.db'),
]:
    db = load_db_by_question(path)
    for q, outcomes in db.items():
        if q not in ob:
            ob[q] = outcomes
    print(f"  {label}: {len(db)} markets loaded")

print(f"  Total unique markets in orderbook: {len(ob)}")

def get_ob_snapshot(question, trade_ts, window=45):
    """Get Up/Down ask at trade_ts ± window seconds."""
    if question not in ob:
        return None
    up_ticks = ob[question].get('Up', [])
    dn_ticks = ob[question].get('Down', [])
    if not up_ticks or not dn_ticks:
        return None
    def closest(ticks):
        best, bd = None, float('inf')
        for ts, bid, ask, mid in ticks:
            d = abs(ts - trade_ts)
            if d < bd and d <= window:
                bd = d; best = (ts, bid, ask, mid)
        return best
    up = closest(up_ticks)
    dn = closest(dn_ticks)
    if not up or not dn:
        return None
    return {
        'up_bid': up[1], 'up_ask': up[2], 'up_mid': up[3],
        'dn_bid': dn[1], 'dn_ask': dn[2], 'dn_mid': dn[3],
        'combined_ask': up[2] + dn[2],
        'combined_mid': up[3] + dn[3],
    }

# ── Load wallet trades ───────────────────────────────────────────────────────

def parse_ts(s):
    try:
        f = float(s)
        return f / 1000 if f > 9999999999 else f
    except:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except:
            pass
    return 0

WALLET_FILES = {
    'wallet_1': 'wallet_1_trades.csv',
    'wallet_2': 'wallet_2_trades.csv',
    'wallet_3': 'wallet_3_trades.csv',
    'wallet_4': 'wallet_4_trades.csv',
    'wallet_5': 'wallet_5_trades.csv',
    'wallet_6': 'wallet_6_trades.csv',
    'wallet_7': 'wallet_7_trades.csv',
    'wallet_8': 'wallet_8_fresh.csv',
    'wallet_9': 'wallet_9_trades.csv',
    'bosh':     'wallet_target.csv',
}

all_trades = {}
for name, f in WALLET_FILES.items():
    try:
        rows = []
        for r in csv.DictReader(open(f)):
            ts = parse_ts(r.get('timestamp', r.get('time_utc', '0')))
            price = float(r.get('price') or 0)
            side  = r.get('side', '').upper()
            outcome = r.get('outcome', '')
            market  = r.get('market', '')
            if not price or not outcome or not market or not ts:
                continue
            rows.append({'ts': ts, 'side': side, 'outcome': outcome,
                         'price': price, 'size': float(r.get('size') or 0), 'market': market})
        all_trades[name] = sorted(rows, key=lambda x: x['ts'])
        print(f"{name}: {len(rows)} trades")
    except Exception as e:
        print(f"{name}: ERROR {e}")

# ── ANALYSIS 1: Combined ask at entry ───────────────────────────────────────

print()
print("=" * 80)
print("ANALYSIS 1: What was the combined ask at the moment of each buy?")
print("=" * 80)

# Focus on the shared candles with all 9 wallets
TARGET_CANDLES = [
    'Bitcoin Up or Down - March 19, 6:30PM-6:35PM ET',
    'Bitcoin Up or Down - March 19, 6:25PM-6:30PM ET',
    'Bitcoin Up or Down - March 19, 6:20PM-6:25PM ET',
    'Bitcoin Up or Down - March 19, 6:15PM-6:20PM ET',
]

for candle in TARGET_CANDLES:
    print(f"\n--- {candle} ---")
    print(f"{'Wallet':<12} {'Side':<6} {'Outcome':<7} {'Trade$':<8} {'UpAsk':<8} {'DnAsk':<8} {'CombAsk':<10} {'ARB?'}")
    print("-" * 75)

    candle_ob_snapshots = []

    for name, trades in all_trades.items():
        candle_trades = [t for t in trades if t['market'] == candle and t['side'] == 'BUY']
        if not candle_trades:
            continue
        for t in candle_trades[:4]:  # first 4 buys per wallet
            snap = get_ob_snapshot(candle, t['ts'])
            arb = ''
            if snap:
                arb = '< ARB' if snap['combined_ask'] < 1.0 else ''
                candle_ob_snapshots.append(snap['combined_ask'])
                print(f"  {name:<10} {t['side']:<6} {t['outcome']:<7} {t['price']:<8.4f} "
                      f"{snap['up_ask']:<8.4f} {snap['dn_ask']:<8.4f} {snap['combined_ask']:<10.4f} {arb}")
            else:
                print(f"  {name:<10} {t['side']:<6} {t['outcome']:<7} {t['price']:<8.4f} {'no_ob':<8} {'no_ob':<8} {'?':<10}")

    if candle_ob_snapshots:
        avg_ca = sum(candle_ob_snapshots) / len(candle_ob_snapshots)
        pct_arb = 100 * sum(1 for c in candle_ob_snapshots if c < 1.0) / len(candle_ob_snapshots)
        print(f"\n  >> Combined ask at entry: avg={avg_ca:.4f} | {pct_arb:.0f}% of entries were ARB (< 1.0)")

# ── ANALYSIS 2: Wallet correlation — same side same candle? ─────────────────

print()
print("=" * 80)
print("ANALYSIS 2: When wallets trade the same candle, do they buy the SAME side?")
print("=" * 80)

# For each shared candle, what side did each wallet favor?
all_candles = set()
for trades in all_trades.values():
    all_candles.update(t['market'] for t in trades)

shared_candle_sides = []
agree_count = disagree_count = 0

for candle in sorted(all_candles):
    wallet_sides = {}
    for name, trades in all_trades.items():
        ct = [t for t in trades if t['market'] == candle and t['side'] == 'BUY']
        if not ct:
            continue
        up_vol   = sum(t['price'] * t['size'] for t in ct if t['outcome'] == 'Up')
        dn_vol   = sum(t['price'] * t['size'] for t in ct if t['outcome'] == 'Down')
        # dominant side by volume
        if up_vol > 0 and dn_vol > 0:
            dominant = 'BOTH'
        elif up_vol > dn_vol:
            dominant = 'Up'
        else:
            dominant = 'Down'
        wallet_sides[name] = {'dominant': dominant, 'up_vol': up_vol, 'dn_vol': dn_vol}

    if len(wallet_sides) < 2:
        continue

    sides = [v['dominant'] for v in wallet_sides.values()]
    all_same = len(set(sides)) == 1
    shared_candle_sides.append((candle, wallet_sides, all_same))

print(f"\nShared candles analyzed: {len(shared_candle_sides)}")

# Side agreement matrix
print(f"\n{'Candle':<55} {'Wallets':>7} {'Agreement'}")
print("-" * 80)
for candle, wallet_sides, all_same in sorted(shared_candle_sides, key=lambda x: -len(x[1])):
    n = len(wallet_sides)
    sides_str = ', '.join(f"{w}:{v['dominant']}" for w, v in list(wallet_sides.items())[:5])
    agree = 'ALL SAME' if all_same else 'MIXED'
    print(f"{candle[:54]:<55} {n:>7}  {agree} [{sides_str}...]")

# ── ANALYSIS 3: Do they buy cheap side or expensive side? ───────────────────

print()
print("=" * 80)
print("ANALYSIS 3: When buying Up/Down, is it the CHEAP (<0.50) or EXPENSIVE (>0.50) side?")
print("=" * 80)

print(f"\n{'Wallet':<12} {'Up<0.5%':>9} {'Up>0.5%':>9} {'Dn<0.5%':>9} {'Dn>0.5%':>9}  Interpretation")
print("-" * 75)

for name, trades in all_trades.items():
    buys = [t for t in trades if t['side'] == 'BUY']
    up_cheap  = sum(1 for t in buys if t['outcome'] == 'Up'   and t['price'] < 0.50)
    up_exp    = sum(1 for t in buys if t['outcome'] == 'Up'   and t['price'] >= 0.50)
    dn_cheap  = sum(1 for t in buys if t['outcome'] == 'Down' and t['price'] < 0.50)
    dn_exp    = sum(1 for t in buys if t['outcome'] == 'Down' and t['price'] >= 0.50)
    total = max(len(buys), 1)

    # Interpretation
    if up_cheap / total > 0.3 and dn_exp / total > 0.3:
        interp = "Buys cheap Up + expensive Down (expects DOWN)"
    elif dn_cheap / total > 0.3 and up_exp / total > 0.3:
        interp = "Buys cheap Down + expensive Up (expects UP)"
    elif (up_cheap + dn_cheap) / total > 0.5:
        interp = "Buys mostly cheap side on both (arb hunter)"
    else:
        interp = "Mixed / buys both sides near 0.50"

    print(f"  {name:<10} {100*up_cheap/total:>8.1f}% {100*up_exp/total:>8.1f}% {100*dn_cheap/total:>8.1f}% {100*dn_exp/total:>8.1f}%  {interp}")

# ── ANALYSIS 4: Price sequence within a candle — what triggers re-entry? ────

print()
print("=" * 80)
print("ANALYSIS 4: Within a candle, do wallets buy in CLUSTERS or CONTINUOUSLY?")
print("            (Looking at time gaps between consecutive buys)")
print("=" * 80)

for name in ['wallet_8', 'wallet_9', 'bosh', 'wallet_1']:
    trades = all_trades.get(name, [])
    buys   = [t for t in trades if t['side'] == 'BUY']
    if len(buys) < 2:
        continue

    # Time gaps between consecutive buys
    gaps = []
    for i in range(1, len(buys)):
        gap = buys[i]['ts'] - buys[i-1]['ts']
        if gap < 600:  # ignore cross-candle gaps
            gaps.append(gap)

    if not gaps:
        continue

    cluster_gaps  = [g for g in gaps if g <= 5]    # within 5 seconds = burst
    medium_gaps   = [g for g in gaps if 5 < g <= 60]
    large_gaps    = [g for g in gaps if g > 60]

    print(f"\n  {name}: {len(buys)} buys | avg gap={sum(gaps)/len(gaps):.1f}s")
    print(f"    Burst (<5s):   {len(cluster_gaps):>4} ({100*len(cluster_gaps)/len(gaps):.0f}%)")
    print(f"    Medium (5-60s):{len(medium_gaps):>4} ({100*len(medium_gaps)/len(gaps):.0f}%)")
    print(f"    Large (>60s):  {len(large_gaps):>4} ({100*len(large_gaps)/len(gaps):.0f}%)")

    # Price changes between large gaps — do they buy after a price move?
    if large_gaps:
        print(f"    After large gaps — price of next buy vs prev buy:")
        count = 0
        for i in range(1, len(buys)):
            gap = buys[i]['ts'] - buys[i-1]['ts']
            if gap > 60 and count < 8:
                p_prev = buys[i-1]['price']
                p_next = buys[i]['price']
                dt = datetime.fromtimestamp(buys[i]['ts'], tz=timezone.utc).strftime('%H:%M:%S')
                print(f"      {dt} gap={gap:.0f}s | prev={p_prev:.3f}{buys[i-1]['outcome'][:1]} -> next={p_next:.3f}{buys[i]['outcome'][:1]} | delta={p_next-p_prev:+.3f}")
                count += 1

# -- ANALYSIS 5: First buy in each candle -- momentum or contrarian? ----------

print()
print("=" * 80)
print("ANALYSIS 5: First buy in each candle -- momentum or contrarian?")
print("            (Does the first buy go WITH the favored side, or AGAINST it?)")
print("=" * 80)

for name in ['wallet_1', 'wallet_8', 'wallet_9', 'bosh']:
    trades = all_trades.get(name, [])
    buys = [t for t in trades if t['side'] == 'BUY']
    candles = defaultdict(list)
    for t in buys:
        candles[t['market']].append(t)

    momentum = contrarian = neutral = 0
    for candle, ctrades in candles.items():
        first = ctrades[0]
        p = first['price']
        outcome = first['outcome']
        # if buying Up at >0.50: Up is favored, buying momentum
        # if buying Up at <0.50: Up is cheap/unfavored, buying contrarian
        # if buying Down at >0.50: Down is favored, buying momentum
        # if buying Down at <0.50: Down is cheap, buying contrarian
        snap = get_ob_snapshot(candle, first['ts'])
        if snap:
            if outcome == 'Up':
                favored = snap['up_mid'] > 0.50
            else:
                favored = snap['dn_mid'] > 0.50
            if favored:
                momentum += 1
            else:
                contrarian += 1
        else:
            neutral += 1

    total = momentum + contrarian
    if total:
        print(f"  {name:<12}: momentum={momentum} ({100*momentum/total:.0f}%) | contrarian={contrarian} ({100*contrarian/total:.0f}%) | no_ob={neutral}")

# -- ANALYSIS 6: Do wallets enter same candle at same TIME? -------------------

print()
print("=" * 80)
print("ANALYSIS 6: Do wallets enter the same candle at the same timestamp?")
print("            (True correlation = same bot / same signal)")
print("=" * 80)

# Pick the most-shared candle: BTC 6:30PM-6:35PM
focus = 'Bitcoin Up or Down - March 19, 6:25PM-6:30PM ET'
print(f"\nFocus candle: {focus}")
print(f"{'Wallet':<12} {'FirstBuy':>10} {'Price':>7} {'Outcome':>8} {'OB_UpAsk':>10} {'OB_DnAsk':>10} {'CombAsk':>9}")
print("-" * 72)

first_entries = []
for name, trades in all_trades.items():
    ct = sorted([t for t in trades if t['market'] == focus and t['side'] == 'BUY'], key=lambda x: x['ts'])
    if not ct:
        continue
    f = ct[0]
    snap = get_ob_snapshot(focus, f['ts'])
    dt = datetime.fromtimestamp(f['ts'], tz=timezone.utc).strftime('%H:%M:%S')
    first_entries.append((name, f['ts'], f['price'], f['outcome'], snap))
    if snap:
        print(f"  {name:<10} {dt:>10} {f['price']:>7.4f} {f['outcome']:>8} {snap['up_ask']:>10.4f} {snap['dn_ask']:>10.4f} {snap['combined_ask']:>9.4f}")
    else:
        print(f"  {name:<10} {dt:>10} {f['price']:>7.4f} {f['outcome']:>8} {'no_ob':>10} {'no_ob':>10} {'?':>9}")

if first_entries:
    ts_vals = [e[1] for e in first_entries]
    spread_s = max(ts_vals) - min(ts_vals)
    print(f"\n  Time spread of first entries: {spread_s:.1f} seconds")
    print(f"  (If <10s, wallets are likely triggered by the same signal)")
