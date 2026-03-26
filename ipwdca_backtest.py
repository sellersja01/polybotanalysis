"""
Inverse-Price-Weighted Continuous DCA (IPWDCA) Backtest
========================================================
Strategy:
  Every TICK_INTERVAL seconds during a candle, buy shares of BOTH sides
  proportional to (1 - price):
      shares = BASE_NOTIONAL * (1 - mid) / mid   (so spend ≈ BASE_NOTIONAL * (1-mid) dollars)

  Actually simpler: spend BASE_NOTIONAL dollars, buy BASE_NOTIONAL / ask shares.
  But weight the spend by (1 - mid) so we spend more on cheaper sides.

  Specifically each tick:
      spend_up   = BASE * (1 - up_mid)
      spend_down = BASE * (1 - dn_mid)
      shares_up   = spend_up   / up_ask
      shares_dn   = spend_down / dn_ask

At resolution:
  winner (higher final mid) pays $1/share
  loser pays $0/share

Fees: fee = shares * price * 0.25 * (price * (1 - price))^2  (taker)

100% of candles — no filtering.
"""

import sqlite3
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
DB            = 'databases/market_btc_5m.db'
TICK_INTERVAL = 30        # seconds between buys
BASE          = 1.0       # base notional per tick per side (scales linearly)
MIN_TICKS     = 3         # skip candle if fewer ticks than this (data gap)
MIN_DIVERGENCE = 0.10     # only buy when |up_mid - dn_mid| >= this (one side <= 0.45)

def fee(shares, price):
    return shares * price * 0.25 * (price * (1 - price)) ** 2

def run_backtest():
    conn = sqlite3.connect(DB)

    print("Loading data...", flush=True)

    # Add index if missing for fast grouping
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_time ON polymarket_odds(market_id, unix_time)")
    conn.commit()

    # Load everything at once, sorted
    rows_all = conn.execute("""
        SELECT market_id, unix_time, outcome, bid, ask, mid
        FROM polymarket_odds
        ORDER BY market_id, unix_time
    """).fetchall()

    # Group by market_id in Python
    from itertools import groupby
    from operator import itemgetter
    markets_data = {}
    for market_id, group in groupby(rows_all, key=itemgetter(0)):
        markets_data[market_id] = list(group)

    markets = list(markets_data.keys())
    print(f"Loaded {len(rows_all):,} rows across {len(markets)} candles.\n")

    print(f"\n{'='*65}")
    print(f"  IPWDCA BACKTEST — BTC 5m  ({len(markets)} candles)")
    print(f"  TICK_INTERVAL={TICK_INTERVAL}s  BASE=${BASE}/tick/side")
    print(f"{'='*65}\n")

    total_cost   = 0.0
    total_pnl    = 0.0
    wins = losses = skipped = 0
    candle_pnls  = []

    for market_id in markets:
        rows = [(t, out, bid, ask, mid) for _, t, out, bid, ask, mid in markets_data[market_id]]

        # Split into Up/Down tick streams (sorted by time)
        up_ticks = sorted([(t, bid, ask, mid) for t, out, bid, ask, mid in rows if out == 'Up'])
        dn_ticks = sorted([(t, bid, ask, mid) for t, out, bid, ask, mid in rows if out == 'Down'])

        if not up_ticks or not dn_ticks:
            skipped += 1
            continue

        candle_start = min(up_ticks[0][0], dn_ticks[0][0])
        candle_end   = max(up_ticks[-1][0], dn_ticks[-1][0])

        if candle_end - candle_start < TICK_INTERVAL * MIN_TICKS:
            skipped += 1
            continue

        # At each sample point, use most-recent known price for each side
        # Build pointer indices into sorted tick lists
        up_i = dn_i = 0

        up_shares = 0.0
        dn_shares = 0.0
        cost      = 0.0
        n_buys    = 0

        t = candle_start
        while t <= candle_end:
            # Advance pointers to most recent tick <= t
            while up_i + 1 < len(up_ticks) and up_ticks[up_i + 1][0] <= t:
                up_i += 1
            while dn_i + 1 < len(dn_ticks) and dn_ticks[dn_i + 1][0] <= t:
                dn_i += 1

            _, up_bid, up_ask, up_mid = up_ticks[up_i]
            _, dn_bid, dn_ask, dn_mid = dn_ticks[dn_i]

            # Only buy when divergence is large enough to overcome fee drag
            if up_ask > 0 and dn_ask > 0 and abs(up_mid - dn_mid) >= MIN_DIVERGENCE:
                spend_up = BASE * (1 - up_mid)
                spend_dn = BASE * (1 - dn_mid)

                sh_up = spend_up / up_ask
                sh_dn = spend_dn / dn_ask

                cost      += spend_up + fee(sh_up, up_ask)
                cost      += spend_dn + fee(sh_dn, dn_ask)
                up_shares += sh_up
                dn_shares += sh_dn
                n_buys    += 1

            t += TICK_INTERVAL

        if cost == 0 or n_buys < MIN_TICKS:
            skipped += 1
            continue

        # Determine winner from final mid prices
        last_up_mid = up_ticks[-1][3]
        last_dn_mid = dn_ticks[-1][3]
        up_wins = last_up_mid >= last_dn_mid

        if up_wins:
            payout = up_shares * 1.0
            wins += 1
        else:
            payout = dn_shares * 1.0
            losses += 1

        candle_pnl = payout - cost
        total_cost += cost
        total_pnl  += candle_pnl
        candle_pnls.append(candle_pnl)

    conn.close()

    n = wins + losses
    if n == 0:
        print("No candles processed.")
        return

    candle_pnls.sort()
    p10 = candle_pnls[int(0.10 * n)]
    p25 = candle_pnls[int(0.25 * n)]
    p50 = candle_pnls[int(0.50 * n)]
    p75 = candle_pnls[int(0.75 * n)]
    p90 = candle_pnls[int(0.90 * n)]

    avg_cost  = total_cost / n
    avg_pnl   = total_pnl / n
    roi       = total_pnl / total_cost * 100

    print(f"  Candles processed  : {n:,}  (skipped {skipped})")
    print(f"  Win rate           : {wins/n*100:.1f}%  ({wins} W / {losses} L)")
    print(f"  Total cost         : ${total_cost:>14,.2f}")
    print(f"  Net PnL            : ${total_pnl:>+14,.2f}")
    print(f"  ROI                : {roi:>+.2f}%")
    print(f"  Avg cost/candle    : ${avg_cost:>10,.2f}")
    print(f"  Avg PnL/candle     : ${avg_pnl:>+10,.2f}")
    print(f"\n  PnL distribution per candle:")
    print(f"  P10  ${p10:>+10,.2f}")
    print(f"  P25  ${p25:>+10,.2f}")
    print(f"  P50  ${p50:>+10,.2f}")
    print(f"  P75  ${p75:>+10,.2f}")
    print(f"  P90  ${p90:>+10,.2f}")

    # Also show blended avg entry prices
    print(f"\n{'='*65}")
    print(f"  SENSITIVITY (vary TICK_INTERVAL, same data)")
    print(f"{'='*65}")
    print(f"  (run with different TICK_INTERVAL values to compare)")
    print(f"\n{'='*65}\n")

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        MIN_DIVERGENCE = float(sys.argv[1])
    run_backtest()

    # Sensitivity sweep
    if len(sys.argv) == 1:
        print(f"\n{'='*65}")
        print(f"  SENSITIVITY SWEEP — MIN_DIVERGENCE")
        print(f"{'='*65}")
        print(f"  {'Div':>5}  {'N':>5}  {'WR%':>6}  {'ROI%':>7}  {'$/candle':>10}")
        print(f"  {'-'*45}")

        import sqlite3 as _sq
        from itertools import groupby as _gb
        from operator import itemgetter as _ig

        conn2 = _sq.connect(DB)
        rows_all2 = conn2.execute(
            "SELECT market_id, unix_time, outcome, bid, ask, mid FROM polymarket_odds ORDER BY market_id, unix_time"
        ).fetchall()
        conn2.close()
        md2 = {}
        for mid2, grp in _gb(rows_all2, key=_ig(0)):
            md2[mid2] = list(grp)

        for div in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
            tc = tp = w = l = sk = 0
            for market_id in md2:
                rows = [(t, out, bid, ask, mid) for _, t, out, bid, ask, mid in md2[market_id]]
                up_ticks = sorted([(t, bid, ask, mid) for t, out, bid, ask, mid in rows if out == 'Up'])
                dn_ticks = sorted([(t, bid, ask, mid) for t, out, bid, ask, mid in rows if out == 'Down'])
                if not up_ticks or not dn_ticks:
                    sk += 1; continue
                cs = min(up_ticks[0][0], dn_ticks[0][0])
                ce = max(up_ticks[-1][0], dn_ticks[-1][0])
                if ce - cs < TICK_INTERVAL * MIN_TICKS:
                    sk += 1; continue
                ui = di = 0
                ush = dsh = cost = nb = 0.0
                t = cs
                while t <= ce:
                    while ui + 1 < len(up_ticks) and up_ticks[ui+1][0] <= t: ui += 1
                    while di + 1 < len(dn_ticks) and dn_ticks[di+1][0] <= t: di += 1
                    _, uba, uak, umid = up_ticks[ui]
                    _, dba, dak, dmid = dn_ticks[di]
                    if uak > 0 and dak > 0 and abs(umid - dmid) >= div:
                        su = BASE*(1-umid); sd = BASE*(1-dmid)
                        shu = su/uak; shd = sd/dak
                        cost += su + fee(shu, uak) + sd + fee(shd, dak)
                        ush += shu; dsh += shd; nb += 1
                    t += TICK_INTERVAL
                if cost == 0 or nb < MIN_TICKS:
                    sk += 1; continue
                up_wins = up_ticks[-1][3] >= dn_ticks[-1][3]
                payout = ush if up_wins else dsh
                pnl = payout - cost
                tc += cost; tp += pnl
                if up_wins: w += 1
                else: l += 1
            n = w + l
            if n:
                print(f"  {div:>5.2f}  {n:>5}  {w/n*100:>6.1f}%  {tp/tc*100:>+7.2f}%  {tp/n:>+10.2f}")
        print()
