"""
Show PnL for Paper Trader v7

Usage:
  python3 show_pnl.py          — summary + recent candles
  python3 show_pnl.py -v       — verbose: show all candles
  python3 show_pnl.py -open    — show open positions (current candles)
  python3 show_pnl.py -fills   — show all raw fills
"""

import sqlite3
import sys
import time
import os
from datetime import datetime, timezone

PAPER_DB = '/home/opc/paper_trades.db'

MARKETS = {
    'BTC_15m': ('/home/opc/market_btc_15m.db', 900),
    'BTC_5m':  ('/home/opc/market_btc_5m.db',  300),
    'ETH_15m': ('/home/opc/market_eth_15m.db', 900),
    'ETH_5m':  ('/home/opc/market_eth_5m.db',  300),
}
PRICE_CAP = 0.35
SHARES    = 100


def ts_str(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%m-%d %H:%M')


def open_ro(db_path):
    if not os.path.exists(db_path):
        return None
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def get_current_ask(src_db, candle_start, interval, outcome):
    """Get the latest ask for an outcome in the current candle."""
    conn = open_ro(src_db)
    if not conn:
        return None
    try:
        row = conn.execute("""
            SELECT ask FROM polymarket_odds
            WHERE unix_time >= ? AND unix_time < ?
              AND outcome = ? AND ask > 0
            ORDER BY unix_time DESC LIMIT 1
        """, (candle_start, candle_start + interval, outcome)).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def get_current_mid(src_db, candle_start, interval, outcome):
    conn = open_ro(src_db)
    if not conn:
        return None
    try:
        row = conn.execute("""
            SELECT mid FROM polymarket_odds
            WHERE unix_time >= ? AND unix_time < ?
              AND outcome = ? AND mid > 0
            ORDER BY unix_time DESC LIMIT 1
        """, (candle_start, candle_start + interval, outcome)).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def show_summary(conn, verbose=False):
    now = time.time()

    # ── Overall stats ─────────────────────────────────────────────────────────
    row = conn.execute("""
        SELECT COUNT(*), SUM(pnl), SUM(up_cost + dn_cost),
               SUM(CASE WHEN win=1 THEN 1 ELSE 0 END)
        FROM resolved
        WHERE winner NOT IN ('', 'SKIP') AND (n_up > 0 OR n_dn > 0)
    """).fetchone()
    n_resolved, total_pnl, total_deployed, wins = row
    n_resolved  = n_resolved  or 0
    total_pnl   = total_pnl   or 0.0
    total_deployed = total_deployed or 0.0
    wins        = wins        or 0
    losses      = n_resolved - wins
    wr          = 100 * wins / n_resolved if n_resolved else 0
    roi         = 100 * total_pnl / total_deployed if total_deployed else 0

    fills_row = conn.execute("SELECT COUNT(*), SUM(cost) FROM fills").fetchone()
    n_fills = fills_row[0] or 0
    total_cost = fills_row[1] or 0.0

    # Time range
    first_fill = conn.execute("SELECT MIN(ts) FROM fills").fetchone()[0]
    last_fill  = conn.execute("SELECT MAX(ts) FROM fills").fetchone()[0]

    print(f"\n{'='*70}")
    print(f"  PAPER TRADER v7  |  as of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*70}")
    print(f"  Resolved candles : {n_resolved}  (wins={wins}, losses={losses}, WR={wr:.1f}%)")
    print(f"  Total PnL        : ${total_pnl:+.2f}")
    print(f"  Total deployed   : ${total_deployed:.2f}  (ROI={roi:.1f}%)")
    print(f"  Total fills      : {n_fills}  (${total_cost:.2f} simulated capital)")
    if first_fill and last_fill:
        elapsed_h = (last_fill - first_fill) / 3600
        print(f"  Running since    : {ts_str(first_fill)}  ({elapsed_h:.1f}h)")
    print(f"{'─'*70}")

    # ── Per-market breakdown ─────────────────────────────────────────────────
    rows = conn.execute("""
        SELECT market, COUNT(*),
               SUM(pnl), SUM(up_cost+dn_cost),
               SUM(CASE WHEN win=1 THEN 1 ELSE 0 END),
               AVG(CASE WHEN n_up>0 AND n_dn>0 THEN combined_avg END),
               AVG(n_up + n_dn)
        FROM resolved
        WHERE winner NOT IN ('', 'SKIP') AND (n_up > 0 OR n_dn > 0)
        GROUP BY market
        ORDER BY market
    """).fetchall()

    if rows:
        print(f"  {'Market':<10} {'N':>5} {'WR%':>6} {'PnL':>10} {'Deployed':>10} {'ROI%':>7} {'AvgComb':>9} {'Fills/C':>8}")
        print(f"  {'─'*10} {'─'*5} {'─'*6} {'─'*10} {'─'*10} {'─'*7} {'─'*9} {'─'*8}")
        for mkt, n, pnl, dep, w, avg_comb, avg_fills in rows:
            wr_m  = 100 * w / n if n else 0
            roi_m = 100 * pnl / dep if dep else 0
            comb_s = f"{avg_comb:.4f}" if avg_comb else "  N/A "
            fills_s = f"{avg_fills:.1f}" if avg_fills else "  N/A"
            print(f"  {mkt:<10} {n:>5} {wr_m:>5.1f}% {pnl:>+10.2f} {dep:>10.2f} {roi_m:>6.1f}% {comb_s:>9} {fills_s:>8}")
        print(f"{'─'*70}")

    # ── Recent resolved candles ───────────────────────────────────────────────
    limit = 50 if verbose else 15
    recent = conn.execute("""
        SELECT market, candle_start, winner, n_up, n_dn,
               avg_up, avg_dn, combined_avg, pnl, win,
               up_shares, dn_shares, up_cost, dn_cost
        FROM resolved
        WHERE winner NOT IN ('', 'SKIP') AND (n_up > 0 OR n_dn > 0)
        ORDER BY resolved_at DESC LIMIT ?
    """, (limit,)).fetchall()

    if recent:
        print(f"\n  {'Recent resolved candles':}")
        print(f"  {'Market':<10} {'Time':>8} {'Win':>4} {'nUp':>4} {'nDn':>4} "
              f"{'AvgUp':>7} {'AvgDn':>7} {'Comb':>7} {'PnL':>9}")
        print(f"  {'─'*75}")
        for mkt, cs, winner, n_up, n_dn, avg_up, avg_dn, comb, pnl, win, \
                up_sh, dn_sh, up_c, dn_c in recent:
            icon = "+" if win else "-"
            comb_s = f"{comb:.4f}" if comb else "  N/A "
            print(f"  {mkt:<10} {ts_str(cs):>8} {winner:>4} {n_up:>4} {n_dn:>4} "
                  f"{avg_up:>7.4f} {avg_dn:>7.4f} {comb_s:>7} {icon}${abs(pnl):>8.2f}")
    print()


def show_open_positions(conn):
    """Show currently open (unresolved) candle positions with mark-to-market."""
    now = time.time()
    print(f"\n  {'─'*65}")
    print(f"  OPEN POSITIONS  (as of {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')})")
    print(f"  {'─'*65}")

    any_open = False
    for market, (src_db, interval) in MARKETS.items():
        current_cs = (int(now) // interval) * interval
        # Check current and previous candle
        for cs in [current_cs, current_cs - interval]:
            # Skip if already resolved
            resolved = conn.execute(
                "SELECT 1 FROM resolved WHERE market=? AND candle_start=?",
                (market, cs)
            ).fetchone()
            if resolved:
                continue

            # Get fills
            rows = conn.execute("""
                SELECT outcome, ask, shares FROM fills
                WHERE market=? AND candle_start=?
            """, (market, cs)).fetchall()
            if not rows:
                continue

            up_fills = [(float(a), int(s)) for o, a, s in rows if o == 'Up']
            dn_fills = [(float(a), int(s)) for o, a, s in rows if o == 'Down']
            up_sh = sum(s for _, s in up_fills)
            dn_sh = sum(s for _, s in dn_fills)
            up_cost = sum(p * s for p, s in up_fills)
            dn_cost = sum(p * s for p, s in dn_fills)
            avg_up = (up_cost / up_sh) if up_sh else 0
            avg_dn = (dn_cost / dn_sh) if dn_sh else 0

            # Current mark
            mid_up = get_current_mid(src_db, cs, interval, 'Up')
            mid_dn = get_current_mid(src_db, cs, interval, 'Down')

            # Mark-to-market PnL
            mtm_up = (mid_up - avg_up) * up_sh if (mid_up and up_sh) else 0
            mtm_dn = (mid_dn - avg_dn) * dn_sh if (mid_dn and dn_sh) else 0
            mtm = mtm_up + mtm_dn

            remaining_s = max(0, (cs + interval) - now)
            m, s = divmod(int(remaining_s), 60)

            cs_str = ts_str(cs)
            comb = avg_up + avg_dn if (up_sh and dn_sh) else 0
            print(f"  {market:<8} candle={cs_str} | {m}m{s:02d}s left | "
                  f"up={len(up_fills)}f/{up_sh:.0f}sh avg={avg_up:.3f}  "
                  f"dn={len(dn_fills)}f/{dn_sh:.0f}sh avg={avg_dn:.3f} | "
                  f"comb={comb:.3f} | MtM=${mtm:+.2f}")
            any_open = True

    if not any_open:
        print("  No open positions.")
    print()


def show_fills(conn):
    rows = conn.execute("""
        SELECT ts, market, candle_start, outcome, ask, shares, cost
        FROM fills ORDER BY ts DESC LIMIT 100
    """).fetchall()
    print(f"\n  {'Recent fills (last 100)':}")
    print(f"  {'Time':>16} {'Market':<10} {'Outcome':>8} {'Ask':>7} {'Shares':>7} {'Cost':>8}")
    print(f"  {'─'*65}")
    for ts, mkt, cs, out, ask, sh, cost in rows:
        print(f"  {ts_str(ts):>16} {mkt:<10} {out:>8} {ask:>7.4f} {sh:>7} ${cost:>7.2f}")
    print()


def main():
    if not os.path.exists(PAPER_DB):
        print(f"DB not found: {PAPER_DB}")
        print("Is paper_trader_v7.py running?")
        sys.exit(1)

    conn = sqlite3.connect(PAPER_DB)

    args = sys.argv[1:]
    verbose = '-v' in args
    show_open = '-open' in args
    show_f = '-fills' in args

    show_summary(conn, verbose=verbose or (not show_open and not show_f))

    if show_open or (not show_f):
        show_open_positions(conn)

    if show_f:
        show_fills(conn)

    conn.close()


if __name__ == '__main__':
    main()
