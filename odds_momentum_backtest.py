"""
Odds-Momentum Signal Backtest

Signal: Buy side X when its mid price has DROPPED by >= DROP_THRESHOLD in the last LOOKBACK seconds.
This replicates what the profitable wallets are actually doing: buying into dips in the odds price.

Also tests combining with BTC price momentum as a second filter.
"""

import sqlite3
import bisect
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
SHARES      = 10
MAX_BUYS    = 8          # max buys per side per candle
COOLDOWN_S  = 20         # seconds between buys on same side

# Signal params to sweep
LOOKBACKS         = [30, 60, 120]   # how far back to measure the drop
DROP_THRESHOLDS   = [0.02, 0.03, 0.04, 0.05, 0.07, 0.10]  # mid must drop by this much
MAX_ENTRY_PRICE   = [0.50, 0.40, 0.35]   # only buy if mid <= this value
BTC_MOM_FILTERS   = [None, 0.0001, 0.0002]  # require BTC moving against us (contrarian)


def load_btc_prices(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute('SELECT unix_time, price FROM asset_price ORDER BY unix_time').fetchall()
    conn.close()
    return [r[0] for r in rows], [r[1] for r in rows]

def btc_momentum(btc_times, btc_prices, ts, lookback_s):
    idx_now = bisect.bisect_right(btc_times, ts) - 1
    idx_old = bisect.bisect_right(btc_times, ts - lookback_s) - 1
    if idx_now < 0 or idx_old < 0:
        return None
    p_now = btc_prices[idx_now]
    p_old = btc_prices[idx_old]
    if p_old == 0:
        return None
    return (p_now - p_old) / p_old


def run_backtest(db_path, label, lookback_s, drop_thresh, max_price,
                 btc_filter_thresh, btc_times=None, btc_prices=None):
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
    except Exception:
        return None

    # Group by candle
    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, market_id, outcome, bid, ask, mid in rows:
        candle_start = (int(float(ts)) // interval) * interval
        candles[(candle_start, market_id)][outcome].append({
            'ts': float(ts), 'ask': float(ask), 'mid': float(mid),
        })

    results = []

    for (candle_start, market_id), sides in candles.items():
        up_ticks = sides['Up']
        dn_ticks = sides['Down']
        if not up_ticks or not dn_ticks:
            continue

        # Determine resolution
        final_up = up_ticks[-1]['mid']
        if final_up >= 0.85:
            resolved = 'Up'
        elif final_up <= 0.15:
            resolved = 'Down'
        else:
            continue

        # Build sorted time series for each side (for lookback queries)
        up_ts_series  = [t['ts'] for t in up_ticks]
        up_mid_series = [t['mid'] for t in up_ticks]
        dn_ts_series  = [t['ts'] for t in dn_ticks]
        dn_mid_series = [t['mid'] for t in dn_ticks]

        def mid_lookback(ts_series, mid_series, ts, lb):
            """Return mid price at ts - lb seconds, or None."""
            idx = bisect.bisect_right(ts_series, ts - lb) - 1
            if idx < 0:
                return None
            return mid_series[idx]

        # Merge ticks
        all_ticks = [(t['ts'], 'Up',   t['ask'], t['mid']) for t in up_ticks] + \
                    [(t['ts'], 'Down', t['ask'], t['mid']) for t in dn_ticks]
        all_ticks.sort()

        up_fills = []
        dn_fills = []
        last_up_buy = -999
        last_dn_buy = -999

        for ts, outcome, ask, mid in all_ticks:
            # ── Signal: mid has dropped by >= drop_thresh in last lookback_s ──
            if outcome == 'Up':
                mid_lb = mid_lookback(up_ts_series, up_mid_series, ts, lookback_s)
            else:
                mid_lb = mid_lookback(dn_ts_series, dn_mid_series, ts, lookback_s)

            if mid_lb is None:
                continue

            drop = mid_lb - mid   # positive = mid has fallen (good: we want to buy cheap)

            if drop < drop_thresh:
                continue
            if mid > max_price:
                continue

            # ── BTC contrarian filter (optional) ──
            if btc_filter_thresh is not None and btc_times is not None:
                btc_mom = btc_momentum(btc_times, btc_prices, ts, 60)
                if btc_mom is None:
                    pass  # no filter if no data
                else:
                    # For Up buy: want BTC falling (btc_mom < -btc_filter_thresh) = contrarian
                    # For Down buy: want BTC rising (btc_mom > +btc_filter_thresh) = contrarian
                    if outcome == 'Up' and btc_mom > -btc_filter_thresh:
                        continue
                    if outcome == 'Down' and btc_mom < btc_filter_thresh:
                        continue

            # ── Execute buy ──
            if outcome == 'Up' and len(up_fills) < MAX_BUYS:
                if ts - last_up_buy >= COOLDOWN_S:
                    up_fills.append(ask)
                    last_up_buy = ts
            elif outcome == 'Down' and len(dn_fills) < MAX_BUYS:
                if ts - last_dn_buy >= COOLDOWN_S:
                    dn_fills.append(ask)
                    last_dn_buy = ts

        if not up_fills and not dn_fills:
            continue

        # ── Resolve ──
        up_pnl = sum((1.0 - p) * SHARES if resolved == 'Up' else (0.0 - p) * SHARES
                     for p in up_fills)
        dn_pnl = sum((1.0 - p) * SHARES if resolved == 'Down' else (0.0 - p) * SHARES
                     for p in dn_fills)
        pnl = up_pnl + dn_pnl

        avg_up = sum(up_fills) / len(up_fills) if up_fills else None
        avg_dn = sum(dn_fills) / len(dn_fills) if dn_fills else None
        both = avg_up is not None and avg_dn is not None
        combined = (avg_up + avg_dn) if both else None

        results.append({
            'pnl': pnl, 'both': both, 'combined': combined,
            'n_up': len(up_fills), 'n_dn': len(dn_fills),
            'resolved': resolved,
        })

    if not results:
        return None

    total_pnl = sum(r['pnl'] for r in results)
    n = len(results)
    wins = sum(1 for r in results if r['pnl'] > 0)
    both_candles = [r for r in results if r['both']]
    avg_combined = (sum(r['combined'] for r in both_candles) / len(both_candles)
                    if both_candles else None)

    return {
        'label': label, 'n_candles': n,
        'total_pnl': total_pnl, 'pnl_per_candle': total_pnl / n,
        'win_pct': 100 * wins / n,
        'both_pct': 100 * len(both_candles) / n,
        'avg_combined': avg_combined,
    }


# ── Load BTC prices for filter ────────────────────────────────────────────────
print("Loading BTC price data...")
btc_times, btc_prices = load_btc_prices(DBS['BTC_15m'])

# ── Part 1: Sweep drop_threshold x lookback, no BTC filter ───────────────────
print("\n" + "="*100)
print("PART 1: Odds-drop signal sweep — no BTC filter  (BTC_15m + ETH_15m)")
print("="*100)
print(f"{'Lookback':>9} {'Drop':>7} {'MaxPx':>7} {'Cndls':>6} {'WR%':>6} {'Both%':>7} {'AvgComb':>9} {'NetPnL':>10} {'PnL/C':>8}")
print('-'*80)

best_results = []
for lb in LOOKBACKS:
    for dt in DROP_THRESHOLDS:
        for mp in MAX_ENTRY_PRICE:
            combined_pnl = 0
            combined_n   = 0
            all_ok = True
            line_parts = []
            for label, db_path in [('BTC_15m', DBS['BTC_15m']), ('ETH_15m', DBS['ETH_15m'])]:
                r = run_backtest(db_path, label, lb, dt, mp, None)
                if not r:
                    all_ok = False
                    break
                combined_pnl += r['total_pnl']
                combined_n   += r['n_candles']
                line_parts.append(r)
            if not all_ok or not line_parts:
                continue
            marker = ' **' if combined_pnl > 0 else ''
            avg_comb_str = (f"{sum(r['avg_combined'] for r in line_parts if r['avg_combined']) / len(line_parts):.4f}"
                            if any(r['avg_combined'] for r in line_parts) else 'N/A')
            ppc = combined_pnl / max(combined_n, 1)
            wr  = sum(r['win_pct'] for r in line_parts) / len(line_parts)
            bp  = sum(r['both_pct'] for r in line_parts) / len(line_parts)
            print(f"{lb:>9}s {dt:>7.2f} {mp:>7.2f} {combined_n:>6} {wr:>5.1f}% {bp:>6.1f}% "
                  f"{avg_comb_str:>9} {combined_pnl:>+10.2f} {ppc:>+8.3f}{marker}")
            if combined_pnl > 0:
                best_results.append((combined_pnl, lb, dt, mp))

# ── Part 2: BTC filter on best combos ────────────────────────────────────────
print("\n" + "="*100)
print("PART 2: Add BTC contrarian filter — top 3 configs from Part 1")
print("="*100)

best_results.sort(reverse=True)
top_configs = best_results[:3]

if top_configs:
    print(f"{'Lookback':>9} {'Drop':>7} {'MaxPx':>7} {'BTCFilt':>9} {'Cndls':>6} {'WR%':>6} {'NetPnL':>10} {'PnL/C':>8}")
    print('-'*70)
    for _, lb, dt, mp in top_configs:
        for btcf in BTC_MOM_FILTERS:
            combined_pnl = 0
            combined_n   = 0
            all_ok = True
            for label, db_path in [('BTC_15m', DBS['BTC_15m']), ('ETH_15m', DBS['ETH_15m'])]:
                r = run_backtest(db_path, label, lb, dt, mp, btcf,
                                 btc_times, btc_prices)
                if not r:
                    all_ok = False
                    break
                combined_pnl += r['total_pnl']
                combined_n   += r['n_candles']
            if not all_ok:
                continue
            btcf_str = 'None' if btcf is None else f'{btcf:.4f}'
            ppc = combined_pnl / max(combined_n, 1)
            marker = ' **' if combined_pnl > 0 else ''
            print(f"{lb:>9}s {dt:>7.2f} {mp:>7.2f} {btcf_str:>9} {combined_n:>6} "
                  f"{combined_pnl:>+10.2f} {ppc:>+8.3f}{marker}")
        print()
else:
    print("  No profitable configs found in Part 1")

# ── Part 3: All 4 markets with best config ────────────────────────────────────
print("\n" + "="*100)
print("PART 3: Best config across all 4 markets")
print("="*100)
if best_results:
    _, lb, dt, mp = best_results[0]
    print(f"Config: lookback={lb}s, drop>={dt:.2f}, max_price<={mp:.2f}")
    print(f"{'Market':<12} {'Cndls':>6} {'WR%':>6} {'Both%':>7} {'AvgComb':>9} {'NetPnL':>10} {'PnL/C':>8}")
    print('-'*60)
    grand_total = 0
    for label, db_path in DBS.items():
        r = run_backtest(db_path, label, lb, dt, mp, None)
        if r:
            grand_total += r['total_pnl']
            comb_str = f"{r['avg_combined']:.4f}" if r['avg_combined'] else 'N/A'
            print(f"{label:<12} {r['n_candles']:>6} {r['win_pct']:>5.1f}% "
                  f"{r['both_pct']:>6.1f}% {comb_str:>9} {r['total_pnl']:>+10.2f} "
                  f"{r['pnl_per_candle']:>+8.3f}")
    print(f"{'TOTAL':<12} {'':>6} {'':>6} {'':>7} {'':>9} {grand_total:>+10.2f}")

print("\nDone.")
