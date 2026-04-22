"""
Backtest of live_s13_v3 — streaming per-candle to stay under memory budget.
Adds 6c slippage per entry; replays bot's exact check() logic.
"""
import sqlite3, time, os
from datetime import datetime, timezone
from collections import defaultdict

INTERVAL          = 300
MOVE_THRESH       = 0.03
SLIPPAGE          = 0.06
SHARES_PER_TRADE  = 100   # fixed size model: always buy 100 shares (cost varies with price)
MIN_AGE           = 10
MAX_AGE           = INTERVAL - 30
MAX_MID           = 0.55
MAX_ASK           = 0.75
ASSETS = ['btc','eth','sol','xrp']

def backtest_asset(asset):
    # Read directly from local snapshot DBs (no lock risk, they aren't being written to)
    db_path = os.path.join(os.path.dirname(__file__), 'databases', f'market_{asset}_5m.db')
    c = sqlite3.connect(db_path)
    # Make sure there's an index on unix_time (for fast range queries)
    try: c.execute('CREATE INDEX IF NOT EXISTS idx_ap_time ON asset_price(unix_time)')
    except: pass
    try: c.execute('CREATE INDEX IF NOT EXISTS idx_po_time ON polymarket_odds(unix_time)')
    except: pass
    c.commit()

    # Get candle range
    row = c.execute('SELECT MIN(unix_time), MAX(unix_time) FROM asset_price').fetchone()
    if not row or row[0] is None:
        c.close(); os.remove(snap)
        return {'asset': asset, 'trades': [], 'candles_total': 0, 'candles_with_data': 0}
    t_start, t_end = float(row[0]), float(row[1])
    first_candle = (int(t_start) // INTERVAL) * INTERVAL
    last_candle  = (int(t_end)   // INTERVAL) * INTERVAL

    trades = []
    candles_total = 0
    candles_with_data = 0

    cs = first_candle
    while cs <= last_candle:
        ce = cs + INTERVAL
        candles_total += 1

        # Load CB ticks for this candle only
        cb_rows = c.execute(
            'SELECT unix_time, price FROM asset_price WHERE unix_time >= ? AND unix_time < ? ORDER BY unix_time',
            (cs, ce)
        ).fetchall()
        if not cb_rows:
            cs = ce; continue
        cb = [(float(t), float(p)) for t, p in cb_rows if float(p) > 0]
        if not cb:
            cs = ce; continue

        candle_open = cb[0][1]

        # Load Poly rows for this candle only (both sides)
        poly_rows = c.execute(
            "SELECT unix_time, outcome, bid, ask FROM polymarket_odds "
            "WHERE unix_time >= ? AND unix_time < ? AND outcome IN ('Up','Down') ORDER BY unix_time",
            (cs, ce)
        ).fetchall()
        poly_up = [(float(t), float(b), float(a)) for t, o, b, a in poly_rows if o == 'Up']
        poly_dn = [(float(t), float(b), float(a)) for t, o, b, a in poly_rows if o == 'Down']
        if not poly_up or not poly_dn:
            cs = ce; continue
        candles_with_data += 1

        # Determine winner
        _, ub_final, ua_final = poly_up[-1]
        _, db_final, da_final = poly_dn[-1]
        if ua_final == 0 and da_final > 0:
            winner = 'Up'
        elif da_final == 0 and ua_final > 0:
            winner = 'Down'
        else:
            um = (ub_final + ua_final) / 2
            dm = (db_final + da_final) / 2
            winner = 'Up' if um >= dm else 'Down'

        def latest_at(rows, ts):
            # Binary search
            lo, hi = 0, len(rows)
            while lo < hi:
                m = (lo + hi) // 2
                if rows[m][0] <= ts: lo = m + 1
                else: hi = m
            return rows[lo-1] if lo > 0 else None

        # Simulate: walk CB ticks, fire on first qualifying
        for t, p in cb:
            age = t - cs
            if age < MIN_AGE: continue
            if age > MAX_AGE: break
            move_pct = (p - candle_open) / candle_open * 100
            if abs(move_pct) < MOVE_THRESH: continue
            direction = 'Up' if move_pct > 0 else 'Down'
            row = latest_at(poly_up if direction == 'Up' else poly_dn, t)
            if row is None: continue
            _, bid, ask = row
            mid = (bid + ask) / 2
            if mid <= 0 or mid > MAX_MID: continue
            if ask <= 0 or ask >= MAX_ASK: continue
            effective_fill = ask + SLIPPAGE
            if effective_fill >= 1.0: continue
            cost = SHARES_PER_TRADE * effective_fill
            pnl = (SHARES_PER_TRADE * 1.0 - cost) if direction == winner else (0.0 - cost)
            trades.append({
                'candle_ts': cs, 'asset': asset.upper(),
                'direction': direction, 'log_ask': ask,
                'effective_fill': effective_fill, 'shares': SHARES_PER_TRADE, 'cost': cost,
                'winner': winner, 'pnl': pnl, 'age': age, 'move_pct': move_pct,
            })
            break  # one entry per candle

        cs = ce

    c.close()
    return {'asset': asset, 'trades': trades,
            'candles_total': candles_total, 'candles_with_data': candles_with_data}

def summarize(results):
    print(f'\n{"="*72}')
    print(f'  Backtest: live_s13_v3 logic — {MOVE_THRESH}% CEX move, {SHARES_PER_TRADE} shares/trade, {int(SLIPPAGE*100)}c slippage')
    print(f'{"="*72}\n')
    all_trades = []
    for r in results: all_trades.extend(r['trades'])
    if not all_trades:
        print('No trades found.'); return

    t0 = min(t['candle_ts'] for t in all_trades)
    t1 = max(t['candle_ts'] for t in all_trades)
    days = (t1 - t0) / 86400
    print(f'Time range: {datetime.fromtimestamp(t0,tz=timezone.utc).strftime("%Y-%m-%d %H:%M")} to {datetime.fromtimestamp(t1,tz=timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC  ({days:.1f} days)')

    print(f'\n{"Asset":<6} {"Candles":>8} {"WithData":>10} {"Trades":>7} {"Wins":>6} {"WR%":>6} {"AvgWin$":>9} {"AvgLoss$":>9} {"Avg $/tr":>10} {"Total $":>10}')
    for r in results:
        a = r['asset'].upper(); tr = r['trades']
        if not tr:
            print(f'{a:<6} {r["candles_total"]:>8} {r["candles_with_data"]:>10} {"0":>7}'); continue
        wins = [t for t in tr if t['pnl'] > 0]
        losses = [t for t in tr if t['pnl'] <= 0]
        aw = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
        al = sum(t['pnl'] for t in losses)/len(losses) if losses else 0
        total = sum(t['pnl'] for t in tr)
        avg_tr = total/len(tr)
        wr = 100.0 * len(wins) / len(tr)
        print(f'{a:<6} {r["candles_total"]:>8} {r["candles_with_data"]:>10} {len(tr):>7} {len(wins):>6} {wr:>5.1f}% {aw:>9.2f} {al:>9.2f} {avg_tr:>10.2f} {total:>10.2f}')

    wins = [t for t in all_trades if t['pnl'] > 0]
    losses = [t for t in all_trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in all_trades)
    wr = 100.0 * len(wins)/len(all_trades)
    aw = sum(t['pnl'] for t in wins)/len(wins) if wins else 0
    al = sum(t['pnl'] for t in losses)/len(losses) if losses else 0
    avg_tr = total_pnl / len(all_trades)
    daily = total_pnl / days if days > 0 else 0
    print(f'\n{"TOTAL":<6} {"":>8} {"":>10} {len(all_trades):>7} {len(wins):>6} {wr:>5.1f}% {aw:>9.2f} {al:>9.2f} {avg_tr:>10.2f} {total_pnl:>10.2f}')
    total_deployed = sum(t['cost'] for t in all_trades)
    print(f'\nDaily PnL at {SHARES_PER_TRADE} shares/trade: ${daily:.2f}/day')
    print(f'Total capital deployed across {len(all_trades)} trades: ${total_deployed:,.2f}')
    print(f'Avg capital per trade: ${total_deployed/len(all_trades):.2f}')
    print(f'Return on deployed capital: {total_pnl/total_deployed*100:.2f}%')

    print(f'\n--- By entry-price band (effective fill incl. {int(SLIPPAGE*100)}c slippage) ---')
    bands = [(0, 0.10), (0.10, 0.25), (0.25, 0.45), (0.45, 0.55), (0.55, 0.70), (0.70, 0.82)]
    for lo, hi in bands:
        bt = [t for t in all_trades if lo <= t['effective_fill'] < hi]
        if not bt: continue
        bw = [t for t in bt if t['pnl'] > 0]
        wr = 100.0 * len(bw) / len(bt)
        total = sum(t['pnl'] for t in bt)
        avg = total / len(bt)
        print(f'  fill {int(lo*100):>2}c-{int(hi*100):>2}c:  {len(bt):>5} trades  WR={wr:>5.1f}%  avg=${avg:+.2f}  total=${total:+.2f}')

if __name__ == '__main__':
    results = []
    for a in ASSETS:
        print(f'Running {a.upper()}...', flush=True)
        r = backtest_asset(a)
        results.append(r)
        print(f'  {a.upper()}: {r["candles_total"]} candles, {r["candles_with_data"]} with data, {len(r["trades"])} trades', flush=True)
    summarize(results)
