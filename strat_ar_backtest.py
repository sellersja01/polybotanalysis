import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────
LIMIT_PRICE      = 0.40   # enter when ask drops to this
STARTING_BALANCE = 2500.0
RISK_PCT         = 0.04   # 4% per leg
CANCEL_THRESHOLD = 0.95   # cancel unfilled leg if other side nearly resolved
CUT_THRESHOLD    = 0.05   # exit losing position early
STOP_LOSS        = 0.10   # exit one-leg if it moves 10 cents against us
CANCEL_AFTER     = 120    # cancel first leg if second leg not filled within X seconds

DATABASES = [
    (r"C:\Users\James\polybotanalysis\market_btc_151m.db", "BTC 15M"),
]

def run_backtest(db_path, label, cancel_after=None, silent=False):
    print(f"\n{'='*65}")
    print(f"  {label} — {db_path}")
    print(f"{'='*65}")

    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    # Load all data
    try:
        rows = conn.execute("""
            SELECT unix_time, market_id, outcome, bid, ask, mid
            FROM polymarket_odds
            ORDER BY unix_time ASC
        """).fetchall()
    except Exception as e:
        print(f"  SKIPPED — {e}")
        conn.close()
        return
    conn.close()

    if not rows:
        print("  No data.")
        return

    if not silent: print(f"  Rows loaded: {len(rows):,}")

    # Group by market_id (each = one candle)
    candles = defaultdict(lambda: {"Up": [], "Down": []})
    for unix_time, market_id, outcome, bid, ask, mid in rows:
        if outcome in ("Up", "Down") and ask and ask > 0 and bid and bid >= 0:
            candles[market_id][outcome].append((unix_time, float(bid), float(ask), float(mid)))

    if not silent: print(f"  Candles: {len(candles)}")

    # Simulate strat_AR on each candle
    balance = STARTING_BALANCE
    peak    = STARTING_BALANCE

    total_candles   = 0
    both_filled     = 0
    one_leg_only    = 0
    neither_filled  = 0
    wins            = 0
    losses          = 0
    total_pnl       = 0.0
    edges           = []
    one_leg_results = []

    for market_id, sides in sorted(candles.items(), key=lambda x: x[1]["Up"][0][0] if x[1]["Up"] else 0):
        up_rows   = sides["Up"]
        down_rows = sides["Down"]

        if not up_rows or not down_rows:
            continue

        total_candles += 1
        bet = balance * RISK_PCT

        # Simulate: walk through time chronologically
        # Find first moment each side's ask drops to LIMIT_PRICE
        up_fill_ts   = None
        up_fill_ask  = None
        down_fill_ts = None
        down_fill_ask= None

        for ts, bid, ask, mid in up_rows:
            if ask <= LIMIT_PRICE and up_fill_ts is None:
                up_fill_ts  = ts
                up_fill_ask = ask

        for ts, bid, ask, mid in down_rows:
            if ask <= LIMIT_PRICE and down_fill_ts is None:
                down_fill_ts  = ts
                down_fill_ask = ask

        # Get final resolution (last mid value)
        final_up_mid   = up_rows[-1][3]
        final_down_mid = down_rows[-1][3]
        resolved_up    = final_up_mid >= 0.90
        resolved_down  = final_up_mid <= 0.10
        
        if not resolved_up and not resolved_down:
            continue  # candle not resolved, skip

        resolution = "Up" if resolved_up else "Down"

        # Case 1: Both legs filled
        if up_fill_ts and down_fill_ts:
            both_filled += 1
            combined = up_fill_ask + down_fill_ask
            edge     = 1.0 - combined
            edges.append(edge)

            # Both legs spend bet each
            up_shares   = bet / up_fill_ask
            down_shares = bet / down_fill_ask

            if resolution == "Up":
                # Up wins — collect on Up leg, lose Down bet
                up_pnl   = (1.0 - up_fill_ask) * up_shares
                down_pnl = -bet
            else:
                # Down wins — collect on Down leg, lose Up bet
                up_pnl   = -bet
                down_pnl = (1.0 - down_fill_ask) * down_shares

            candle_pnl = up_pnl + down_pnl
            total_pnl += candle_pnl
            balance   += candle_pnl
            peak       = max(peak, balance)

            if candle_pnl > 0:
                wins += 1
            else:
                losses += 1

        # Case 2: Only Up filled
        elif up_fill_ts and not down_fill_ts:
            cancel = True
            for ts, bid, ask, mid in down_rows:
                ca = cancel_after if cancel_after else CANCEL_AFTER
            if ts >= up_fill_ts and ts <= up_fill_ts + ca:
                    if ask <= LIMIT_PRICE + 0.05:
                        cancel = False
                        break
            if cancel:
                # Walk away — sell first leg back at bid price at cancel time
                up_shares = bet / up_fill_ask
                # Find bid price at cancel time (up_fill_ts + CANCEL_AFTER)
                cancel_bid = None
                for ts, bid, ask, mid in up_rows:
                    if ts >= up_fill_ts + CANCEL_AFTER:
                        cancel_bid = bid if bid > 0 else mid * 0.97
                        break
                if cancel_bid is None:
                    cancel_bid = up_rows[-1][1] if up_rows[-1][1] > 0 else up_rows[-1][3] * 0.97
                pnl = (cancel_bid - up_fill_ask) * up_shares
                total_pnl += pnl
                balance += pnl
                neither_filled += 1
                one_leg_results.append(("Up walk away", pnl))
                if pnl > 0: wins += 1
                else: losses += 1
                continue
            one_leg_only += 1
            up_shares = bet / up_fill_ask
            stop_price = up_fill_ask - STOP_LOSS  # exit if mid drops 10c below entry

            cut = False
            for ts, bid, ask, mid in up_rows:
                if ts > up_fill_ts:
                    # Stop loss — mid moved 10c against us
                    if mid <= stop_price or mid <= CUT_THRESHOLD:
                        cut_bid = bid if bid > 0 else mid
                        pnl = (cut_bid - up_fill_ask) * up_shares
                        total_pnl += pnl
                        balance   += pnl
                        cut = True
                        one_leg_results.append(("Up only - stop", pnl))
                        if pnl > 0: wins += 1
                        else: losses += 1
                        break

            if not cut:
                if resolution == "Up":
                    pnl = (1.0 - up_fill_ask) * up_shares
                    wins += 1
                else:
                    pnl = -bet
                    losses += 1
                total_pnl += pnl
                balance   += pnl
                one_leg_results.append(("Up only - resolved", pnl))

        # Case 3: Only Down filled
        elif not up_fill_ts and down_fill_ts:
            cancel = True
            for ts, bid, ask, mid in up_rows:
                ca = cancel_after if cancel_after else CANCEL_AFTER
            if ts >= down_fill_ts and ts <= down_fill_ts + ca:
                    if ask <= LIMIT_PRICE + 0.05:
                        cancel = False
                        break
            if cancel:
                # Walk away — sell first leg back at bid price at cancel time
                down_shares = bet / down_fill_ask
                cancel_bid = None
                for ts, bid, ask, mid in down_rows:
                    if ts >= down_fill_ts + CANCEL_AFTER:
                        cancel_bid = bid if bid > 0 else mid * 0.97
                        break
                if cancel_bid is None:
                    cancel_bid = down_rows[-1][1] if down_rows[-1][1] > 0 else down_rows[-1][3] * 0.97
                pnl = (cancel_bid - down_fill_ask) * down_shares
                total_pnl += pnl
                balance += pnl
                neither_filled += 1
                one_leg_results.append(("Down walk away", pnl))
                if pnl > 0: wins += 1
                else: losses += 1
                continue
            one_leg_only += 1
            down_shares = bet / down_fill_ask
            stop_price = down_fill_ask - STOP_LOSS  # exit if mid drops 10c below entry

            cut = False
            for ts, bid, ask, mid in down_rows:
                if ts > down_fill_ts:
                    if mid <= stop_price or mid <= CUT_THRESHOLD:
                        cut_bid = bid if bid > 0 else mid
                        pnl = (cut_bid - down_fill_ask) * down_shares
                        total_pnl += pnl
                        balance   += pnl
                        cut = True
                        one_leg_results.append(("Down only - stop", pnl))
                        if pnl > 0: wins += 1
                        else: losses += 1
                        break

            if not cut:
                if resolution == "Down":
                    pnl = (1.0 - down_fill_ask) * down_shares
                    wins += 1
                else:
                    pnl = -bet
                    losses += 1
                total_pnl += pnl
                balance   += pnl
                one_leg_results.append(("Down only - resolved", pnl))

        # Case 4: Neither filled — no trade
        else:
            neither_filled += 1

    # Print results
    total_trades = wins + losses
    wr = 100 * wins / total_trades if total_trades > 0 else 0
    
    if silent:
        return {
            "balance": balance, "pnl": total_pnl, "trades": total_trades,
            "wr": wr, "both": both_filled, "walk": neither_filled
        }

    if not silent: print(f"\n  Starting balance: ${STARTING_BALANCE:,.2f}")
    print(f"  Final balance:    ${balance:,.2f}")
    print(f"  Total P&L:        ${total_pnl:+,.2f}")
    print(f"  Peak balance:     ${peak:,.2f}")
    print(f"\n  Candle outcomes:")
    print(f"    Both legs filled:  {both_filled}/{total_candles} ({100*both_filled//(total_candles+1)}%)")
    print(f"    One leg only:      {one_leg_only}/{total_candles} ({100*one_leg_only//(total_candles+1)}%)")
    print(f"    Neither filled:    {neither_filled}/{total_candles} ({100*neither_filled//(total_candles+1)}%)")
    print(f"\n  Trade results:")
    print(f"    Total trades: {total_trades} | Wins: {wins} | Losses: {losses} | WR: {wr:.1f}%")

    if edges:
        avg_edge = sum(edges)/len(edges)
        print(f"\n  Both-leg stats:")
        print(f"    Avg edge:  {avg_edge*100:.2f}¢")
        print(f"    Max edge:  {max(edges)*100:.2f}¢")
        print(f"    Min edge:  {min(edges)*100:.2f}¢")

    if one_leg_results:
        one_leg_pnl = sum(p for _, p in one_leg_results)
        print(f"\n  One-leg stats:")
        print(f"    Total one-leg P&L: ${one_leg_pnl:+,.2f}")
        print(f"    Avg per trade:     ${one_leg_pnl/len(one_leg_results):+.2f}")

if __name__ == "__main__":
    print(f"\nStrat AR Backtest — Cancel Time Optimization")
    print(f"Limit price: {LIMIT_PRICE} | Risk: {RISK_PCT*100:.0f}% | Start: ${STARTING_BALANCE:,.2f}")

    cancel_times = [30, 60, 90, 120, 180, 300]
    
    for db_path, label in DATABASES:
        print(f"\n{'='*65}")
        print(f"  {label}")
        print(f"{'='*65}")
        print(f"  {'Cancel':>8} {'Final Bal':>12} {'P&L':>10} {'Trades':>8} {'WR':>8} {'Both':>6} {'Walk':>6}")
        print(f"  {'-'*65}")
        for ct in cancel_times:
            result = run_backtest(db_path, label, cancel_after=ct, silent=True)
            if result:
                print(f"  {ct:>6}s {result['balance']:>12,.2f} {result['pnl']:>+10,.2f} {result['trades']:>8} {result['wr']:>7.1f}% {result['both']:>6} {result['walk']:>6}")

    print(f"\n\nDone.")
