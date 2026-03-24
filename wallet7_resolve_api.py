"""
Fetch wallet_7 candle resolutions directly from Polymarket API.

For the 164 candles with no local DB data, we query:
  https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{timestamp}

Resolved markets return outcomePrices = ["1","0"] or ["0","1"],
telling us exactly which side won.
"""

import csv
import re
import json
import time
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

WALLET_CSV  = 'Wallets_new/wallet_7_merged.csv'
API_BASE    = 'https://gamma-api.polymarket.com/events'
DELAY       = 0.25   # seconds between API calls (be polite)

DBS = {
    'BTC_5m':  ('databases/market_btc_5m.db',  300),
    'BTC_15m': ('databases/market_btc_15m.db', 900),
}


# ── helpers ──────────────────────────────────────────────────────

def parse_candle(title):
    """
    Parse market title -> (asset_slug, interval, candle_open_unix, db_key)
    e.g. "Bitcoin Up or Down - March 22, 8:00PM-8:05PM ET"
    """
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
    date_str = date_match.group(1)

    for yr in [2026, 2025]:
        try:
            open_str = f'{date_str} {yr} {match.group(1).upper()}'
            naive = datetime.strptime(open_str, '%B %d %Y %I:%M%p')
            ts = int(naive.timestamp()) + 4 * 3600
            break
        except Exception:
            ts = None

    if ts is None:
        return None

    asset_slug = 'btc' if 'bitcoin' in title.lower() or 'btc' in title.lower() else 'eth'
    tf         = f'{interval // 60}m'
    db_key     = f'{asset_slug.upper()}_{tf}'
    slug       = f'{asset_slug}-updown-{tf}-{ts}'

    return slug, interval, ts, db_key


def fetch_resolution(slug):
    """
    Query Polymarket gamma API for a slug.
    Returns ('Up','Down', or None if unresolved/not found), and market_id.
    """
    try:
        r = requests.get(f'{API_BASE}?slug={slug}', timeout=10)
        data = r.json()
        if not data:
            return None, None

        event  = data[0]
        market = event['markets'][0]
        market_id = str(market.get('id', ''))
        closed = market.get('closed', False)
        outcome_prices_raw = market.get('outcomePrices', '[]')

        try:
            prices = json.loads(outcome_prices_raw)
        except Exception:
            prices = []

        if not closed or len(prices) < 2:
            return None, market_id

        # prices[0] = Up token final price, prices[1] = Down token final price
        up_price = float(prices[0])
        dn_price = float(prices[1])

        if up_price > 0.5:
            return 'Up', market_id
        elif dn_price > 0.5:
            return 'Down', market_id
        else:
            return None, market_id   # still settling

    except Exception as e:
        return None, None


# ── load local DB winners (reuse from wallet7_pnl.py) ────────────

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
            if out == 'Up':
                last_up[key] = float(mid)
            else:
                last_dn[key] = float(mid)
        for key in set(last_up) | set(last_dn):
            cs, _ = key
            winner = 'Up' if last_up.get(key, 0) >= last_dn.get(key, 0) else 'Down'
            winners[(db_key, cs)] = winner
    return winners


# ── main ─────────────────────────────────────────────────────────

def main():
    print("\nLoading wallet_7 trades...")
    buys = []
    with open(WALLET_CSV, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['side'].upper() == 'BUY' and row['market'].strip():
                buys.append(row)

    # Group by candle title
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for row in buys:
        out = row['outcome'].strip().capitalize()
        if out in ('Up', 'Down'):
            candles[row['market'].strip()][out].append(row)

    print(f"  {len(buys)} buys across {len(candles)} candles")

    # Load local DB winners
    print("\nLoading local DB winners...")
    db_winners = load_db_winners()
    print(f"  {len(db_winners)} candle timestamps in DB")

    # Split: DB-matched vs needs API
    db_results  = []
    api_needed  = []

    for market_title, sides in candles.items():
        parsed = parse_candle(market_title)
        if parsed is None:
            continue
        slug, interval, ts, db_key = parsed

        # Try DB first
        winner = None
        for offset in [0, -interval, interval, -300, 300]:
            w = db_winners.get((db_key, ts + offset))
            if w:
                winner = w
                break

        entry = {
            'market': market_title,
            'db_key': db_key,
            'slug':   slug,
            'ts':     ts,
            'sides':  sides,
        }

        if winner:
            entry['winner'] = winner
            entry['source'] = 'db'
            db_results.append(entry)
        else:
            api_needed.append(entry)

    print(f"\nDB matched : {len(db_results)}")
    print(f"Need API   : {len(api_needed)}")

    # Query API for unmatched
    api_results  = []
    still_missing = 0

    print(f"\nQuerying Polymarket API for {len(api_needed)} candles...")
    for i, entry in enumerate(api_needed):
        slug = entry['slug']
        winner, market_id = fetch_resolution(slug)

        if winner:
            entry['winner'] = winner
            entry['source'] = 'api'
            api_results.append(entry)
            status = f"OK  -> {winner}"
        else:
            still_missing += 1
            status = "no resolution"

        if (i + 1) % 10 == 0 or i == len(api_needed) - 1:
            print(f"  [{i+1}/{len(api_needed)}] {slug} : {status}")

        time.sleep(DELAY)

    all_results = db_results + api_results
    print(f"\nTotal resolved: {len(all_results)} ({len(db_results)} DB + {len(api_results)} API)")
    print(f"Still missing : {still_missing}")

    # Compute PnL
    results = []
    for entry in all_results:
        sides   = entry['sides']
        winner  = entry['winner']
        up_rows = sides['Up']
        dn_rows = sides['Down']

        up_shares  = sum(float(r['size']) for r in up_rows)
        dn_shares  = sum(float(r['size']) for r in dn_rows)
        up_cost    = sum(float(r['usdc']) for r in up_rows)
        dn_cost    = sum(float(r['usdc']) for r in dn_rows)
        total_cost = up_cost + dn_cost

        revenue = up_shares if winner == 'Up' else dn_shares
        pnl     = revenue - total_cost

        results.append({
            'market':     entry['market'],
            'db_key':     entry['db_key'],
            'source':     entry['source'],
            'winner':     winner,
            'up_shares':  up_shares,
            'dn_shares':  dn_shares,
            'up_cost':    up_cost,
            'dn_cost':    dn_cost,
            'total_cost': total_cost,
            'revenue':    revenue,
            'pnl':        pnl,
            'win':        pnl > 0,
        })

    if not results:
        print("No results.")
        return

    n          = len(results)
    wins       = sum(1 for r in results if r['win'])
    net_pnl    = sum(r['pnl'] for r in results)
    total_cost = sum(r['total_cost'] for r in results)
    total_rev  = sum(r['revenue'] for r in results)
    win_pnls   = [r['pnl'] for r in results if r['win']]
    loss_pnls  = [r['pnl'] for r in results if not r['win']]

    print(f"\n{'='*62}")
    print(f"  WALLET_7 FULL PnL SUMMARY")
    print(f"{'='*62}")
    print(f"  Candles analyzed   : {n}")
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

    print(f"\n  --- By market ---")
    for db_key in ['BTC_5m', 'BTC_15m', 'ETH_5m']:
        sub = [r for r in results if r['db_key'] == db_key]
        if not sub:
            continue
        sn   = len(sub)
        sw   = sum(1 for r in sub if r['win'])
        snet = sum(r['pnl'] for r in sub)
        sc   = sum(r['total_cost'] for r in sub)
        print(f"  {db_key:>10}: {sn:4d} candles | WR {sw/sn*100:.1f}% | Net ${snet:>+10,.2f} | ROI {100*snet/sc:.2f}%")

    print(f"\n  --- Top 5 worst candles ---")
    for r in sorted(results, key=lambda x: x['pnl'])[:5]:
        print(f"  {r['pnl']:>+9.2f}  [{r['source']:3s}]  {r['market']}")

    print(f"\n  --- Top 5 best candles ---")
    for r in sorted(results, key=lambda x: x['pnl'])[-5:]:
        print(f"  {r['pnl']:>+9.2f}  [{r['source']:3s}]  {r['market']}")


if __name__ == '__main__':
    main()
