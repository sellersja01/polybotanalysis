"""
Latency Arb Backtest
====================
Strategy: when asset price moves >= MOVE_THRESH% in the last LOOKBACK seconds
AND Polymarket up_mid < 0.55 (stale / not yet updated), enter in the direction
of the move and exit when mid moves 2¢ in favor OR after 60 seconds.

Databases tested:
  - market_btc_5m.db   (BTC  5m)
  - market_eth_5m.db   (ETH  5m)
  - market_eth_15m.db  (ETH 15m)
"""

import sqlite3
import numpy as np
from bisect import bisect_left, bisect_right

# ── Strategy parameters ────────────────────────────────────────────────────────
LOOKBACK      = 15        # seconds to look back for price move
MOVE_THRESH   = 0.0015    # 0.15% move required
MIN_ENTRY     = 0.25      # poly mid must be above this to enter
MAX_ENTRY     = 0.75      # poly mid must be below this to enter
STALE_MAX     = 0.55      # "stale" condition: up_mid < 0.55 means market not updated
COOLDOWN      = 2         # seconds between new entries
EXIT_FAVOR    = 0.02      # 2¢ move in our favor → exit
EXIT_TIMEOUT  = 60        # seconds before forced exit

DB_BASE = r"C:\Users\James\polybotanalysis"

MARKETS = [
    ("BTC  5m", f"{DB_BASE}/market_btc_5m.db"),
    ("ETH  5m", f"{DB_BASE}/market_eth_5m.db"),
    ("ETH 15m", f"{DB_BASE}/market_eth_15m.db"),
]


def poly_fee(price):
    """Polymarket taker fee per share."""
    return price * 0.25 * (price * (1 - price)) ** 2


def load_data(db_path):
    """Load asset prices and polymarket odds, aligned by candle."""
    conn = sqlite3.connect(db_path)

    # Asset prices
    asset_rows = conn.execute(
        "SELECT unix_time, price FROM asset_price ORDER BY unix_time"
    ).fetchall()

    # Polymarket Up odds only (Down can be derived)
    poly_rows = conn.execute(
        "SELECT unix_time, mid, ask, outcome FROM polymarket_odds ORDER BY unix_time"
    ).fetchall()

    conn.close()

    asset_times  = np.array([r[0] for r in asset_rows], dtype=np.float64)
    asset_prices = np.array([r[1] for r in asset_rows], dtype=np.float64)

    # Separate Up and Down rows; merge into single timeline keyed on unix_time
    up_dict   = {}   # ts -> (mid, ask)
    down_dict = {}   # ts -> (mid, ask)
    for ts, mid, ask, outcome in poly_rows:
        if outcome == "Up":
            up_dict[ts]   = (mid, ask)
        elif outcome == "Down":
            down_dict[ts] = (mid, ask)

    # Build unified poly timeline: for each Up tick also capture Down ask
    poly_times    = []
    poly_up_mid   = []
    poly_up_ask   = []
    poly_dn_ask   = []

    # We iterate over all poly rows in time order; keep a running last-known
    # up and down price so every update has a full picture
    last_up_mid = None
    last_up_ask = None
    last_dn_ask = None

    # Collect all unique timestamps from both Up and Down rows
    all_poly_ts = sorted(set(list(up_dict.keys()) + list(down_dict.keys())))

    for ts in all_poly_ts:
        if ts in up_dict:
            last_up_mid, last_up_ask = up_dict[ts]
        if ts in down_dict:
            _, last_dn_ask = down_dict[ts]   # Down ask
        if last_up_mid is None or last_up_ask is None or last_dn_ask is None:
            continue
        poly_times.append(ts)
        poly_up_mid.append(last_up_mid)
        poly_up_ask.append(last_up_ask)
        poly_dn_ask.append(last_dn_ask)

    poly_times  = np.array(poly_times,  dtype=np.float64)
    poly_up_mid = np.array(poly_up_mid, dtype=np.float64)
    poly_up_ask = np.array(poly_up_ask, dtype=np.float64)
    poly_dn_ask = np.array(poly_dn_ask, dtype=np.float64)

    return asset_times, asset_prices, poly_times, poly_up_mid, poly_up_ask, poly_dn_ask


def get_asset_price_at(asset_times, asset_prices, ts):
    """Return the most recent asset price at or before ts."""
    idx = bisect_right(asset_times, ts) - 1
    if idx < 0:
        return None
    return float(asset_prices[idx])


def get_poly_at(poly_times, poly_up_mid, poly_up_ask, poly_dn_ask, ts):
    """Return the most recent poly snapshot at or before ts."""
    idx = bisect_right(poly_times, ts) - 1
    if idx < 0:
        return None, None, None
    return float(poly_up_mid[idx]), float(poly_up_ask[idx]), float(poly_dn_ask[idx])


def run_backtest(label, db_path):
    print(f"\n{'='*60}")
    print(f"  {label}  —  {db_path.split('/')[-1]}")
    print(f"{'='*60}")

    asset_times, asset_prices, poly_times, poly_up_mid, poly_up_ask, poly_dn_ask = load_data(db_path)

    print(f"  Asset rows  : {len(asset_times):,}")
    print(f"  Poly rows   : {len(poly_times):,}")
    if len(asset_times) == 0 or len(poly_times) == 0:
        print("  ERROR: empty data — skipping.")
        return

    t_start = max(asset_times[0],  poly_times[0])
    t_end   = min(asset_times[-1], poly_times[-1])
    print(f"  Overlap     : {(t_end - t_start)/3600:.1f} hours")

    # ── Walk through every poly tick as a potential signal ────────────────────
    trades = []
    last_entry_ts = -np.inf

    for i, ts in enumerate(poly_times):
        if ts < t_start + LOOKBACK:
            continue
        if ts > t_end:
            break

        # Current poly prices
        up_mid = poly_up_mid[i]
        up_ask = poly_up_ask[i]
        dn_ask = poly_dn_ask[i]

        # ── Filter 1: poly up_mid must be "stale" (< STALE_MAX) ───────────────
        if up_mid >= STALE_MAX:
            continue

        # ── Filter 2: must be in tradeable range ──────────────────────────────
        if not (MIN_ENTRY <= up_mid <= MAX_ENTRY):
            continue

        # ── Filter 3: cooldown ─────────────────────────────────────────────────
        if ts - last_entry_ts < COOLDOWN:
            continue

        # ── Asset price move over last LOOKBACK seconds ───────────────────────
        idx_now  = bisect_right(asset_times, ts) - 1
        idx_back = bisect_left(asset_times, ts - LOOKBACK)
        if idx_now < 0 or idx_back > idx_now:
            continue

        price_now  = float(asset_prices[idx_now])
        price_back = float(asset_prices[idx_back])

        if price_back == 0:
            continue

        pct_move = (price_now - price_back) / price_back

        if abs(pct_move) < MOVE_THRESH:
            continue

        # ── Determine direction and entry price ───────────────────────────────
        if pct_move > 0:
            direction  = "UP"
            entry_ask  = up_ask
            entry_mid  = up_mid
        else:
            direction  = "DOWN"
            # Down token ask ≈ dn_ask (stored directly from Down rows)
            entry_ask  = dn_ask
            entry_mid  = 1.0 - up_mid   # approximate Down mid

        if not (MIN_ENTRY <= entry_ask <= MAX_ENTRY + 0.05):
            continue

        # ── Simulate exit ─────────────────────────────────────────────────────
        exit_mid  = None
        exit_ts   = None
        exit_type = "timeout"

        # scan forward in poly timeline for exit condition
        for j in range(i + 1, len(poly_times)):
            jts = poly_times[j]
            if jts - ts > EXIT_TIMEOUT:
                # Force exit at mid at timeout
                if direction == "UP":
                    exit_mid = float(poly_up_mid[j])
                else:
                    exit_mid = 1.0 - float(poly_up_mid[j])
                exit_ts = jts
                exit_type = "timeout"
                break

            if direction == "UP":
                cur_mid = float(poly_up_mid[j])
            else:
                cur_mid = 1.0 - float(poly_up_mid[j])

            if cur_mid - entry_mid >= EXIT_FAVOR:
                exit_mid  = cur_mid
                exit_ts   = jts
                exit_type = "target"
                break

        if exit_mid is None:
            # End of data before timeout — use last available
            if direction == "UP":
                exit_mid = float(poly_up_mid[-1])
            else:
                exit_mid = 1.0 - float(poly_up_mid[-1])
            exit_ts   = poly_times[-1]
            exit_type = "eof"

        fee = poly_fee(entry_ask)
        pnl = exit_mid - entry_ask - fee   # per share

        trades.append({
            "ts":        ts,
            "direction": direction,
            "entry_ask": entry_ask,
            "entry_mid": entry_mid,
            "exit_mid":  exit_mid,
            "exit_ts":   exit_ts,
            "exit_type": exit_type,
            "fee":       fee,
            "pnl":       pnl,
        })

        last_entry_ts = ts

    # ── Results ───────────────────────────────────────────────────────────────
    if not trades:
        print("  No trades found.")
        return

    n      = len(trades)
    pnls   = np.array([t["pnl"] for t in trades])
    wins   = (pnls > 0).sum()
    wr     = wins / n * 100
    avg_pnl_cents = pnls.mean() * 100
    total_pnl_cents = pnls.sum() * 100

    target_exits  = sum(1 for t in trades if t["exit_type"] == "target")
    timeout_exits = sum(1 for t in trades if t["exit_type"] == "timeout")
    eof_exits     = sum(1 for t in trades if t["exit_type"] == "eof")

    up_trades   = sum(1 for t in trades if t["direction"] == "UP")
    down_trades = sum(1 for t in trades if t["direction"] == "DOWN")

    print(f"\n  Trades         : {n:,}  (UP: {up_trades}, DOWN: {down_trades})")
    print(f"  Win rate       : {wr:.1f}%")
    print(f"  Avg PnL/trade  : {avg_pnl_cents:+.2f}¢")
    print(f"  Total PnL      : {total_pnl_cents:+.1f}¢  (= ${total_pnl_cents/100:+.2f} per share)")
    print(f"  Exit breakdown : target={target_exits}  timeout={timeout_exits}  eof={eof_exits}")

    # Per-direction breakdown
    for d in ("UP", "DOWN"):
        d_pnls = [t["pnl"] for t in trades if t["direction"] == d]
        if d_pnls:
            d_arr = np.array(d_pnls)
            d_wr  = (d_arr > 0).mean() * 100
            d_avg = d_arr.mean() * 100
            print(f"    {d:4s}: {len(d_arr):4d} trades | WR {d_wr:.1f}% | avg {d_avg:+.2f}¢")

    # Fee stats
    avg_fee = np.array([t["fee"] for t in trades]).mean() * 100
    print(f"  Avg fee        : {avg_fee:.3f}¢")

    # Percentile breakdown of PnL
    p25, p50, p75 = np.percentile(pnls * 100, [25, 50, 75])
    print(f"  PnL percentiles: p25={p25:+.2f}¢  p50={p50:+.2f}¢  p75={p75:+.2f}¢")

    return {"label": label, "n": n, "wr": wr, "avg_pnl_cents": avg_pnl_cents, "total_pnl_cents": total_pnl_cents}


def main():
    print("\nLatency Arb Backtest")
    print(f"Parameters: LOOKBACK={LOOKBACK}s | MOVE={MOVE_THRESH*100:.2f}% | "
          f"ENTRY={MIN_ENTRY}-{MAX_ENTRY} | STALE<{STALE_MAX} | "
          f"COOLDOWN={COOLDOWN}s | EXIT +{EXIT_FAVOR*100:.0f}¢ or {EXIT_TIMEOUT}s")

    results = []
    for label, db_path in MARKETS:
        r = run_backtest(label, db_path)
        if r:
            results.append(r)

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Market':<12}  {'Trades':>7}  {'WR':>7}  {'Avg PnL':>10}  {'Total PnL':>12}")
    print(f"  {'-'*55}")
    for r in results:
        print(f"  {r['label']:<12}  {r['n']:>7,}  {r['wr']:>6.1f}%  "
              f"{r['avg_pnl_cents']:>+9.2f}¢  {r['total_pnl_cents']:>+10.1f}¢")


if __name__ == "__main__":
    main()
