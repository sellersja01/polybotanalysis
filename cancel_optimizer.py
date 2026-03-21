import sqlite3
from collections import defaultdict

DB_PATH         = r"C:\Users\James\polybotanalysis\market_btc_5m.db"
LABEL           = "BTC 5M"
STARTING_BALANCE= 2500.0
RISK_PCT        = 0.04
STOP_LOSS       = 0.10
CANCEL_TIMES    = [30, 60, 90, 120, 180, 300]
LIMIT_PRICES    = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50]

def load_candles(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT unix_time, market_id, outcome, bid, ask, mid
        FROM polymarket_odds
        ORDER BY unix_time ASC
    """).fetchall()
    conn.close()

    candles = defaultdict(lambda: {"Up": [], "Down": []})
    for unix_time, market_id, outcome, bid, ask, mid in rows:
        if outcome in ("Up", "Down") and ask and float(ask) > 0:
            candles[market_id][outcome].append((
                unix_time, float(bid or 0), float(ask), float(mid or 0)
            ))
    return candles

def run(candles, cancel_after, limit_price=0.40, min_entry=0.10):
    balance = STARTING_BALANCE
    both_filled = walk_away = one_leg = neither = wins = losses = 0
    total_pnl = 0.0

    for market_id, sides in sorted(candles.items(), key=lambda x: x[1]["Up"][0][0] if x[1]["Up"] else 0):
        up_rows   = sides["Up"]
        down_rows = sides["Down"]
        if not up_rows or not down_rows:
            continue

        bet = balance * RISK_PCT

        # Resolution
        final_up_mid = up_rows[-1][3]
        if final_up_mid >= 0.90:
            resolution = "Up"
        elif final_up_mid <= 0.10:
            resolution = "Down"
        else:
            continue

        # Find first fill on each side
        up_fill   = next(((ts, ask) for ts, bid, ask, mid in up_rows if min_entry <= ask <= limit_price), None)
        down_fill = next(((ts, ask) for ts, bid, ask, mid in down_rows if min_entry <= ask <= limit_price), None)

        def sell_back(rows, fill_ts, fill_ask, ca):
            shares = bet / fill_ask
            # Find bid at cancel time
            cancel_bid = None
            for ts, bid, ask, mid in rows:
                if ts >= fill_ts + ca:
                    cancel_bid = bid if bid > 0 else mid * 0.97
                    break
            if cancel_bid is None:
                cancel_bid = rows[-1][1] if rows[-1][1] > 0 else rows[-1][3] * 0.97
            return (cancel_bid - fill_ask) * shares

        if up_fill and down_fill:
            # Check: did second leg fill within cancel_after seconds of first leg?
            first_fill_ts = min(up_fill[0], down_fill[0])
            second_fill_ts = max(up_fill[0], down_fill[0])
            
            if second_fill_ts - first_fill_ts <= cancel_after:
                # Both legs filled within window — guaranteed profit
                both_filled += 1
                up_shares   = bet / up_fill[1]
                down_shares = bet / down_fill[1]
                if resolution == "Up":
                    pnl = (1.0 - up_fill[1]) * up_shares - bet
                else:
                    pnl = (1.0 - down_fill[1]) * down_shares - bet
                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else: losses += 1
            else:
                # Second leg filled too late — treat as one-leg then walk away
                # First leg entered, second leg came but too late — sell back first leg
                if up_fill[0] < down_fill[0]:
                    # Up was first, walk it back
                    cancel_bid = None
                    for ts, bid, ask, mid in up_rows:
                        if ts >= up_fill[0] + cancel_after:
                            cancel_bid = bid if bid > 0 else mid * 0.97
                            break
                    if cancel_bid is None:
                        cancel_bid = up_rows[-1][1] if up_rows[-1][1] > 0 else up_rows[-1][3] * 0.97
                    shares = bet / up_fill[1]
                    pnl = (cancel_bid - up_fill[1]) * shares
                else:
                    cancel_bid = None
                    for ts, bid, ask, mid in down_rows:
                        if ts >= down_fill[0] + cancel_after:
                            cancel_bid = bid if bid > 0 else mid * 0.97
                            break
                    if cancel_bid is None:
                        cancel_bid = down_rows[-1][1] if down_rows[-1][1] > 0 else down_rows[-1][3] * 0.97
                    shares = bet / down_fill[1]
                    pnl = (cancel_bid - down_fill[1]) * shares
                walk_away += 1
                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else: losses += 1

        elif up_fill and not down_fill:
            # Check if second leg came close within cancel window
            second_came_close = any(
                ask <= limit_price + 0.05
                for ts, bid, ask, mid in down_rows
                if up_fill[0] <= ts <= up_fill[0] + cancel_after
            )
            if not second_came_close:
                # Walk away — sell back at bid
                walk_away += 1
                pnl = sell_back(up_rows, up_fill[0], up_fill[1], cancel_after)
                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else: losses += 1
            else:
                # Hold as one-leg directional
                one_leg += 1
                up_shares = bet / up_fill[1]
                stop_price = up_fill[1] - STOP_LOSS
                cut = False
                for ts, bid, ask, mid in up_rows:
                    if ts > up_fill[0] and (mid <= stop_price or mid <= 0.05):
                        pnl = ((bid if bid > 0 else mid * 0.97) - up_fill[1]) * up_shares
                        cut = True
                        break
                if not cut:
                    pnl = (1.0 - up_fill[1]) * up_shares if resolution == "Up" else -bet
                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else: losses += 1

        elif not up_fill and down_fill:
            second_came_close = any(
                ask <= limit_price + 0.05
                for ts, bid, ask, mid in up_rows
                if down_fill[0] <= ts <= down_fill[0] + cancel_after
            )
            if not second_came_close:
                walk_away += 1
                pnl = sell_back(down_rows, down_fill[0], down_fill[1], cancel_after)
                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else: losses += 1
            else:
                one_leg += 1
                down_shares = bet / down_fill[1]
                stop_price = down_fill[1] - STOP_LOSS
                cut = False
                for ts, bid, ask, mid in down_rows:
                    if ts > down_fill[0] and (mid <= stop_price or mid <= 0.05):
                        pnl = ((bid if bid > 0 else mid * 0.97) - down_fill[1]) * down_shares
                        cut = True
                        break
                if not cut:
                    pnl = (1.0 - down_fill[1]) * down_shares if resolution == "Down" else -bet
                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else: losses += 1
        else:
            neither += 1

    total = wins + losses
    wr = 100 * wins / total if total > 0 else 0
    return balance, total_pnl, total, wr, both_filled, walk_away, one_leg

if __name__ == "__main__":
    print(f"\nStrat AR — Cancel Time Optimizer")
    print(f"Database: {LABEL} | Risk: {RISK_PCT*100:.0f}%\n")

    print("Loading candles...")
    candles = load_candles(DB_PATH)
    print(f"Loaded {len(candles)} candles\n")

    # Test limit prices at 60s cancel
    print(f"\n--- Limit Price Test (cancel=60s) ---")
    print(f"{'Limit':>8} {'Final Bal':>12} {'P&L':>10} {'Trades':>8} {'WR':>8} {'Both':>6} {'Walk':>6} {'OneLeg':>8}")
    print("-" * 75)
    for lp in LIMIT_PRICES:
        bal, pnl, trades, wr, both, walk, one = run(candles, 60, limit_price=lp, min_entry=lp*0.25)
        print(f"{lp:>7.2f}   ${bal:>11,.2f} ${pnl:>+9,.2f} {trades:>8} {wr:>7.1f}% {both:>6} {walk:>6} {one:>8}")

    # Test cancel times at best limit price
    print(f"\n--- Cancel Time Test (limit=0.35) ---")
    print(f"{'Cancel':>8} {'Final Bal':>12} {'P&L':>10} {'Trades':>8} {'WR':>8} {'Both':>6} {'Walk':>6} {'OneLeg':>8}")
    print("-" * 75)
    for ct in CANCEL_TIMES:
        bal, pnl, trades, wr, both, walk, one = run(candles, ct, limit_price=0.35, min_entry=0.08)
        print(f"{ct:>6}s  ${bal:>11,.2f} ${pnl:>+9,.2f} {trades:>8} {wr:>7.1f}% {both:>6} {walk:>6} {one:>8}")

    print("\nDone.")
