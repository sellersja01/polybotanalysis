"""
Intra-Candle Scalp Backtest

Strategy:
  1. At candle open, buy both Up and Down at market ask
  2. Throughout candle: sell any side whose mid rises >= PROFIT_TARGET above avg entry
  3. After selling, rebuy that side if mid drops back below REBUY_THRESH
  4. At resolution, remaining shares pay out at $0 or $1

This simulates the buy-low/sell-high cycle wallets appear to run within each candle.
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
}
CANDLE_INTERVALS = {'5m': 300, '15m': 900}
SHARES       = 10      # shares per buy order
MAX_BUYS     = 15      # max total buys per side per candle
OPEN_DELAY_S = 15      # seconds into candle before first buy (avoid candle-open noise)


def run(db_path, label, profit_target, rebuy_thresh, open_buy_price=None):
    """
    profit_target : sell when mid >= avg_entry + profit_target
    rebuy_thresh  : rebuy when mid <= rebuy_thresh  (absolute price, e.g. 0.40)
    open_buy_price: if not None, only open at candle start if ask <= this (None = always open)
    """
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

        # Per-side state
        state = {
            'Up':   {'open': [], 'realised': 0.0, 'total_buys': 0, 'opened': False},
            'Down': {'open': [], 'realised': 0.0, 'total_buys': 0, 'opened': False},
        }

        all_ticks = (
            [(t['ts'], 'Up',   t['ask'], t['mid']) for t in up] +
            [(t['ts'], 'Down', t['ask'], t['mid']) for t in dn]
        )
        all_ticks.sort()

        for ts, out, ask, mid in all_ticks:
            s = state[out]
            time_in_candle = ts - cs

            # ── Opening buy (once per side at candle start) ──────────────────
            if not s['opened'] and time_in_candle >= OPEN_DELAY_S:
                if open_buy_price is None or ask <= open_buy_price:
                    s['open'].append(ask)
                    s['total_buys'] += 1
                    s['opened'] = True

            if not s['opened']:
                continue

            avg_entry = sum(s['open']) / len(s['open']) if s['open'] else None

            # ── Sell trigger: mid has risen >= profit_target above avg entry ─
            if avg_entry is not None and mid >= avg_entry + profit_target and s['open']:
                # Sell all open shares at current mid (bid approx = mid - small spread)
                sell_price = mid - 0.01   # small haircut for bid side
                for ep in s['open']:
                    s['realised'] += (sell_price - ep) * SHARES
                s['open'] = []

            # ── Rebuy trigger: mid dropped below rebuy_thresh ────────────────
            if not s['open'] and s['total_buys'] < MAX_BUYS and ask <= rebuy_thresh:
                s['open'].append(ask)
                s['total_buys'] += 1

        # ── Resolution: open positions pay $0 or $1 ─────────────────────────
        up_res = sum(
            (1.0 - ep) * SHARES if resolved == 'Up' else -ep * SHARES
            for ep in state['Up']['open']
        )
        dn_res = sum(
            (1.0 - ep) * SHARES if resolved == 'Down' else -ep * SHARES
            for ep in state['Down']['open']
        )

        pnl = (state['Up']['realised'] + state['Down']['realised'] + up_res + dn_res)

        total_buys = state['Up']['total_buys'] + state['Down']['total_buys']
        if total_buys == 0:
            continue

        # Track avg entry across ALL buys (to see effective cost basis)
        all_up_buys = state['Up']['total_buys']
        all_dn_buys = state['Down']['total_buys']

        results.append({
            'pnl': pnl,
            'realised': state['Up']['realised'] + state['Down']['realised'],
            'n_up': all_up_buys,
            'n_dn': all_dn_buys,
            'resolved': resolved,
        })

    if not results:
        return None

    n    = len(results)
    tp   = sum(r['pnl'] for r in results)
    wins = sum(1 for r in results if r['pnl'] > 0)
    realised_total = sum(r['realised'] for r in results)
    avg_fills = sum(r['n_up'] + r['n_dn'] for r in results) / n

    return {
        'n': n, 'tp': tp, 'ppc': tp / n,
        'wr': 100 * wins / n,
        'realised': realised_total,
        'avg_fills': avg_fills,
    }


# ── Sweep ─────────────────────────────────────────────────────────────────────
PROFIT_TARGETS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
REBUY_THRESHS  = [0.25, 0.30, 0.35, 0.40, 0.45]
OPEN_PRICES    = [None, 0.55, 0.50]   # None = always open at candle start

MARKETS_15M = [('BTC_15m', DBS['BTC_15m']), ('ETH_15m', DBS['ETH_15m'])]

print("=" * 105)
print("INTRA-CANDLE SCALP BACKTEST  (BTC_15m + ETH_15m combined)")
print("Buy open, sell when mid rises PROFIT_TARGET above avg entry, rebuy when mid <= REBUY_THRESH")
print("=" * 105)
print(f"{'ProfTgt':>8} {'RebuyThr':>9} {'OpenPx':>8} {'Cndls':>6} {'WR%':>6} "
      f"{'Realised':>10} {'Total':>10} {'PnL/C':>8} {'Fills/C':>8}")
print('-' * 80)

best = []
for pt in PROFIT_TARGETS:
    for rb in REBUY_THRESHS:
        for op in OPEN_PRICES:
            combined_pnl  = 0
            combined_n    = 0
            combined_wins = 0
            combined_real = 0
            combined_fills = 0
            for label, db in MARKETS_15M:
                r = run(db, label, pt, rb, op)
                if not r:
                    continue
                combined_pnl   += r['tp']
                combined_n     += r['n']
                combined_wins  += r['n'] * r['wr'] / 100
                combined_real  += r['realised']
                combined_fills += r['n'] * r['avg_fills']
            if not combined_n:
                continue
            wr  = 100 * combined_wins / combined_n
            ppc = combined_pnl / combined_n
            afc = combined_fills / combined_n
            op_s = f'{op:.2f}' if op else ' any'
            mk = ' **' if combined_pnl > 0 else ''
            print(f"{pt:>8.2f} {rb:>9.2f} {op_s:>8} {combined_n:>6} {wr:>5.1f}% "
                  f"{combined_real:>+10.2f} {combined_pnl:>+10.2f} {ppc:>+8.3f} {afc:>8.1f}{mk}")
            if combined_pnl > 0:
                best.append((combined_pnl, pt, rb, op))
    print()

if best:
    best.sort(reverse=True)
    print("\n" + "=" * 80)
    print("TOP PROFITABLE CONFIGS:")
    print("=" * 80)
    for tp, pt, rb, op in best[:8]:
        op_s = f'{op:.2f}' if op else 'any'
        print(f"  profit_target={pt:.2f}  rebuy<={rb:.2f}  open_price<={op_s}"
              f"  ->  Total={tp:+.2f}")

    # Best config across all 4 markets
    _, pt, rb, op = best[0]
    op_s = f'{op:.2f}' if op else 'any'
    print(f"\nBest config — all 4 markets: profit={pt:.2f}, rebuy<={rb:.2f}, open<={op_s}")
    print(f"{'Market':<12} {'Cndls':>6} {'WR%':>6} {'Realised':>10} {'Total':>10} {'PnL/C':>8} {'Fills/C':>8}")
    print('-' * 65)
    grand = 0
    for label, db in DBS.items():
        r = run(db, label, pt, rb, op)
        if r:
            grand += r['tp']
            print(f"{label:<12} {r['n']:>6} {r['wr']:>5.1f}% "
                  f"{r['realised']:>+10.2f} {r['tp']:>+10.2f} "
                  f"{r['ppc']:>+8.3f} {r['avg_fills']:>8.1f}")
    print(f"{'TOTAL':<12} {'':>6} {'':>6} {'':>10} {grand:>+10.2f}")
else:
    print("\nNo profitable configs found.")

print("\nDone.")
