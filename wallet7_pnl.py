"""
Wallet_7 Total PnL Calculator

Cross-references wallet_7_merged.csv buy trades with market DBs
to determine which side won each candle, then computes total PnL.

Rules:
  - BUY trades: cost = usdc column (already price * size)
  - Winner = side with higher mid at last observed tick in DB
  - If winner == Up: revenue = up_shares * $1.00
  - If winner == Down: revenue = dn_shares * $1.00
  - Fee already priced in (we paid ask, not mid)
  - SELL trades counted as revenue directly
"""

import csv
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

WALLET_CSV = 'Wallets_new/wallet_7_merged.csv'

DBS = {
    'BTC_5m':  ('databases/market_btc_5m.db',  300),
    'BTC_15m': ('databases/market_btc_15m.db', 900),
    'ETH_5m':  ('databases/market_eth_5m.db',  300),
}


def parse_candle_open_utc(market_title):
    """
    Parse the candle open time from title like:
    "Bitcoin Up or Down - March 22, 6:10PM-6:15PM ET"
    Returns (asset, interval_secs, candle_open_unix_utc) or None.
    """
    title = market_title.lower()

    if 'bitcoin' in title or 'btc' in title:
        asset = 'BTC'
    elif 'ethereum' in title or 'eth' in title:
        asset = 'ETH'
    else:
        return None

    # Extract time range e.g. "6:10PM-6:15PM"
    match = re.search(r'(\d+:\d+(?:AM|PM))-(\d+:\d+(?:AM|PM))', market_title, re.IGNORECASE)
    if not match:
        return None

    fmt = '%I:%M%p'
    try:
        t1 = datetime.strptime(match.group(1).upper(), fmt)
        t2 = datetime.strptime(match.group(2).upper(), fmt)
        diff = int((t2 - t1).total_seconds())
        if diff < 0:
            diff += 86400
        interval = diff  # 300 or 900
    except Exception:
        return None

    # Extract date e.g. "March 22"
    date_match = re.search(r'(\w+ \d+)', market_title)
    if not date_match:
        return None

    # We need the year — infer from market_title or use current year context
    # Titles span 2025-2026; use 2026 as base, fallback 2025
    date_str = date_match.group(1)
    open_str = f"{date_str} 2026 {match.group(1).upper()}"
    try:
        naive = datetime.strptime(open_str, '%B %d %Y %I:%M%p')
    except Exception:
        try:
            open_str = f"{date_str} 2025 {match.group(1).upper()}"
            naive = datetime.strptime(open_str, '%B %d %Y %I:%M%p')
        except Exception:
            return None

    # ET = UTC-4 (EDT) — convert to UTC by adding 4 hours
    candle_open_utc = int(naive.timestamp()) + 4 * 3600

    if asset == 'BTC':
        db_key = f'BTC_{interval//60}m'
    else:
        db_key = f'ETH_{interval//60}m'

    return asset, interval, candle_open_utc, db_key


def load_market_winners():
    """
    Load all candle winners from all DBs.
    Returns dict: {(db_key, candle_start_unix) -> winner_side}
    """
    winners = {}
    last_mids = {}  # (db_key, candle_start, market_id) -> last_up_mid

    for db_key, (db_path, interval) in DBS.items():
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                'SELECT unix_time, market_id, outcome, mid FROM polymarket_odds '
                'WHERE outcome IN ("Up","Down") AND mid > 0 ORDER BY unix_time ASC'
            ).fetchall()
            conn.close()
        except Exception as e:
            print(f"  Warning: could not load {db_key}: {e}")
            continue

        # Group last mid by (candle_start, market_id)
        last_up = {}
        last_dn = {}
        for ts, mid_id, out, mid in rows:
            ts = float(ts)
            cs = (int(ts) // interval) * interval
            key = (cs, mid_id)
            if out == 'Up':
                last_up[key] = float(mid)
            else:
                last_dn[key] = float(mid)

        for key in set(last_up) | set(last_dn):
            cs, mid_id = key
            up_mid = last_up.get(key, 0)
            dn_mid = last_dn.get(key, 0)
            winner = 'Up' if up_mid >= dn_mid else 'Down'
            winners[(db_key, cs)] = winner  # may overwrite — last market_id wins; fine for matching

        print(f"  {db_key}: {len(set(k[0] for k in last_up))} candle starts loaded")

    return winners


def main():
    print("\nLoading market winner data from DBs...")
    winners = load_market_winners()
    print(f"  Total candle timestamps cached: {len(winners)}")

    # Load wallet_7 trades
    buys = []
    sells = []
    with open(WALLET_CSV, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['side'].upper() == 'BUY':
                buys.append(row)
            elif row['side'].upper() == 'SELL':
                sells.append(row)

    print(f"\nWallet_7 trades: {len(buys)} buys, {len(sells)} sells")

    # Group buys by market title
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for row in buys:
        out = row['outcome'].strip().capitalize()
        if out in ('Up', 'Down'):
            candles[row['market']][out].append(row)

    print(f"Unique candles (buy side): {len(candles)}")

    # For each candle, compute PnL
    results = []
    unmatched = 0
    no_db_data = 0

    for market_title, sides in candles.items():
        parsed = parse_candle_open_utc(market_title)
        if parsed is None:
            unmatched += 1
            continue

        asset, interval, candle_open_utc, db_key = parsed

        # Try to find winner — look in a 10-minute window around candle_open
        # (account for small parse errors)
        winner = None
        for offset in [0, -interval, interval, -300, 300]:
            cs_try = candle_open_utc + offset
            w = winners.get((db_key, cs_try))
            if w:
                winner = w
                break

        if winner is None:
            no_db_data += 1
            continue

        up_rows = sides['Up']
        dn_rows = sides['Down']

        up_shares = sum(float(r['size']) for r in up_rows)
        dn_shares = sum(float(r['size']) for r in dn_rows)
        up_cost   = sum(float(r['usdc']) for r in up_rows)
        dn_cost   = sum(float(r['usdc']) for r in dn_rows)
        total_cost = up_cost + dn_cost

        if winner == 'Up':
            revenue = up_shares * 1.0
        else:
            revenue = dn_shares * 1.0

        pnl = revenue - total_cost

        results.append({
            'market': market_title,
            'db_key': db_key,
            'winner': winner,
            'up_shares': up_shares,
            'dn_shares': dn_shares,
            'up_cost': up_cost,
            'dn_cost': dn_cost,
            'total_cost': total_cost,
            'revenue': revenue,
            'pnl': pnl,
            'win': pnl > 0,
        })

    # Also count sell revenue (early exits or manual sells)
    total_sell_revenue = sum(float(r['usdc']) for r in sells)

    print(f"\nCandles parsed:      {len(results)}")
    print(f"  Unmatched titles:  {unmatched}")
    print(f"  No DB data:        {no_db_data}")
    print(f"  Sells found:       {len(sells)} (total USDC: ${total_sell_revenue:.2f})")

    if not results:
        print("No results to analyze.")
        return

    n = len(results)
    wins = sum(1 for r in results if r['win'])
    losses = n - wins
    net_pnl = sum(r['pnl'] for r in results)
    total_cost = sum(r['total_cost'] for r in results)
    total_rev = sum(r['revenue'] for r in results)

    win_pnls  = [r['pnl'] for r in results if r['win']]
    loss_pnls = [r['pnl'] for r in results if not r['win']]

    print(f"\n{'='*60}")
    print(f"  WALLET_7 PnL SUMMARY")
    print(f"{'='*60}")
    print(f"  Candles analyzed   : {n}")
    print(f"  Wins               : {wins} ({wins/n*100:.1f}%)")
    print(f"  Losses             : {losses} ({losses/n*100:.1f}%)")
    print(f"  Total cost (USDC)  : ${total_cost:,.2f}")
    print(f"  Total revenue      : ${total_rev:,.2f}")
    print(f"  Net PnL (buy/sell) : ${net_pnl:+,.2f}")
    print(f"  ROI                : {100*net_pnl/total_cost:.2f}%")
    print(f"  Avg PnL/candle     : ${net_pnl/n:+,.2f}")
    print(f"  Avg win            : ${sum(win_pnls)/len(win_pnls):+,.2f}" if win_pnls else "")
    print(f"  Avg loss           : ${sum(loss_pnls)/len(loss_pnls):+,.2f}" if loss_pnls else "")
    print(f"  Avg cost/candle    : ${total_cost/n:,.2f}")

    # By market
    print(f"\n  --- By market ---")
    for db_key in ['BTC_5m', 'BTC_15m', 'ETH_5m']:
        sub = [r for r in results if r['db_key'] == db_key]
        if not sub:
            continue
        sn = len(sub)
        sw = sum(1 for r in sub if r['win'])
        snet = sum(r['pnl'] for r in sub)
        scost = sum(r['total_cost'] for r in sub)
        print(f"  {db_key:>10}: {sn:4d} candles | WR {sw/sn*100:.1f}% | Net ${snet:+,.2f} | ROI {100*snet/scost:.2f}%")

    # Largest wins/losses
    results_sorted = sorted(results, key=lambda r: r['pnl'])
    print(f"\n  --- Top 5 worst candles ---")
    for r in results_sorted[:5]:
        print(f"  {r['pnl']:+8.2f}  {r['market']}")

    print(f"\n  --- Top 5 best candles ---")
    for r in results_sorted[-5:]:
        print(f"  {r['pnl']:+8.2f}  {r['market']}")


if __name__ == '__main__':
    main()
