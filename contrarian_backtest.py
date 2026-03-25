"""
Contrarian threshold backtest.

Rule: Within each candle, whenever a side's mid price drops below THRESHOLD,
buy it at the current ask. Do this for both sides independently.
At resolution, collect $1 on the winning side.

Tests multiple thresholds and buy frequencies to find the best params.
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}

CANDLE_INTERVALS = {'5m': 300, '15m': 900}

# Strategy params to sweep
THRESHOLDS    = [0.25, 0.30, 0.35, 0.40, 0.45]
SHARES        = 10          # shares per buy order
MAX_BUYS_SIDE = 10          # max buys per side per candle
COOLDOWN_S    = 15          # seconds between buys on same side (avoid hammering)
STOP_LOSS     = 0.10        # sell out if position drops this far below avg entry price


def run_backtest(db_path, label, threshold, max_buys, cooldown, stop_loss=None):
    tf = '15m' if '15m' in label else '5m'
    interval = CANDLE_INTERVALS[tf]

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute('''
            SELECT unix_time, market_id, outcome, bid, ask, mid
            FROM polymarket_odds
            WHERE outcome IN ('Up','Down') AND ask > 0 AND mid > 0
            ORDER BY unix_time ASC
        ''').fetchall()
        conn.close()
    except Exception as e:
        return None

    # Group by candle
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, market_id, outcome, bid, ask, mid in rows:
        candle_start = (int(float(ts)) // interval) * interval
        key = (candle_start, market_id)
        candles[key][outcome].append({
            'ts': float(ts),
            'ask': float(ask),
            'mid': float(mid),
        })

    results = []

    for (candle_start, market_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        # Determine resolution — use ALL candles, winner = highest mid at last tick
        final_up = up_ticks[-1]['mid']
        resolved = 'Up' if final_up >= 0.5 else 'Down'

        # Simulate: scan ticks in time order, buy when side drops below threshold
        up_fills = []   # list of ask prices paid
        dn_fills = []

        last_up_buy_ts = -999
        last_dn_buy_ts = -999

        # Merge and sort all ticks by time
        all_ticks = [(t['ts'], 'Up',   t['ask'], t['mid']) for t in up_ticks] + \
                    [(t['ts'], 'Down', t['ask'], t['mid']) for t in dn_ticks]
        all_ticks.sort()

        # Track open positions per side: list of (entry_ask, shares)
        up_open   = []   # open Up positions
        dn_open   = []   # open Down positions
        up_closed_pnl = 0.0  # realised PnL from stop-loss exits
        dn_closed_pnl = 0.0
        up_fills  = []   # all fill prices (for avg tracking)
        dn_fills  = []

        for ts, outcome, ask, mid in all_ticks:
            # ── Stop-loss check: sell out if mid fell stop_loss below avg entry ──
            if stop_loss is not None:
                if up_open:
                    avg_entry = sum(p for p, s in up_open) / len(up_open)
                    # get current Up mid
                    if outcome == 'Up' and mid < avg_entry - stop_loss:
                        # sell all Up at current mid (pessimistic exit)
                        exit_price = mid
                        for ep, sh in up_open:
                            up_closed_pnl += (exit_price - ep) * sh
                        up_open = []
                if dn_open:
                    avg_entry = sum(p for p, s in dn_open) / len(dn_open)
                    if outcome == 'Down' and mid < avg_entry - stop_loss:
                        exit_price = mid
                        for ep, sh in dn_open:
                            dn_closed_pnl += (exit_price - ep) * sh
                        dn_open = []

            # ── Buy trigger ──
            if mid <= threshold:
                if outcome == 'Up' and len(up_fills) < max_buys:
                    if ts - last_up_buy_ts >= cooldown:
                        up_fills.append(ask)
                        up_open.append((ask, SHARES))
                        last_up_buy_ts = ts
                elif outcome == 'Down' and len(dn_fills) < max_buys:
                    if ts - last_dn_buy_ts >= cooldown:
                        dn_fills.append(ask)
                        dn_open.append((ask, SHARES))
                        last_dn_buy_ts = ts

        if not up_fills and not dn_fills:
            continue

        # ── Resolve open positions at candle end ──
        # remaining open positions pay out at resolution
        up_open_pnl = sum((1.0 - ep) * sh if resolved == 'Up' else (0.0 - ep) * sh
                          for ep, sh in up_open)
        dn_open_pnl = sum((1.0 - ep) * sh if resolved == 'Down' else (0.0 - ep) * sh
                          for ep, sh in dn_open)

        pnl = up_closed_pnl + dn_closed_pnl + up_open_pnl + dn_open_pnl

        avg_up = sum(up_fills) / len(up_fills) if up_fills else None
        avg_dn = sum(dn_fills) / len(dn_fills) if dn_fills else None
        both   = avg_up is not None and avg_dn is not None

        results.append({
            'candle_start': candle_start,
            'resolved': resolved,
            'avg_up': avg_up, 'avg_dn': avg_dn,
            'n_up_fills': len(up_fills), 'n_dn_fills': len(dn_fills),
            'combined': (avg_up + avg_dn) if avg_up and avg_dn else None,
            'pnl': pnl,
            'both': both,
        })

    if not results:
        return None

    total_pnl   = sum(r['pnl'] for r in results)
    wins        = sum(1 for r in results if r['pnl'] > 0)
    losses      = sum(1 for r in results if r['pnl'] <= 0)
    both_candles = [r for r in results if r['both']]
    one_sided    = [r for r in results if not r['both']]
    avg_combined = sum(r['combined'] for r in both_candles) / max(len(both_candles), 1)
    both_pnl     = sum(r['pnl'] for r in both_candles)
    one_pnl      = sum(r['pnl'] for r in one_sided)

    return {
        'label': label, 'threshold': threshold,
        'n_candles': len(results),
        'wins': wins, 'losses': losses,
        'win_pct': 100 * wins / max(wins + losses, 1),
        'total_pnl': total_pnl,
        'both_candles': len(both_candles),
        'both_pct': 100 * len(both_candles) / max(len(results), 1),
        'avg_combined': avg_combined,
        'both_pnl': both_pnl,
        'one_pnl': one_pnl,
        'pnl_per_candle': total_pnl / max(len(results), 1),
    }


print("CONTRARIAN THRESHOLD BACKTEST — WITH STOP-LOSS")
print(f"Shares/buy={SHARES} | Max buys/side={MAX_BUYS_SIDE} | Cooldown={COOLDOWN_S}s")
print(f"Buy rule: when mid < THRESHOLD buy at ask | Stop-loss: exit if mid drops STOP below avg entry")
print()

# ── Part 1: No stop-loss vs stop-loss comparison ────────────────────────────
STOP_LOSSES = [None, 0.05, 0.10, 0.15, 0.20]

print("=" * 110)
print("PART 1: Stop-loss sweep at threshold=0.35, BTC_5m + ETH_15m")
print("=" * 110)
print(f"{'StopLoss':>10} {'Market':<12} {'Cndls':>6} {'WR%':>6} {'Both%':>7} {'NetPnL':>10} {'PnL/Cndl':>10}")
print('-' * 70)
for sl in STOP_LOSSES:
    sl_label = 'None' if sl is None else f'{sl:.2f}'
    sl_total = 0
    for label, db_path in [('BTC_5m', DBS['BTC_5m']), ('ETH_15m', DBS['ETH_15m'])]:
        r = run_backtest(db_path, label, 0.35, MAX_BUYS_SIDE, COOLDOWN_S, sl)
        if not r: continue
        sl_total += r['total_pnl']
        marker = ' *' if r['total_pnl'] > 0 else ''
        print(f"{sl_label:>10} {label:<12} {r['n_candles']:>6} {r['win_pct']:>5.1f}% "
              f"{r['both_pct']:>6.1f}% {r['total_pnl']:>+10.2f} {r['pnl_per_candle']:>+10.3f}{marker}")
    print(f"{'':>10} {'TOTAL':<12} {'':>6} {'':>6} {'':>7} {sl_total:>+10.2f}")
    print()

# ── Part 2: Full sweep with best stop-loss ──────────────────────────────────
print("=" * 110)
print("PART 2: Full threshold sweep WITH stop-loss=0.10 across all markets")
print("=" * 110)
hdr = f"{'Thresh':>8} {'Market':<12} {'Cndls':>6} {'WR%':>6} {'Both%':>7} {'AvgComb':>9} {'NetPnL':>10} {'PnL/Cndl':>10}"
print(hdr)
print('-' * 80)

all_results = []
for threshold in THRESHOLDS:
    thresh_total = 0
    for label, db_path in DBS.items():
        r = run_backtest(db_path, label, threshold, MAX_BUYS_SIDE, COOLDOWN_S, stop_loss=0.10)
        if not r: continue
        all_results.append(r)
        thresh_total += r['total_pnl']
        marker = ' **PROFIT**' if r['total_pnl'] > 0 else ''
        print(f"{threshold:>8.2f} {label:<12} {r['n_candles']:>6} {r['win_pct']:>5.1f}% "
              f"{r['both_pct']:>6.1f}% {r['avg_combined']:>9.4f} {r['total_pnl']:>+10.2f} "
              f"{r['pnl_per_candle']:>+10.3f}{marker}")
    print(f"{'':>8} {'TOTAL':<12} {'':>6} {'':>6} {'':>7} {'':>9} {thresh_total:>+10.2f}")
    print()

# ── Part 3: Fine-tune stop-loss on most promising combo ─────────────────────
print("=" * 80)
print("PART 3: Fine-tune stop-loss x threshold (BTC_15m + ETH_15m combined)")
print("=" * 80)
print(f"{'Thresh':>8} {'StopLoss':>10} {'BTC15_PnL':>12} {'ETH15_PnL':>12} {'Combined':>10}")
print('-' * 56)
for thresh in [0.25, 0.30, 0.35, 0.40]:
    for sl in [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]:
        rb = run_backtest(DBS['BTC_15m'], 'BTC_15m', thresh, MAX_BUYS_SIDE, COOLDOWN_S, sl)
        re = run_backtest(DBS['ETH_15m'], 'ETH_15m', thresh, MAX_BUYS_SIDE, COOLDOWN_S, sl)
        if rb and re:
            combo = rb['total_pnl'] + re['total_pnl']
            marker = ' << PROFIT' if combo > 0 else ''
            print(f"{thresh:>8.2f} {sl:>10.2f} {rb['total_pnl']:>+12.2f} {re['total_pnl']:>+12.2f} {combo:>+10.2f}{marker}")
    print()
