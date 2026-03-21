import sqlite3
from datetime import datetime, timezone

BASE = r"C:\Users\James\polybotanalysis"

# Thresholds are in odds-scale units (0.0-1.0)
MARKETS = [
    (f"{BASE}\\market_btc_5m.db",  "BTC 5M",  0.04, 0.10),
    (f"{BASE}\\market_btc_15m.db", "BTC 15M", 0.06, 0.14),
    (f"{BASE}\\market_eth_5m.db",  "ETH 5M",  0.04, 0.10),
    (f"{BASE}\\market_eth_15m.db", "ETH 15M", 0.06, 0.14),
    (f"{BASE}\\market_sol_5m.db",  "SOL 5M",  0.04, 0.10),
    (f"{BASE}\\market_sol_15m.db", "SOL 15M", 0.06, 0.14),
    (f"{BASE}\\market_xrp_5m.db",  "XRP 5M",  0.04, 0.10),
    (f"{BASE}\\market_xrp_15m.db", "XRP 15M", 0.06, 0.14),
]

BET = 10.0
ENTRY_DELAY = 60       # seconds into candle before entry allowed
SIGNAL_WINDOW = 120    # seconds to measure price move (first 2 min)
MAX_ENTRY_ODDS = 0.50  # only enter when odds < 50c (cheap side)
MIN_ENTRY_ODDS = 0.05  # skip if odds < 5c (too late / already resolved)
S7_SPREAD_MAX = 0.015  # tight spread required for S7

def run(db_path, label, thresh_small, thresh_large):
    print(f"\n{'='*70}")
    print(f" {label} — {db_path}")
    print(f"{'='*70}")

    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        print(f"  ERROR opening db: {e}")
        return

    # Load all odds rows sorted by time
    try:
        rows = conn.execute("""
            SELECT unix_time, market_id, outcome, bid, ask, mid
            FROM polymarket_odds
            ORDER BY unix_time ASC
        """).fetchall()
    except sqlite3.DatabaseError as e:
        print(f"  SKIPPED — corrupted database: {e}")
        conn.close()
        return
    conn.close()

    if not rows:
        print("  No data found.")
        return

    print(f"  Rows loaded: {len(rows):,}")

    # Group by market_id (each market_id = one candle)
    candles = {}
    for unix_time, market_id, outcome, bid, ask, mid in rows:
        if market_id not in candles:
            candles[market_id] = {"start": unix_time, "rows": []}
        candles[market_id]["rows"].append((unix_time, outcome, bid, ask, mid))

    # Get sorted candle list
    sorted_candles = sorted(candles.items(), key=lambda x: x[1]["start"])
    print(f"  Candles: {len(sorted_candles)}")

    # Compute avg asset price from mid of Up outcome
    up_mids = [mid for _, _, outcome, _, _, mid in rows if outcome == "Up" and mid and 0.01 < mid < 0.99]
    if up_mids:
        # mid of Up ≈ prob of going up, not the price. Skip avg price for now.
        pass

    # Strategy results
    strategies = {
        "S1_Down": {"n": 0, "wins": 0, "pnl": 0.0},
        "S1_Up":   {"n": 0, "wins": 0, "pnl": 0.0},
        "S2_Down": {"n": 0, "wins": 0, "pnl": 0.0},
        "S4_Down": {"n": 0, "wins": 0, "pnl": 0.0},
        "S6_Down": {"n": 0, "wins": 0, "pnl": 0.0},
        "S6_Up":   {"n": 0, "wins": 0, "pnl": 0.0},
        "S7_Down": {"n": 0, "wins": 0, "pnl": 0.0},
        "S8_Down": {"n": 0, "wins": 0, "pnl": 0.0},
    }

    prev_resolution = None  # track what last candle resolved to

    for idx, (market_id, cdata) in enumerate(sorted_candles):
        cstart = cdata["start"]
        crow = cdata["rows"]

        # Separate Up and Down rows
        up_rows   = [(t, bid, ask, mid) for t, outcome, bid, ask, mid in crow if outcome == "Up"]
        down_rows = [(t, bid, ask, mid) for t, outcome, bid, ask, mid in crow if outcome == "Down"]

        if not up_rows or not down_rows:
            continue

        # Candle open price proxy: first Up mid in first 10s
        open_up_rows = [m for t, _, _, m in up_rows if t <= cstart + 10]
        if not open_up_rows:
            open_up_rows = [up_rows[0][3]]
        open_up_mid = open_up_rows[0]

        # Price at entry (after ENTRY_DELAY)
        entry_up_rows = [(t, bid, ask, mid) for t, bid, ask, mid in up_rows if t >= cstart + ENTRY_DELAY]
        entry_down_rows = [(t, bid, ask, mid) for t, bid, ask, mid in down_rows if t >= cstart + ENTRY_DELAY]

        if not entry_up_rows or not entry_down_rows:
            continue

        entry_up_mid   = entry_up_rows[0][3]
        entry_down_mid = entry_down_rows[0][3]
        entry_up_bid   = entry_up_rows[0][1]
        entry_up_ask   = entry_up_rows[0][2]
        entry_down_ask = entry_down_rows[0][2]

        spread = abs(entry_up_ask - entry_up_bid) if entry_up_bid and entry_up_ask else 999

        # Price move in signal window
        signal_up_rows = [m for t, _, _, m in up_rows if cstart <= t <= cstart + SIGNAL_WINDOW]
        if not signal_up_rows:
            continue
        price_move = abs(signal_up_rows[-1] - signal_up_rows[0]) if len(signal_up_rows) > 1 else 0.0

        # Resolution: last mid of Up
        final_up_mid = up_rows[-1][3]
        resolved_up   = final_up_mid >= 0.90
        resolved_down = final_up_mid <= 0.10

        if not resolved_up and not resolved_down:
            continue  # candle not resolved yet, skip

        resolution = "Up" if resolved_up else "Down"

        def record(strat, bet_direction, entry_odds, require_cheap=False):
            if entry_odds < MIN_ENTRY_ODDS:
                return
            if require_cheap and entry_odds > MAX_ENTRY_ODDS:
                return
            strategies[strat]["n"] += 1
            won = (bet_direction == resolution)
            if won:
                payout = BET / entry_odds - BET
                strategies[strat]["wins"] += 1
                strategies[strat]["pnl"] += payout
            else:
                strategies[strat]["pnl"] -= BET

        # S1 Down — small price drop, bet Down (no odds filter)
        if price_move >= thresh_small and signal_up_rows[-1] < signal_up_rows[0]:
            record("S1_Down", "Down", entry_down_mid)

        # S1 Up — small price pump, bet Up (no odds filter)
        if price_move >= thresh_small and signal_up_rows[-1] > signal_up_rows[0]:
            record("S1_Up", "Up", entry_up_mid)

        # S2 Down — large price drop, bet Down (no odds filter)
        if price_move >= thresh_large and signal_up_rows[-1] < signal_up_rows[0]:
            record("S2_Down", "Down", entry_down_mid)

        # S4 Down — Down odds very cheap (<25c), fade the crowd
        if entry_down_mid < 0.25:
            record("S4_Down", "Down", entry_down_mid, require_cheap=True)

        # S6 Down — previous candle resolved Up, mean revert Down
        if prev_resolution == "Up":
            record("S6_Down", "Down", entry_down_mid, require_cheap=True)

        # S6 Up — previous candle resolved Down, mean revert Up
        if prev_resolution == "Down":
            record("S6_Up", "Up", entry_up_mid, require_cheap=True)

        # S7 Down — tight spread + small drop (no odds filter)
        if price_move >= thresh_small and signal_up_rows[-1] < signal_up_rows[0] and spread <= S7_SPREAD_MAX:
            record("S7_Down", "Down", entry_down_mid)

        # S8 Down — large drop BUT Up odds still > 40c (odds lagging)
        if price_move >= thresh_large and signal_up_rows[-1] < signal_up_rows[0] and entry_up_mid > 0.40:
            record("S8_Down", "Down", entry_down_mid)

        prev_resolution = resolution

    # Print results
    print(f"\n  {'Strategy':<12} {'Trades':>7} {'Win%':>7} {'Total PnL':>12} {'Avg PnL':>10}")
    print(f"  {'-'*52}")
    for name, s in strategies.items():
        if s["n"] == 0:
            continue
        wr = 100 * s["wins"] / s["n"]
        avg = s["pnl"] / s["n"]
        marker = " ✓" if wr >= 70 else ""
        print(f"  {name:<12} {s['n']:>7} {wr:>6.1f}% {s['pnl']:>+12.2f} {avg:>+10.2f}{marker}")


if __name__ == "__main__":
    print(f"\nPolymarket Strategy Backtester v4")
    print(f"Bet size: ${BET} | Entry delay: {ENTRY_DELAY}s | Max entry odds: {MAX_ENTRY_ODDS}")
    print(f"Running on {len(MARKETS)} markets...\n")
    for db_path, label, thresh_small, thresh_large in MARKETS:
        run(db_path, label, thresh_small, thresh_large)
    print(f"\n\nDone. ✓ = 70%+ win rate")
