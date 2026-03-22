"""
Continuous Buy Backtest — replicate the screenshot candle behavior.

Key observation from screenshot:
  - Bot buys BOTH sides continuously throughout candle at prevailing prices
  - Fixed USDC per buy (not fixed shares) — so cheap prices = more shares accumulated
  - UP fell from 0.45 -> 0.06, avg came out 0.17 (bought heavily cheap)
  - DOWN rose from 0.44 -> 0.90, avg came out 0.69 (bought at market throughout)
  - BOTH outcomes profitable because UP avg so low it more than offsets DOWN losses

Strategy:
  Every BUY_INTERVAL seconds, spend BUY_USDC on each side at current ask.
  No price filter — just buy both sides continuously.
  At resolution, collect $1 per winning share.
"""

import sqlite3
from collections import defaultdict

DBS = {
    'BTC_5m':  r'C:\Users\James\polybotanalysis\market_btc_5m.db',
    'BTC_15m': r'C:\Users\James\polybotanalysis\market_btc_15m.db',
    'ETH_5m':  r'C:\Users\James\polybotanalysis\market_eth_5m.db',
    'ETH_15m': r'C:\Users\James\polybotanalysis\market_eth_15m.db',
}
CANDLE_INTERVALS = {'5m': 300, '15m': 900}


def run(db_path, label, buy_interval_s, buy_usdc):
    """
    buy_interval_s : seconds between buys on each side
    buy_usdc       : dollars spent per buy (buys buy_usdc / ask shares)
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

        # Simulate: every buy_interval_s, spend buy_usdc on each side
        up_shares_total = 0.0
        up_cost_total   = 0.0
        dn_shares_total = 0.0
        dn_cost_total   = 0.0

        last_up_buy = -999
        last_dn_buy = -999

        all_ticks = (
            [(t['ts'], 'Up',   t['ask']) for t in up] +
            [(t['ts'], 'Down', t['ask']) for t in dn]
        )
        all_ticks.sort()

        for ts, out, ask in all_ticks:
            if ask <= 0:
                continue
            if out == 'Up' and ts - last_up_buy >= buy_interval_s:
                shares = buy_usdc / ask
                up_shares_total += shares
                up_cost_total   += buy_usdc
                last_up_buy = ts
            elif out == 'Down' and ts - last_dn_buy >= buy_interval_s:
                shares = buy_usdc / ask
                dn_shares_total += shares
                dn_cost_total   += buy_usdc
                last_dn_buy = ts

        if up_shares_total == 0 or dn_shares_total == 0:
            continue

        avg_up = up_cost_total  / up_shares_total
        avg_dn = dn_cost_total  / dn_shares_total

        up_pnl = up_shares_total * (1.0 - avg_up) if resolved == 'Up' \
                 else up_shares_total * (0.0 - avg_up)
        dn_pnl = dn_shares_total * (1.0 - avg_dn) if resolved == 'Down' \
                 else dn_shares_total * (0.0 - avg_dn)
        pnl = up_pnl + dn_pnl

        total_cost = up_cost_total + dn_cost_total

        # Would profit BOTH ways? (both pnl scenarios positive)
        pnl_if_up   = up_shares_total*(1-avg_up) + dn_shares_total*(0-avg_dn)
        pnl_if_down = up_shares_total*(0-avg_up) + dn_shares_total*(1-avg_dn)
        guaranteed = pnl_if_up > 0 and pnl_if_down > 0

        results.append({
            'pnl': pnl,
            'pnl_if_up': pnl_if_up,
            'pnl_if_down': pnl_if_down,
            'guaranteed': guaranteed,
            'avg_up': avg_up, 'avg_dn': avg_dn,
            'combined': avg_up + avg_dn,
            'up_cost': up_cost_total, 'dn_cost': dn_cost_total,
            'total_cost': total_cost,
            'up_shares': up_shares_total, 'dn_shares': dn_shares_total,
        })

    if not results:
        return None

    n    = len(results)
    tp   = sum(r['pnl'] for r in results)
    wins = sum(1 for r in results if r['pnl'] > 0)
    guar = sum(1 for r in results if r['guaranteed'])
    avg_combined = sum(r['combined'] for r in results) / n
    avg_cost     = sum(r['total_cost'] for r in results) / n
    avg_up_p     = sum(r['avg_up'] for r in results) / n
    avg_dn_p     = sum(r['avg_dn'] for r in results) / n

    return {
        'n': n, 'tp': tp, 'ppc': tp / n,
        'wr': 100 * wins / n,
        'guar_pct': 100 * guar / n,
        'avg_combined': avg_combined,
        'avg_cost': avg_cost,
        'avg_up': avg_up_p,
        'avg_dn': avg_dn_p,
        'roi_pct': 100 * tp / sum(r['total_cost'] for r in results),
    }


# ── Sweep ─────────────────────────────────────────────────────────────────────
BUY_INTERVALS = [10, 15, 20, 30, 45, 60]   # seconds between buys per side
BUY_USDCS     = [1, 5, 10]                  # USDC per buy (scale-neutral, results scale linearly)

MARKETS_15M = [('BTC_15m', DBS['BTC_15m']), ('ETH_15m', DBS['ETH_15m'])]

print("=" * 110)
print("CONTINUOUS BUY BACKTEST — spend fixed USDC per buy on BOTH sides every N seconds")
print("No price filter. Buy at market ask throughout entire candle.")
print("BTC_15m + ETH_15m combined")
print("=" * 110)
print(f"{'Interval':>9} {'USDC/buy':>9} {'Cndls':>6} {'WR%':>6} {'Guar%':>7} "
      f"{'AvgUp':>7} {'AvgDn':>7} {'AvgComb':>9} {'AvgCost':>9} "
      f"{'Total':>10} {'PnL/C':>8} {'ROI%':>7}")
print('-' * 105)

best = []
for iv in BUY_INTERVALS:
    for usdc in BUY_USDCS:
        combined_pnl  = 0; combined_n = 0; combined_wins = 0
        combined_guar = 0; combined_cost = 0
        comb_sum = 0; up_sum = 0; dn_sum = 0
        for label, db in MARKETS_15M:
            r = run(db, label, iv, usdc)
            if not r: continue
            combined_pnl  += r['tp']
            combined_n    += r['n']
            combined_wins += r['n'] * r['wr'] / 100
            combined_guar += r['n'] * r['guar_pct'] / 100
            combined_cost += r['n'] * r['avg_cost']
            comb_sum      += r['n'] * r['avg_combined']
            up_sum        += r['n'] * r['avg_up']
            dn_sum        += r['n'] * r['avg_dn']
        if not combined_n: continue
        wr   = 100 * combined_wins / combined_n
        guar = 100 * combined_guar / combined_n
        ppc  = combined_pnl / combined_n
        ac   = comb_sum / combined_n
        au   = up_sum / combined_n
        ad   = dn_sum / combined_n
        roi  = 100 * combined_pnl / combined_cost if combined_cost else 0
        avg_c = combined_cost / combined_n
        mk = ' **' if combined_pnl > 0 else ''
        print(f"{iv:>9}s {usdc:>9} {combined_n:>6} {wr:>5.1f}% {guar:>6.1f}% "
              f"{au:>7.4f} {ad:>7.4f} {ac:>9.4f} ${avg_c:>8.2f} "
              f"{combined_pnl:>+10.2f} {ppc:>+8.3f} {roi:>6.2f}%{mk}")
        if combined_pnl > 0:
            best.append((combined_pnl, iv, usdc))
    print()

# ── All 4 markets with best config ───────────────────────────────────────────
if best:
    best.sort(reverse=True)
    print("\n" + "=" * 90)
    print("TOP CONFIGS:")
    for tp, iv, usdc in best[:6]:
        print(f"  interval={iv}s  usdc/buy={usdc}  ->  total={tp:+.2f} (scales linearly with USDC)")

    _, iv, usdc = best[0]
    print(f"\nBest config — all 4 markets: interval={iv}s, ${usdc}/buy")
    print(f"{'Market':<12} {'Cndls':>6} {'WR%':>6} {'Guar%':>7} {'AvgUp':>7} {'AvgDn':>7} "
          f"{'AvgComb':>9} {'Total':>10} {'PnL/C':>8} {'ROI%':>7}")
    print('-' * 80)
    grand = 0
    for label, db in DBS.items():
        r = run(db, label, iv, usdc)
        if r:
            grand += r['tp']
            print(f"{label:<12} {r['n']:>6} {r['wr']:>5.1f}% {r['guar_pct']:>6.1f}% "
                  f"{r['avg_up']:>7.4f} {r['avg_dn']:>7.4f} "
                  f"{r['avg_combined']:>9.4f} {r['tp']:>+10.2f} "
                  f"{r['ppc']:>+8.3f} {r['roi_pct']:>6.2f}%")
    print(f"{'TOTAL':<12} {'':>6} {'':>6} {'':>7} {'':>7} {'':>7} {'':>9} {grand:>+10.2f}")

    # Scale to real money
    _, iv, usdc = best[0]
    print(f"\nScale projection (interval={iv}s, BTC+ETH 15m):")
    r_b = run(DBS['BTC_15m'], 'BTC_15m', iv, usdc)
    r_e = run(DBS['ETH_15m'], 'ETH_15m', iv, usdc)
    if r_b and r_e:
        candles_per_day = (r_b['n'] + r_e['n']) / 20
        ppc_base = (r_b['ppc'] + r_e['ppc']) / 2
        cost_base = (r_b['avg_cost'] + r_e['avg_cost']) / 2
        print(f"  Candles/day: {candles_per_day:.1f} | Base PnL/candle @${usdc}/buy: ${ppc_base:.2f} | Base cost: ${cost_base:.2f}")
        print()
        print(f"  {'$/buy':>8} {'Cost/candle':>13} {'PnL/candle':>12} {'Daily PnL':>12} {'Annual':>13}")
        print(f"  {'-'*60}")
        for scale_usdc in [1, 5, 10, 25, 50, 100, 250, 500]:
            mult = scale_usdc / usdc
            pnl_c = ppc_base * mult
            cap_c = cost_base * mult
            daily = pnl_c * candles_per_day
            annual = daily * 252
            print(f"  ${scale_usdc:>7} ${cap_c:>12.2f} ${pnl_c:>11.2f} ${daily:>11.2f} ${annual:>12.0f}")
else:
    print("\nNo profitable configs found.")

print("\nDone.")
