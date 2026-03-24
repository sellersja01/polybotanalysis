"""
Resolve wallet_7 candle winners using BTC 5m OHLCV from Binance.

If BTC close >= open on that 5m candle -> Up wins.
If BTC close <  open                   -> Down wins.

Much simpler than querying Polymarket for each market.
"""

import csv
import re
import json
import time
import sqlite3
import requests
from datetime import datetime, timezone
from collections import defaultdict

WALLET_CSV = 'Wallets_new/wallet_7_merged.csv'
BINANCE_URL = 'https://api.binance.com/api/v3/klines'

DBS = {
    'BTC_5m':  ('databases/market_btc_5m.db',  300),
    'BTC_15m': ('databases/market_btc_15m.db', 900),
}


def parse_candle(title):
    match = re.search(r'(\d+:\d+(?:AM|PM))-(\d+:\d+(?:AM|PM))', title, re.IGNORECASE)
    if not match:
        return None
    fmt = '%I:%M%p'
    try:
        t1 = datetime.strptime(match.group(1).upper(), fmt)
        t2 = datetime.strptime(match.group(2).upper(), fmt)
        diff = int((t2 - t1).total_seconds())
        if diff < 0:
            diff += 86400
        interval = diff
    except Exception:
        return None

    date_match = re.search(r'(\w+ \d+)', title)
    if not date_match:
        return None

    for yr in [2026, 2025]:
        try:
            open_str = f'{date_match.group(1)} {yr} {match.group(1).upper()}'
            naive = datetime.strptime(open_str, '%B %d %Y %I:%M%p')
            ts = int(naive.timestamp()) + 4 * 3600
            break
        except Exception:
            ts = None

    if ts is None:
        return None

    db_key = f"{'BTC' if 'bitcoin' in title.lower() or 'btc' in title.lower() else 'ETH'}_{interval//60}m"
    return ts, interval, db_key


def fetch_btc_candles_from_db(timestamps_sec):
    """
    Use local asset_price table to get open/close price per 5m candle.
    For each candle start ts, open = first price in [ts, ts+300), close = last price.
    Returns dict: {candle_open_ts -> ('Up'/'Down', open_px, close_px)}
    """
    if not timestamps_sec:
        return {}

    results = {}
    conn = sqlite3.connect('databases/market_btc_5m.db')

    for ts in timestamps_sec:
        rows = conn.execute(
            'SELECT unix_time, price FROM asset_price '
            'WHERE unix_time >= ? AND unix_time < ? ORDER BY unix_time',
            (ts, ts + 300)
        ).fetchall()

        if not rows:
            # Try ±5min window to account for parse offset
            for offset in [-300, 300, -600, 600]:
                rows = conn.execute(
                    'SELECT unix_time, price FROM asset_price '
                    'WHERE unix_time >= ? AND unix_time < ? ORDER BY unix_time',
                    (ts + offset, ts + offset + 300)
                ).fetchall()
                if rows:
                    break

        if rows:
            open_px  = float(rows[0][1])
            close_px = float(rows[-1][1])
            winner   = 'Up' if close_px >= open_px else 'Down'
            results[ts] = (winner, open_px, close_px)

    conn.close()
    return results


def load_db_winners():
    winners = {}
    for db_key, (db_path, interval) in DBS.items():
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                'SELECT unix_time, market_id, outcome, mid FROM polymarket_odds '
                'WHERE outcome IN ("Up","Down") AND mid > 0'
            ).fetchall()
            conn.close()
        except Exception:
            continue
        last_up, last_dn = {}, {}
        for ts, mid_id, out, mid in rows:
            cs  = (int(float(ts)) // interval) * interval
            key = (cs, mid_id)
            if out == 'Up':  last_up[key] = float(mid)
            else:            last_dn[key] = float(mid)
        for key in set(last_up) | set(last_dn):
            cs, _ = key
            winners[(db_key, cs)] = 'Up' if last_up.get(key, 0) >= last_dn.get(key, 0) else 'Down'
    return winners


def main():
    print("Loading wallet_7 trades...")
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

    print(f"  {len(buys)} buys, {len(candles)} candles")

    # Load DB winners
    print("Loading local DB winners...")
    db_winners = load_db_winners()

    # Split matched vs unmatched
    matched   = {}
    unmatched = {}

    for title, sides in candles.items():
        parsed = parse_candle(title)
        if parsed is None:
            continue
        ts, interval, db_key = parsed

        winner = None
        for offset in [0, -interval, interval, -300, 300]:
            w = db_winners.get((db_key, ts + offset))
            if w:
                winner = w
                break

        if winner:
            matched[title]   = (ts, interval, db_key, winner, sides)
        else:
            unmatched[title] = (ts, interval, db_key, sides)

    print(f"  DB matched: {len(matched)}, need BTC price: {len(unmatched)}")

    # Fetch BTC candles for all unmatched timestamps in bulk
    if unmatched:
        btc_timestamps = {ts for title, (ts, interval, db_key, sides) in unmatched.items()}
        print(f"\nResolving {len(btc_timestamps)} candles from local asset_price DB...")
        btc_candles = fetch_btc_candles_from_db(btc_timestamps)
        print(f"  Got {len(btc_candles)} candles resolved")

    # Resolve unmatched via BTC price
    btc_resolved  = {}
    still_missing = {}

    for title, (ts, interval, db_key, sides) in unmatched.items():
        result = btc_candles.get(ts)

        if result:
            winner, open_px, close_px = result
            btc_resolved[title] = (ts, interval, db_key, winner, sides, open_px, close_px)
        else:
            still_missing[title] = (ts, interval, db_key, sides)

    print(f"  BTC resolved: {len(btc_resolved)}, still missing: {len(still_missing)}")

    # Compute PnL for all resolved candles
    results = []

    for title, (ts, interval, db_key, winner, sides) in matched.items():
        up_shares  = sum(float(r['size']) for r in sides['Up'])
        dn_shares  = sum(float(r['size']) for r in sides['Down'])
        up_cost    = sum(float(r['usdc']) for r in sides['Up'])
        dn_cost    = sum(float(r['usdc']) for r in sides['Down'])
        total_cost = up_cost + dn_cost
        revenue    = up_shares if winner == 'Up' else dn_shares
        pnl        = revenue - total_cost
        results.append({
            'market': title, 'source': 'db', 'winner': winner,
            'up_shares': up_shares, 'dn_shares': dn_shares,
            'total_cost': total_cost, 'revenue': revenue, 'pnl': pnl, 'win': pnl > 0,
        })

    for title, (ts, interval, db_key, winner, sides, open_px, close_px) in btc_resolved.items():
        up_shares  = sum(float(r['size']) for r in sides['Up'])
        dn_shares  = sum(float(r['size']) for r in sides['Down'])
        up_cost    = sum(float(r['usdc']) for r in sides['Up'])
        dn_cost    = sum(float(r['usdc']) for r in sides['Down'])
        total_cost = up_cost + dn_cost
        revenue    = up_shares if winner == 'Up' else dn_shares
        pnl        = revenue - total_cost
        results.append({
            'market': title, 'source': 'btc', 'winner': winner,
            'open': open_px, 'close': close_px,
            'up_shares': up_shares, 'dn_shares': dn_shares,
            'total_cost': total_cost, 'revenue': revenue, 'pnl': pnl, 'win': pnl > 0,
        })

    # Summary
    n          = len(results)
    wins       = sum(1 for r in results if r['win'])
    net_pnl    = sum(r['pnl'] for r in results)
    total_cost = sum(r['total_cost'] for r in results)
    total_rev  = sum(r['revenue'] for r in results)
    win_pnls   = [r['pnl'] for r in results if r['win']]
    loss_pnls  = [r['pnl'] for r in results if not r['win']]

    print(f"\n{'='*62}")
    print(f"  WALLET_7 FULL PnL  (DB + BTC price resolution)")
    print(f"{'='*62}")
    print(f"  Candles analyzed   : {n}  ({len(matched)} DB / {len(btc_resolved)} BTC price / {len(still_missing)} missing)")
    print(f"  Wins               : {wins} ({wins/n*100:.1f}%)")
    print(f"  Losses             : {n-wins} ({(n-wins)/n*100:.1f}%)")
    print(f"  Total cost (USDC)  : ${total_cost:>12,.2f}")
    print(f"  Total revenue      : ${total_rev:>12,.2f}")
    print(f"  Net PnL            : ${net_pnl:>+12,.2f}")
    print(f"  ROI                : {100*net_pnl/total_cost:.2f}%")
    print(f"  Avg PnL/candle     : ${net_pnl/n:>+10,.2f}")
    print(f"  Avg cost/candle    : ${total_cost/n:>10,.2f}")
    if win_pnls:  print(f"  Avg win            : ${sum(win_pnls)/len(win_pnls):>+10,.2f}")
    if loss_pnls: print(f"  Avg loss           : ${sum(loss_pnls)/len(loss_pnls):>+10,.2f}")

    # Source breakdown
    db_sub  = [r for r in results if r['source'] == 'db']
    btc_sub = [r for r in results if r['source'] == 'btc']
    print(f"\n  --- Source check (DB vs BTC price should agree) ---")
    if db_sub:
        dn = len(db_sub); dw = sum(1 for r in db_sub if r['win']); dnet = sum(r['pnl'] for r in db_sub)
        print(f"  DB  : {dn:3d} candles | WR {dw/dn*100:.1f}% | Net ${dnet:>+10,.2f}")
    if btc_sub:
        bn = len(btc_sub); bw = sum(1 for r in btc_sub if r['win']); bnet = sum(r['pnl'] for r in btc_sub)
        print(f"  BTC : {bn:3d} candles | WR {bw/bn*100:.1f}% | Net ${bnet:>+10,.2f}")

    print(f"\n  --- Top 5 worst candles ---")
    for r in sorted(results, key=lambda x: x['pnl'])[:5]:
        print(f"  {r['pnl']:>+9.2f}  [{r['source']}]  {r['market']}")

    print(f"\n  --- Top 5 best candles ---")
    for r in sorted(results, key=lambda x: x['pnl'])[-5:]:
        print(f"  {r['pnl']:>+9.2f}  [{r['source']}]  {r['market']}")

    if still_missing:
        print(f"\n  --- {len(still_missing)} candles with no resolution (too recent?) ---")
        for title in sorted(still_missing)[:5]:
            print(f"    {title}")


if __name__ == '__main__':
    main()
