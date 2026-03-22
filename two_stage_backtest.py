"""
Two-Stage Backtest: Wait for first dip, then commit to second side.

Stage 1: Wait for EITHER side to dip below ENTRY_THRESH -> buy it
Stage 2: Watch for second side to dip below SECOND_THRESH within MAX_WAIT_S seconds
         - If second side dips in time  -> buy it, hold both to resolution
         - If second side never dips    -> sell first side at current mid - sell_discount
"""

import sqlite3
import bisect
from collections import defaultdict

DBS = {
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
}
CANDLE_INTERVALS = {'5m': 300, '15m': 900}
SHARES   = 10
MAX_FILLS = 5


def run(db_path, label, entry_thresh, second_thresh, max_wait_s, sell_discount=0.0):
    tf = '15m' if '15m' in label else '5m'
    interval = CANDLE_INTERVALS[tf]
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            'SELECT unix_time,market_id,outcome,ask,mid FROM polymarket_odds '
            'WHERE outcome IN ("Up","Down") AND ask>0 AND mid>0 ORDER BY unix_time ASC'
        ).fetchall()
        conn.close()
    except Exception:
        return None

    candles = defaultdict(lambda: {'Up': [], 'Down': []})
    for ts, mid_id, out, ask, m in rows:
        cs = (int(float(ts)) // interval) * interval
        candles[(cs, mid_id)][out].append({
            'ts': float(ts), 'ask': float(ask), 'mid': float(m)
        })

    results = []

    for (cs, mid_id), sides in candles.items():
        up = sides['Up']
        dn = sides['Down']
        if not up or not dn:
            continue

        fu = up[-1]['mid']
        if fu >= 0.85:
            resolved = 'Up'
        elif fu <= 0.15:
            resolved = 'Down'
        else:
            continue

        up_ts  = [t['ts'] for t in up]
        up_mid = [t['mid'] for t in up]
        dn_ts  = [t['ts'] for t in dn]
        dn_mid = [t['mid'] for t in dn]

        def get_mid(side, ts):
            if side == 'Up':
                idx = bisect.bisect_right(up_ts, ts) - 1
                return up_mid[idx] if idx >= 0 else None
            else:
                idx = bisect.bisect_right(dn_ts, ts) - 1
                return dn_mid[idx] if idx >= 0 else None

        all_ticks = (
            [(t['ts'], 'Up',   t['ask'], t['mid']) for t in up] +
            [(t['ts'], 'Down', t['ask'], t['mid']) for t in dn]
        )
        all_ticks.sort()

        first_side    = None
        first_fill_ts = None
        force_sell_ts = None
        both_filled   = False
        up_fills  = []
        dn_fills  = []
        sold_pnl  = 0.0
        did_sell  = False

        for ts, out, ask, mid in all_ticks:

            # Forced-sell trigger: window expired, second side never showed
            if (force_sell_ts is not None
                    and ts >= force_sell_ts
                    and not both_filled
                    and not did_sell):
                sell_price = max((get_mid(first_side, ts) or 0) - sell_discount, 0)
                fills_ref = up_fills if first_side == 'Up' else dn_fills
                for fp in fills_ref:
                    sold_pnl += (sell_price - fp) * SHARES
                if first_side == 'Up':
                    up_fills = []
                else:
                    dn_fills = []
                did_sell = True
                force_sell_ts = None
                continue

            if did_sell:
                continue  # candle is done, nothing left to do

            # Stage 1: wait for first fill
            if first_side is None:
                if ask <= entry_thresh:
                    if out == 'Up':
                        up_fills.append(ask)
                        first_side    = 'Up'
                        first_fill_ts = ts
                        force_sell_ts = ts + max_wait_s
                    elif out == 'Down':
                        dn_fills.append(ask)
                        first_side    = 'Down'
                        first_fill_ts = ts
                        force_sell_ts = ts + max_wait_s
                continue

            second_side = 'Down' if first_side == 'Up' else 'Up'

            # Accumulate more fills on first side while window is open
            if not both_filled and force_sell_ts is not None and ts < force_sell_ts:
                ref = up_fills if first_side == 'Up' else dn_fills
                if out == first_side and ask <= entry_thresh and len(ref) < MAX_FILLS:
                    ref.append(ask)

            # Second side triggers -> commit
            if out == second_side and ask <= second_thresh:
                if not both_filled:
                    both_filled   = True
                    force_sell_ts = None  # no bail-out needed
                ref2 = up_fills if second_side == 'Up' else dn_fills
                if len(ref2) < MAX_FILLS:
                    ref2.append(ask)

        # Candle ended with open first-side position (window never expired in data)
        if first_side is not None and not both_filled and not did_sell:
            final_ts = all_ticks[-1][0] if all_ticks else cs + interval
            sell_price = max((get_mid(first_side, final_ts) or 0) - sell_discount, 0)
            fills_ref = up_fills if first_side == 'Up' else dn_fills
            for fp in fills_ref:
                sold_pnl += (sell_price - fp) * SHARES
            if first_side == 'Up':
                up_fills = []
            else:
                dn_fills = []

        if not up_fills and not dn_fills and sold_pnl == 0:
            continue

        up_res = sum((1 - p) * SHARES if resolved == 'Up' else -p * SHARES for p in up_fills)
        dn_res = sum((1 - p) * SHARES if resolved == 'Down' else -p * SHARES for p in dn_fills)
        pnl    = up_res + dn_res + sold_pnl

        avg_up = sum(up_fills) / len(up_fills) if up_fills else None
        avg_dn = sum(dn_fills) / len(dn_fills) if dn_fills else None
        comb   = (avg_up + avg_dn) if avg_up and avg_dn else None

        results.append({
            'pnl': pnl, 'both': both_filled, 'combined': comb,
            'n_up': len(up_fills), 'n_dn': len(dn_fills),
            'sold_pnl': sold_pnl,
        })

    if not results:
        return None

    n    = len(results)
    tp   = sum(r['pnl'] for r in results)
    wins = sum(1 for r in results if r['pnl'] > 0)
    bc   = [r for r in results if r['both']]
    oc   = [r for r in results if not r['both']]
    ac   = sum(r['combined'] for r in bc) / len(bc) if bc else None

    return {
        'n': n, 'tp': tp, 'ppc': tp / n, 'wr': 100 * wins / n,
        'bp': 100 * len(bc) / n,
        'ac': ac,
        'bpnl': sum(r['pnl'] for r in bc),
        'opnl': sum(r['pnl'] for r in oc),
        'bn': len(bc), 'on': len(oc),
    }


ENTRY_THRESHOLDS  = [0.25, 0.30, 0.35]
SECOND_THRESHOLDS = [0.35, 0.40, 0.45, 0.50]
MAX_WAITS         = [120, 240, 360, 600]
SELL_DISCOUNTS    = [0.00, 0.05]

MARKETS_15M = [('BTC_15m', DBS['BTC_15m']), ('ETH_15m', DBS['ETH_15m'])]

print("=" * 110)
print("TWO-STAGE BACKTEST  (BTC_15m + ETH_15m combined)")
print("Buy 1st side when ask <= entry_thresh.  Wait max_wait_s for 2nd side at <= second_thresh.")
print("If 2nd never shows: sell 1st at mid - sell_discount.")
print("=" * 110)
print(f"{'Entry':>6} {'2ndThr':>7} {'Wait':>6} {'SellD':>6} "
      f"{'Cndls':>6} {'WR%':>6} {'Both%':>7} {'AvgComb':>9} "
      f"{'BothPnL':>9} {'OnePnL':>9} {'Total':>10} {'PnL/C':>8}")
print('-' * 100)

best = []

for entry in ENTRY_THRESHOLDS:
    for second in SECOND_THRESHOLDS:
        if second < entry:
            continue
        for wait in MAX_WAITS:
            for sd in SELL_DISCOUNTS:
                combined_pnl   = 0
                combined_n     = 0
                combined_bn    = 0
                combined_on    = 0
                combined_bpnl  = 0
                combined_opnl  = 0
                ac_sum         = 0
                ac_cnt         = 0
                wins_sum       = 0

                for label, db in MARKETS_15M:
                    r = run(db, label, entry, second, wait, sd)
                    if not r:
                        continue
                    combined_pnl  += r['tp']
                    combined_n    += r['n']
                    combined_bn   += r['bn']
                    combined_on   += r['on']
                    combined_bpnl += r['bpnl']
                    combined_opnl += r['opnl']
                    if r['ac']:
                        ac_sum += r['ac']
                        ac_cnt += 1
                    wins_sum += r['n'] * r['wr'] / 100

                if not combined_n:
                    continue

                wr   = 100 * wins_sum / combined_n
                ppc  = combined_pnl / combined_n
                bp   = 100 * combined_bn / combined_n
                ac_s = f"{ac_sum/ac_cnt:.4f}" if ac_cnt else '  N/A'
                mk   = ' **' if combined_pnl > 0 else ''

                print(f"{entry:>6.2f} {second:>7.2f} {wait:>6} {sd:>6.2f} "
                      f"{combined_n:>6} {wr:>5.1f}% {bp:>6.1f}% {ac_s:>9} "
                      f"{combined_bpnl:>+9.2f} {combined_opnl:>+9.2f} "
                      f"{combined_pnl:>+10.2f} {ppc:>+8.3f}{mk}")

                if combined_pnl > 0:
                    best.append((combined_pnl, entry, second, wait, sd))

        print()

# ── Summary ──────────────────────────────────────────────────────────────────
if best:
    best.sort(reverse=True)
    print("\n" + "=" * 80)
    print("TOP PROFITABLE CONFIGS:")
    print("=" * 80)
    for tp, entry, second, wait, sd in best[:8]:
        print(f"  Entry<={entry:.2f}  2nd<={second:.2f}  wait={wait}s  sell_disc={sd:.2f}"
              f"  ->  Total={tp:+.2f}")

    # Detail for best config across all 4 markets
    _, entry, second, wait, sd = best[0]
    print(f"\nBest config — all 4 markets: entry<={entry:.2f}, 2nd<={second:.2f},"
          f" wait={wait}s, disc={sd:.2f}")
    print(f"{'Market':<12} {'Cndls':>6} {'WR%':>6} {'Both%':>7} {'AvgComb':>9}"
          f" {'Total':>10} {'PnL/C':>8}")
    print('-' * 62)
    grand = 0
    for label, db in DBS.items():
        r = run(db, label, entry, second, wait, sd)
        if r:
            grand += r['tp']
            acs = f"{r['ac']:.4f}" if r['ac'] else '  N/A'
            print(f"{label:<12} {r['n']:>6} {r['wr']:>5.1f}% {r['bp']:>6.1f}%"
                  f" {acs:>9} {r['tp']:>+10.2f} {r['ppc']:>+8.3f}")
    print(f"{'TOTAL':<12} {'':>6} {'':>6} {'':>7} {'':>9} {grand:>+10.2f}")
else:
    print("\nNo profitable configs found.")

print("\nDone.")
