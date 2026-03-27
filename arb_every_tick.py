"""
arb_every_tick.py — Deploy $100 on EVERY profitable tick
=========================================================
Every time the net gap > 0 (cost + fees < $1), deploy $100.
Shows total PnL across all scenarios.
"""

import sqlite3
from datetime import datetime, timezone

DB = 'databases/arb_collector.db'
conn = sqlite3.connect(DB)

def poly_fee_current(p): return p * 0.25 * (p * (1 - p)) ** 2
def poly_fee_new(p):     return p * 0.072 * (p * (1 - p)) ** 1
def kalshi_taker(p):     return 0.07 * p * (1 - p)
def kalshi_maker(p):     return 0.0

rows = conn.execute("""
    SELECT ts, asset, candle_id,
           p_up_ask, p_dn_ask, k_up_ask, k_dn_ask
    FROM snapshots
    WHERE p_up_bid > 0 AND p_up_ask > 0 AND p_dn_bid > 0 AND p_dn_ask > 0
      AND k_up_bid > 0 AND k_up_ask > 0 AND k_dn_bid > 0 AND k_dn_ask > 0
      AND p_up_ask < 0.95 AND p_dn_ask < 0.95
      AND k_up_ask < 0.95 AND k_dn_ask < 0.95
    ORDER BY ts
""").fetchall()

r = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
hours = (r[1] - r[0]) / 3600
conn.close()

print(f"{'='*70}")
print(f"  DEPLOY $100 ON EVERY PROFITABLE TICK")
print(f"  {len(rows):,} active ticks over {hours:.1f} hours")
print(f"{'='*70}")

scenarios = [
    ("A) Taker+Taker (now)",         poly_fee_current, kalshi_taker),
    ("B) Poly Tkr + Kalshi Mkr",     poly_fee_current, kalshi_maker),
    ("C) Taker+Taker (Mar 30)",      poly_fee_new,     kalshi_taker),
    ("D) New Poly Tkr + Kalshi Mkr", poly_fee_new,     kalshi_maker),
]

for name, pf, kf in scenarios:
    total_pnl = 0.0
    total_deployed = 0.0
    n_trades = 0

    for ts, asset, candle, pua, pda, kua, kda in rows:
        # Dir A: Poly Up + Kalshi Down
        cost_a = pua + kda
        fee_a = pf(pua) + kf(kda)
        net_a = 1.0 - cost_a - fee_a

        # Dir B: Poly Down + Kalshi Up
        cost_b = pda + kua
        fee_b = pf(pda) + kf(kua)
        net_b = 1.0 - cost_b - fee_b

        best_net = max(net_a, net_b)
        best_cost = cost_a + fee_a if net_a >= net_b else cost_b + fee_b

        if best_net > 0:
            contracts = 100.0 / best_cost  # $100 deployed
            pnl = contracts * best_net
            total_pnl += pnl
            total_deployed += 100.0
            n_trades += 1

    per_hour = total_pnl / hours if hours else 0
    per_day = per_hour * 24
    avg_pnl = total_pnl / n_trades if n_trades else 0
    roi = total_pnl / total_deployed * 100 if total_deployed else 0

    print(f"\n  {name}")
    print(f"    Profitable ticks   : {n_trades:>10,}")
    print(f"    Total deployed     : ${total_deployed:>14,.2f}")
    print(f"    Total PnL          : ${total_pnl:>14,.2f}")
    print(f"    ROI                : {roi:>10.2f}%")
    print(f"    Avg PnL per trade  : ${avg_pnl:>10.2f}")
    print(f"    PnL / hour         : ${per_hour:>10,.2f}")
    print(f"    PnL / day (extrap) : ${per_day:>10,.2f}")

print(f"\n{'='*70}")
print(f"  NOTE: This assumes infinite liquidity and no price impact.")
print(f"  Real execution would fill far fewer trades and move the market.")
print(f"{'='*70}")
