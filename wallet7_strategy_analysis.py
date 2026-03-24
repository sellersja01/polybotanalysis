"""
Wallet_7 Strategy Analysis — Tests 1, 2, 3

Test 1: First buy side vs candle outcome (momentum vs contrarian?)
Test 2: Price at first entry vs outcome (did they buy cheap or expensive first?)
Test 3: Sizing ratio (up_shares/dn_shares) vs outcome (does heavy side win more?)
"""

import csv
import re
import json
import sqlite3
import requests
import time
from collections import defaultdict
from datetime import datetime, timezone

WALLET_CSV = 'Wallets_new/wallet_7_merged.csv'
DBS = {
    'BTC_5m':  ('databases/market_btc_5m.db',  300),
    'BTC_15m': ('databases/market_btc_15m.db', 900),
}

# ── Load trades ───────────────────────────────────────────────────

def load_candles():
    buys = []
    with open(WALLET_CSV, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['side'].upper() == 'BUY' and row['market'].strip():
                buys.append(row)

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for row in buys:
        out = row['outcome'].strip().capitalize()
        if out in ('Up', 'Down'):
            candles[row['market'].strip()][out].append(row)

    # Sort each side by timestamp
    for title, sides in candles.items():
        for out in ('Up', 'Down'):
            sides[out].sort(key=lambda r: float(r['timestamp']))

    return candles


# ── Load winners (DB + API) ───────────────────────────────────────

def parse_candle(title):
    match = re.search(r'(\d+:\d+(?:AM|PM))-(\d+:\d+(?:AM|PM))', title, re.IGNORECASE)
    if not match: return None
    fmt = '%I:%M%p'
    try:
        t1 = datetime.strptime(match.group(1).upper(), fmt)
        t2 = datetime.strptime(match.group(2).upper(), fmt)
        diff = int((t2 - t1).total_seconds())
        if diff < 0: diff += 86400
    except: return None
    date_match = re.search(r'(\w+ \d+)', title)
    if not date_match: return None
    for yr in [2026, 2025]:
        try:
            naive = datetime.strptime(f'{date_match.group(1)} {yr} {match.group(1).upper()}', '%B %d %Y %I:%M%p')
            ts = int(naive.timestamp()) + 4 * 3600
            break
        except: ts = None
    if ts is None: return None
    db_key = f"{'BTC' if 'bitcoin' in title.lower() else 'ETH'}_{diff//60}m"
    slug   = f"{'btc' if 'bitcoin' in title.lower() else 'eth'}-updown-{diff//60}m-{ts}"
    return ts, diff, db_key, slug


def load_winners(candles):
    # DB winners
    db_winners = {}
    for db_key, (db_path, interval) in DBS.items():
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute('SELECT unix_time, market_id, outcome, mid FROM polymarket_odds WHERE outcome IN ("Up","Down") AND mid > 0').fetchall()
            conn.close()
        except: continue
        last_up, last_dn = {}, {}
        for ts, mid_id, out, mid in rows:
            cs  = (int(float(ts)) // interval) * interval
            key = (cs, mid_id)
            if out == 'Up': last_up[key] = float(mid)
            else:           last_dn[key] = float(mid)
        for key in set(last_up) | set(last_dn):
            cs, _ = key
            db_winners[(db_key, cs)] = 'Up' if last_up.get(key, 0) >= last_dn.get(key, 0) else 'Down'

    winners = {}
    api_needed = []

    for title in candles:
        p = parse_candle(title)
        if p is None: continue
        ts, interval, db_key, slug = p
        winner = None
        for off in [0, -interval, interval, -300, 300]:
            w = db_winners.get((db_key, ts + off))
            if w: winner = w; break
        if winner:
            winners[title] = winner
        else:
            api_needed.append((title, slug))

    print(f"  DB: {len(winners)}, querying API for {len(api_needed)}...")
    for title, slug in api_needed:
        try:
            r = requests.get(f'https://gamma-api.polymarket.com/events?slug={slug}', timeout=10)
            data = r.json()
            if data:
                m = data[0]['markets'][0]
                if m.get('closed'):
                    prices = json.loads(m.get('outcomePrices', '[]'))
                    if len(prices) >= 2:
                        winners[title] = 'Up' if float(prices[0]) > 0.5 else 'Down'
        except: pass
        time.sleep(0.2)

    print(f"  Total winners resolved: {len(winners)}")
    return winners


# ── TEST 1: First buy side vs outcome ─────────────────────────────

def test1(candles, winners):
    print(f"\n{'='*62}")
    print(f"  TEST 1: Which side did they buy FIRST vs who won?")
    print(f"{'='*62}")

    first_is_winner  = 0
    first_is_loser   = 0
    same_ts          = 0
    no_data          = 0

    # Break down by first-buy price bucket
    first_price_buckets = {
        'cheap  (<0.30)':  {'correct': 0, 'wrong': 0},
        'mid  (0.30-0.50)':{'correct': 0, 'wrong': 0},
        'exp   (>0.50)':   {'correct': 0, 'wrong': 0},
    }

    for title, sides in candles.items():
        winner = winners.get(title)
        if winner is None:
            no_data += 1
            continue

        up_trades = sides['Up']
        dn_trades = sides['Down']
        if not up_trades or not dn_trades:
            no_data += 1
            continue

        up_first_ts = float(up_trades[0]['timestamp'])
        dn_first_ts = float(dn_trades[0]['timestamp'])

        if abs(up_first_ts - dn_first_ts) < 2:
            same_ts += 1
            first_side = None
        elif up_first_ts < dn_first_ts:
            first_side = 'Up'
        else:
            first_side = 'Down'

        if first_side is None:
            continue

        correct = (first_side == winner)
        if correct: first_is_winner += 1
        else:       first_is_loser  += 1

        # Price bucket of the first buy on the first side
        first_price = float(up_trades[0]['price'] if first_side == 'Up' else dn_trades[0]['price'])
        if first_price < 0.30:   bucket = 'cheap  (<0.30)'
        elif first_price < 0.50: bucket = 'mid  (0.30-0.50)'
        else:                    bucket = 'exp   (>0.50)'
        first_price_buckets[bucket]['correct' if correct else 'wrong'] += 1

    total = first_is_winner + first_is_loser
    print(f"\n  Their first buy side matched the winner:")
    print(f"    YES (momentum signal) : {first_is_winner:4d} ({first_is_winner/total*100:.1f}%)")
    print(f"    NO  (contrarian)      : {first_is_loser:4d}  ({first_is_loser/total*100:.1f}%)")
    print(f"    Same timestamp        : {same_ts}")
    print(f"    No data               : {no_data}")
    print(f"\n  If random: expected 50%. >50% = momentum bias, <50% = contrarian bias.")

    print(f"\n  First-buy price vs accuracy:")
    print(f"  {'Bucket':22s} {'Correct':>8} {'Wrong':>8} {'Accuracy':>10}")
    print(f"  {'-'*52}")
    for bucket, counts in first_price_buckets.items():
        c, w = counts['correct'], counts['wrong']
        t = c + w
        if t == 0: continue
        print(f"  {bucket:22s} {c:>8} {w:>8} {c/t*100:>9.1f}%")

    print(f"\n  Interpretation:")
    if first_is_winner / total > 0.55:
        print(f"  -> MOMENTUM: They tend to buy the winning side first ({first_is_winner/total*100:.0f}% of time)")
        print(f"     Likely watching early candle direction and entering the leading side.")
    elif first_is_winner / total < 0.45:
        print(f"  -> CONTRARIAN: They tend to buy the losing side first ({first_is_loser/total*100:.0f}% of time)")
        print(f"     Likely buying the cheap/falling side first, betting on reversal.")
    else:
        print(f"  -> MIXED: No clear directional bias in first buy ({first_is_winner/total*100:.0f}% match winner)")


# ── TEST 2: First entry price vs outcome ─────────────────────────

def test2(candles, winners):
    print(f"\n{'='*62}")
    print(f"  TEST 2: Entry price distribution — cheap vs expensive first")
    print(f"{'='*62}")

    # For each candle: what was the avg price they paid on the WINNING side vs LOSING side?
    winning_side_avg_prices = []
    losing_side_avg_prices  = []

    # Also: did they enter the winning side at a LOWER price (contrarian = yes)?
    cheaper_on_winner = 0
    cheaper_on_loser  = 0

    for title, sides in candles.items():
        winner = winners.get(title)
        if winner is None: continue
        loser = 'Down' if winner == 'Up' else 'Up'

        win_trades  = sides[winner]
        loss_trades = sides[loser]
        if not win_trades or not loss_trades: continue

        win_avg  = sum(float(r['price']) for r in win_trades)  / len(win_trades)
        loss_avg = sum(float(r['price']) for r in loss_trades) / len(loss_trades)

        winning_side_avg_prices.append(win_avg)
        losing_side_avg_prices.append(loss_avg)

        if win_avg < loss_avg:
            cheaper_on_winner += 1   # they paid less for the winning side (contrarian buy)
        else:
            cheaper_on_loser  += 1   # they paid more for the winning side (momentum buy)

    n = cheaper_on_winner + cheaper_on_loser

    def avg(lst): return sum(lst) / len(lst) if lst else 0
    def med(lst):
        s = sorted(lst)
        return s[len(s)//2] if s else 0

    print(f"\n  Avg price paid on WINNING side : ${avg(winning_side_avg_prices):.3f}  (median ${med(winning_side_avg_prices):.3f})")
    print(f"  Avg price paid on LOSING side  : ${avg(losing_side_avg_prices):.3f}  (median ${med(losing_side_avg_prices):.3f})")
    print(f"\n  Paid LESS for winning side (contrarian) : {cheaper_on_winner} ({cheaper_on_winner/n*100:.1f}%)")
    print(f"  Paid MORE for winning side (momentum)   : {cheaper_on_loser}  ({cheaper_on_loser/n*100:.1f}%)")

    # Distribution of winning-side avg price
    buckets = {'<0.20': 0, '0.20-0.35': 0, '0.35-0.50': 0, '0.50-0.65': 0, '>0.65': 0}
    for p in winning_side_avg_prices:
        if p < 0.20:   buckets['<0.20'] += 1
        elif p < 0.35: buckets['0.20-0.35'] += 1
        elif p < 0.50: buckets['0.35-0.50'] += 1
        elif p < 0.65: buckets['0.50-0.65'] += 1
        else:          buckets['>0.65'] += 1

    print(f"\n  Avg price distribution on the WINNING side:")
    for b, c in buckets.items():
        pct = c / len(winning_side_avg_prices) * 100
        bar = '#' * int(pct / 2)
        print(f"    {b:12s} {c:4d} ({pct:5.1f}%) {bar}")

    print(f"\n  Interpretation:")
    if cheaper_on_winner / n > 0.55:
        print(f"  -> CONTRARIAN: They typically pay LESS for the side that wins.")
        print(f"     Avg winning-side price ${avg(winning_side_avg_prices):.3f} vs losing-side ${avg(losing_side_avg_prices):.3f}")
        print(f"     They're loading the cheap/falling side and it's winning.")
    else:
        print(f"  -> MOMENTUM: They typically pay MORE for the side that wins.")
        print(f"     Avg winning-side price ${avg(winning_side_avg_prices):.3f} vs losing-side ${avg(losing_side_avg_prices):.3f}")
        print(f"     They're loading the expensive/rising side.")


# ── TEST 3: Sizing ratio vs outcome ──────────────────────────────

def test3(candles, winners):
    print(f"\n{'='*62}")
    print(f"  TEST 3: Does the heavier side win more often?")
    print(f"{'='*62}")

    heavy_wins   = 0   # they put more $ on winning side
    heavy_loses  = 0   # they put more $ on losing side
    equal        = 0

    ratios_when_heavy_wins  = []
    ratios_when_heavy_loses = []

    for title, sides in candles.items():
        winner = winners.get(title)
        if winner is None: continue
        loser = 'Down' if winner == 'Up' else 'Up'

        win_usdc  = sum(float(r['usdc']) for r in sides[winner])
        loss_usdc = sum(float(r['usdc']) for r in sides[loser])
        if win_usdc == 0 or loss_usdc == 0: continue

        ratio = max(win_usdc, loss_usdc) / min(win_usdc, loss_usdc)

        if win_usdc > loss_usdc * 1.05:
            heavy_wins += 1
            ratios_when_heavy_wins.append(ratio)
        elif loss_usdc > win_usdc * 1.05:
            heavy_loses += 1
            ratios_when_heavy_loses.append(ratio)
        else:
            equal += 1

    total = heavy_wins + heavy_loses + equal

    def avg(lst): return sum(lst)/len(lst) if lst else 0

    print(f"\n  Heavy side = winning side : {heavy_wins:4d} ({heavy_wins/total*100:.1f}%)")
    print(f"  Heavy side = losing side  : {heavy_loses:4d} ({heavy_loses/total*100:.1f}%)")
    print(f"  Roughly equal sizing      : {equal:4d}  ({equal/total*100:.1f}%)")
    print(f"\n  Avg ratio when heavy=winner : {avg(ratios_when_heavy_wins):.2f}x")
    print(f"  Avg ratio when heavy=loser  : {avg(ratios_when_heavy_loses):.2f}x")

    # Bucket by ratio
    print(f"\n  Sizing ratio distribution (winner$ / loser$):")
    all_ratios = []
    for title, sides in candles.items():
        winner = winners.get(title)
        if winner is None: continue
        loser = 'Down' if winner == 'Up' else 'Up'
        win_usdc  = sum(float(r['usdc']) for r in sides[winner])
        loss_usdc = sum(float(r['usdc']) for r in sides[loser])
        if win_usdc > 0 and loss_usdc > 0:
            all_ratios.append(win_usdc / loss_usdc)

    buckets = {'<0.5x': 0, '0.5-0.8x': 0, '0.8-1.2x': 0, '1.2-2x': 0, '2-5x': 0, '>5x': 0}
    for r in all_ratios:
        if r < 0.5:    buckets['<0.5x'] += 1
        elif r < 0.8:  buckets['0.5-0.8x'] += 1
        elif r < 1.2:  buckets['0.8-1.2x'] += 1
        elif r < 2.0:  buckets['1.2-2x'] += 1
        elif r < 5.0:  buckets['2-5x'] += 1
        else:          buckets['>5x'] += 1

    n = len(all_ratios)
    for b, c in buckets.items():
        pct = c/n*100
        bar = '#' * int(pct/2)
        print(f"    {b:10s} {c:4d} ({pct:5.1f}%) {bar}")

    print(f"\n  Interpretation:")
    if heavy_wins / (heavy_wins + heavy_loses) > 0.55:
        print(f"  -> SIGNAL: Their heavy side wins {heavy_wins/(heavy_wins+heavy_loses)*100:.0f}% of the time.")
        print(f"     They have a real directional signal informing their sizing.")
    else:
        print(f"  -> NO SIGNAL: Heavy side wins only {heavy_wins/(heavy_wins+heavy_loses)*100:.0f}% — sizing is not predictive.")
        print(f"     Sizing may be driven by price (cheap = more shares) not direction.")


# ── Main ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Loading trades...")
    candles = load_candles()
    print(f"  {sum(len(s['Up'])+len(s['Down']) for s in candles.values())} buys across {len(candles)} candles")

    print("\nResolving winners...")
    winners = load_winners(candles)

    test1(candles, winners)
    test2(candles, winners)
    test3(candles, winners)

    print(f"\n{'='*62}")
    print(f"  DONE")
    print(f"{'='*62}")
