"""
Loads all wallet trade data and finds behavioral similarities across wallets.
Uses original wallet_*_trades.csv (weekday/Thu data) + wallet_8_fresh.csv.
Focuses on HOW they trade: price levels, position building, order sizing,
market selection, buy/sell behavior — not timing.
"""

import csv
import os
from collections import defaultdict, Counter
from datetime import datetime, timezone

# ── Load data ──────────────────────────────────────────────────────────────

WALLETS = {
    'wallet_1':  ('0x61276aba49117fd9299707d5d573652949d5c977', 'wallet_1_trades.csv'),
    'wallet_2':  ('0x5bde889dc26b097b5eaa2f1f027e01712ebccbb7', 'wallet_2_trades.csv'),
    'wallet_3':  ('0xd111ced402bac802f74606deca83bbf6a1eaaf32', 'wallet_3_trades.csv'),
    'wallet_4':  ('0x437bfe05a1e169b1443f16e718525a88b6f283b2', 'wallet_4_trades.csv'),
    'wallet_5':  ('0x52f8784a81d967a3afb74d2e1608503ff5e261b9', 'wallet_5_trades.csv'),
    'wallet_6':  ('0xa84edaf1a562eabb463dc6cf4c3e9c407a5edbeb', 'wallet_6_trades.csv'),
    'wallet_7':  ('0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82', 'wallet_7_trades.csv'),
    'wallet_8':  ('0xf539c942036cc7633a1e0015209a1343e9b2dda9', 'wallet_8_fresh.csv'),
    'wallet_9':  ('0x37c94ea1b44e01b18a1ce3ab6f8002bd6b9d7e6d', 'wallet_9_trades.csv'),
    'bosh':      ('0x29bc82f761749e67fa00d62896bc6855097b683c', 'wallet_target.csv'),
}

def parse_ts(s):
    """Parse timestamp string to unix float."""
    try:
        f = float(s)
        if f > 9999999999: f /= 1000
        return f
    except:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except:
            pass
    return 0

def load_wallet(csv_path):
    rows = []
    with open(csv_path, newline='') as f:
        for r in csv.DictReader(f):
            price = float(r.get('price') or 0)
            size  = float(r.get('size') or 0)
            side  = r.get('side', '').upper()
            outcome = r.get('outcome', '')
            market  = r.get('market', '')
            ts_raw  = r.get('timestamp', r.get('time_utc', '0'))
            ts = parse_ts(ts_raw)
            if not price or not outcome or not market or not ts:
                continue
            rows.append({
                'ts': ts, 'side': side, 'outcome': outcome,
                'price': price, 'size': size, 'market': market,
            })
    return sorted(rows, key=lambda x: x['ts'])

all_data = {}
for name, (addr, csv_path) in WALLETS.items():
    if not os.path.exists(csv_path):
        print(f'{name}: file not found ({csv_path})')
        continue
    rows = load_wallet(csv_path)
    if rows:
        all_data[name] = rows
        d0 = datetime.fromtimestamp(rows[0]['ts'],  tz=timezone.utc).strftime('%Y-%m-%d %a')
        d1 = datetime.fromtimestamp(rows[-1]['ts'], tz=timezone.utc).strftime('%Y-%m-%d %a')
        print(f'{name}: {len(rows)} trades | {d0} -> {d1}')

print()

# ── Per-wallet stats ────────────────────────────────────────────────────────

def candle_key(market):
    """Normalize market name to group by candle."""
    return market.strip()

def analyze_wallet(name, rows):
    buys  = [r for r in rows if r['side'] == 'BUY']
    sells = [r for r in rows if r['side'] == 'SELL']

    up_buys = [r for r in buys if r['outcome'] == 'Up']
    dn_buys = [r for r in buys if r['outcome'] == 'Down']

    # ── Market selection ──
    markets = Counter(r['market'] for r in rows)
    btc = sum(v for k, v in markets.items() if 'Bitcoin' in k)
    eth = sum(v for k, v in markets.items() if 'Ethereum' in k)
    sol = sum(v for k, v in markets.items() if 'Solana' in k)
    m5  = sum(v for k, v in markets.items() if any(x in k for x in ['5PM','6PM','7PM','8PM','9PM','10PM','11PM','0AM','1AM','2AM','3AM','4AM','5AM','6AM','7AM','8AM','9AM','10AM','11AM','12PM','1PM','2PM','3PM','4PM'] if k.count('-')==3 and 'PM ET' in k))
    # simpler: count by timeframe in name
    t5m  = 0  # placeholder

    # ── Per-candle stats ──
    candles = defaultdict(lambda: {'buys': [], 'sells': []})
    for r in rows:
        ck = candle_key(r['market'])
        if r['side'] == 'BUY':
            candles[ck]['buys'].append(r)
        elif r['side'] == 'SELL':
            candles[ck]['sells'].append(r)

    candle_stats = []
    for ck, cd in candles.items():
        cbuys = cd['buys']
        csells = cd['sells']
        if not cbuys: continue

        up_b = [r for r in cbuys if r['outcome'] == 'Up']
        dn_b = [r for r in cbuys if r['outcome'] == 'Down']

        avg_up = sum(r['price'] for r in up_b) / len(up_b) if up_b else None
        avg_dn = sum(r['price'] for r in dn_b) / len(dn_b) if dn_b else None
        combined = (avg_up + avg_dn) if avg_up and avg_dn else None

        prices = [r['price'] for r in cbuys]
        sizes  = [r['size']  for r in cbuys]

        candle_stats.append({
            'market': ck,
            'n_buys': len(cbuys),
            'n_sells': len(csells),
            'n_up_buys': len(up_b),
            'n_dn_buys': len(dn_b),
            'avg_up': avg_up,
            'avg_dn': avg_dn,
            'combined': combined,
            'min_buy': min(prices) if prices else 0,
            'max_buy': max(prices) if prices else 0,
            'avg_size': sum(sizes) / len(sizes) if sizes else 0,
            'total_usdc': sum(r['price'] * r['size'] for r in cbuys),
            'has_both_sides': avg_up is not None and avg_dn is not None,
        })

    both_candles = [c for c in candle_stats if c['has_both_sides']]
    one_sided    = [c for c in candle_stats if not c['has_both_sides']]

    # ── Price distribution buckets ──
    buy_prices = [r['price'] for r in buys]
    buckets = {
        '0.00-0.10': sum(1 for p in buy_prices if p < 0.10),
        '0.10-0.20': sum(1 for p in buy_prices if 0.10 <= p < 0.20),
        '0.20-0.30': sum(1 for p in buy_prices if 0.20 <= p < 0.30),
        '0.30-0.40': sum(1 for p in buy_prices if 0.30 <= p < 0.40),
        '0.40-0.50': sum(1 for p in buy_prices if 0.40 <= p < 0.50),
        '0.50-0.60': sum(1 for p in buy_prices if 0.50 <= p < 0.60),
        '0.60-0.70': sum(1 for p in buy_prices if 0.60 <= p < 0.70),
        '0.70-0.80': sum(1 for p in buy_prices if 0.70 <= p < 0.80),
        '0.80-0.90': sum(1 for p in buy_prices if 0.80 <= p < 0.90),
        '0.90-1.00': sum(1 for p in buy_prices if p >= 0.90),
    }
    total_b = len(buy_prices) or 1

    # ── Sell behavior ──
    sell_prices = [r['price'] for r in sells]

    return {
        'name': name,
        'total_trades': len(rows),
        'n_buys': len(buys),
        'n_sells': len(sells),
        'buy_sell_ratio': len(buys) / max(len(sells), 1),
        'n_up_buys': len(up_buys),
        'n_dn_buys': len(dn_buys),
        'up_dn_ratio': len(up_buys) / max(len(dn_buys), 1),
        'btc_trades': btc,
        'eth_trades': eth,
        'btc_pct': 100 * btc / max(btc + eth, 1),
        'n_candles': len(candle_stats),
        'both_sides_pct': 100 * len(both_candles) / max(len(candle_stats), 1),
        'avg_buys_per_candle': sum(c['n_buys'] for c in candle_stats) / max(len(candle_stats), 1),
        'avg_sells_per_candle': sum(c['n_sells'] for c in candle_stats) / max(len(candle_stats), 1),
        'avg_combined': sum(c['combined'] for c in both_candles) / max(len(both_candles), 1),
        'pct_combined_lt_1': 100 * sum(1 for c in both_candles if c['combined'] < 1.0) / max(len(both_candles), 1),
        'avg_up_buy_price': sum(r['price'] for r in up_buys) / max(len(up_buys), 1),
        'avg_dn_buy_price': sum(r['price'] for r in dn_buys) / max(len(dn_buys), 1),
        'avg_buy_price': sum(buy_prices) / max(len(buy_prices), 1),
        'min_buy_price': min(buy_prices) if buy_prices else 0,
        'pct_buys_below_020': 100 * buckets['0.00-0.10'] + 100 * buckets['0.10-0.20'] / total_b,
        'pct_buys_below_010': 100 * buckets['0.00-0.10'] / total_b,
        'avg_order_size': sum(r['size'] for r in buys) / max(len(buys), 1),
        'avg_sell_price': sum(sell_prices) / max(len(sell_prices), 1) if sell_prices else 0,
        'buy_price_buckets': {k: 100 * v / total_b for k, v in buckets.items()},
        'candle_stats': candle_stats,
        'both_candles': both_candles,
    }

stats = {}
for name, rows in all_data.items():
    stats[name] = analyze_wallet(name, rows)

# ── Print summary table ─────────────────────────────────────────────────────

print("=" * 110)
print("WALLET BEHAVIOR COMPARISON")
print("=" * 110)

fmt = '{:<12} {:>7} {:>8} {:>9} {:>9} {:>9} {:>10} {:>10} {:>9} {:>9}'
print(fmt.format('Wallet', 'Trades', 'B/S Rat', 'Up/Dn Rat', 'BTC%', 'Both%', 'AvgCombnd', 'Comb<1%', 'AvgBuySz', 'AvgBuyPx'))
print('-' * 110)
for name, s in stats.items():
    print(fmt.format(
        name,
        s['total_trades'],
        f"{s['buy_sell_ratio']:.2f}",
        f"{s['up_dn_ratio']:.2f}",
        f"{s['btc_pct']:.0f}%",
        f"{s['both_sides_pct']:.0f}%",
        f"{s['avg_combined']:.4f}",
        f"{s['pct_combined_lt_1']:.0f}%",
        f"{s['avg_order_size']:.2f}",
        f"{s['avg_buy_price']:.4f}",
    ))

# ── Buy price distribution ──────────────────────────────────────────────────

print()
print("=" * 110)
print("BUY PRICE DISTRIBUTION (% of all buys in each price bucket)")
print("=" * 110)
buckets = ['0.00-0.10','0.10-0.20','0.20-0.30','0.30-0.40','0.40-0.50',
           '0.50-0.60','0.60-0.70','0.70-0.80','0.80-0.90','0.90-1.00']
hdr = f"{'Wallet':<12}" + ''.join(f"{b:>11}" for b in buckets)
print(hdr)
print('-' * 122)
for name, s in stats.items():
    row = f"{name:<12}"
    for b in buckets:
        pct = s['buy_price_buckets'].get(b, 0)
        row += f"{pct:>10.1f}%"
    print(row)

# ── Candle-level patterns ───────────────────────────────────────────────────

print()
print("=" * 110)
print("CANDLE-LEVEL BEHAVIOR")
print("=" * 110)
fmt2 = '{:<12} {:>9} {:>12} {:>14} {:>13} {:>12} {:>12}'
print(fmt2.format('Wallet', 'Candles', 'BothSides%', 'AvgBuys/Cndl', 'AvgSells/Cndl', 'AvgUpPrice', 'AvgDnPrice'))
print('-' * 90)
for name, s in stats.items():
    print(fmt2.format(
        name,
        s['n_candles'],
        f"{s['both_sides_pct']:.1f}%",
        f"{s['avg_buys_per_candle']:.1f}",
        f"{s['avg_sells_per_candle']:.1f}",
        f"{s['avg_up_buy_price']:.4f}",
        f"{s['avg_dn_buy_price']:.4f}",
    ))

# ── Key similarities and differences ───────────────────────────────────────

print()
print("=" * 110)
print("KEY FINDINGS — SIMILARITIES ACROSS WALLETS")
print("=" * 110)

all_stats = list(stats.values())

print()
print("1. BUY PRICE DISTRIBUTION — Where are they buying?")
for name, s in stats.items():
    b = s['buy_price_buckets']
    top_buckets = sorted(b.items(), key=lambda x: x[1], reverse=True)[:3]
    top_str = ', '.join(f"{k}:{v:.1f}%" for k, v in top_buckets)
    print(f"   {name:<12}: {top_str}")

print()
print("2. COMBINED AVG AND WIN RATE (both-sides candles)")
for name, s in stats.items():
    bc = s['both_candles']
    if bc:
        avg_comb = s['avg_combined']
        profitable = sum(1 for c in bc if c['combined'] and c['combined'] < 1.0)
        print(f"   {name:<12}: avg_combined={avg_comb:.4f} | {profitable}/{len(bc)} candles profitable ({100*profitable/len(bc):.0f}%)")

print()
print("3. ORDER SIZE — How much per order?")
for name, s in stats.items():
    print(f"   {name:<12}: avg_order_size={s['avg_order_size']:.2f} shares | avg_buy_px={s['avg_buy_price']:.4f} | avg_usdc/order={s['avg_order_size']*s['avg_buy_price']:.2f}")

print()
print("4. BUY/SELL RATIO — How much do they flip vs hold?")
for name, s in stats.items():
    print(f"   {name:<12}: {s['n_buys']} buys / {s['n_sells']} sells = ratio {s['buy_sell_ratio']:.2f}x | up_dn_ratio={s['up_dn_ratio']:.2f}")

print()
print("5. MARKET SELECTION")
for name, s in stats.items():
    print(f"   {name:<12}: BTC={s['btc_trades']} ({s['btc_pct']:.0f}%) | ETH={s['eth_trades']} ({100-s['btc_pct']:.0f}%)")

# ── Save merged CSV ─────────────────────────────────────────────────────────

print()
print("Saving merged weekday wallet data to all_wallets_weekday.csv ...")
with open('all_wallets_weekday.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['wallet','timestamp','time_utc','side','outcome','price','size','market'])
    for name, rows in all_data.items():
        for r in rows:
            dt = datetime.fromtimestamp(r['ts'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            w.writerow([name, r['ts'], dt, r['side'], r['outcome'], r['price'], r['size'], r['market']])
print("Done.")
