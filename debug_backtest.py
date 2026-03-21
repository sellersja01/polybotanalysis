import sqlite3
from collections import defaultdict

DB_PATH         = r"C:\Users\James\polybotanalysis\market_btc_151m.db"
LIMIT_PRICE     = 0.40
RISK_PCT        = 0.04
STARTING_BALANCE= 2500.0
CANCEL_AFTER    = 30
STOP_LOSS       = 0.10

conn = sqlite3.connect(DB_PATH)
rows = conn.execute("""
    SELECT unix_time, market_id, outcome, bid, ask, mid
    FROM polymarket_odds ORDER BY unix_time ASC
""").fetchall()
conn.close()

candles = defaultdict(lambda: {"Up": [], "Down": []})
for unix_time, market_id, outcome, bid, ask, mid in rows:
    if outcome in ("Up", "Down") and ask and float(ask) > 0:
        candles[market_id][outcome].append((unix_time, float(bid or 0), float(ask), float(mid or 0)))

balance = STARTING_BALANCE
trade_log = []

for market_id, sides in sorted(candles.items(), key=lambda x: x[1]["Up"][0][0] if x[1]["Up"] else 0):
    up_rows   = sides["Up"]
    down_rows = sides["Down"]
    if not up_rows or not down_rows:
        continue

    bet = balance * RISK_PCT
    final_up_mid = up_rows[-1][3]
    if final_up_mid >= 0.90:   resolution = "Up"
    elif final_up_mid <= 0.10: resolution = "Down"
    else: continue

    up_fill   = next(((ts, ask) for ts, bid, ask, mid in up_rows if ask <= LIMIT_PRICE), None)
    down_fill = next(((ts, ask) for ts, bid, ask, mid in down_rows if ask <= LIMIT_PRICE), None)

    if up_fill and down_fill:
        up_shares   = bet / up_fill[1]
        down_shares = bet / down_fill[1]
        combined    = up_fill[1] + down_fill[1]
        if resolution == "Up":
            pnl = (1.0 - up_fill[1]) * up_shares - bet
        else:
            pnl = (1.0 - down_fill[1]) * down_shares - bet
        balance += pnl
        trade_log.append(("BOTH", up_fill[1], down_fill[1], combined, resolution, pnl, balance))

    elif up_fill and not down_fill:
        second_came_close = any(
            ask <= LIMIT_PRICE + 0.05
            for ts, bid, ask, mid in down_rows
            if up_fill[0] <= ts <= up_fill[0] + CANCEL_AFTER
        )
        if not second_came_close:
            # Walk away
            cancel_bid = None
            for ts, bid, ask, mid in up_rows:
                if ts >= up_fill[0] + CANCEL_AFTER:
                    cancel_bid = bid if bid > 0 else mid * 0.97
                    break
            if cancel_bid is None:
                cancel_bid = up_rows[-1][1] if up_rows[-1][1] > 0 else up_rows[-1][3] * 0.97
            shares = bet / up_fill[1]
            pnl = (cancel_bid - up_fill[1]) * shares
            balance += pnl
            trade_log.append(("WALK_UP", up_fill[1], cancel_bid, 0, resolution, pnl, balance))
        else:
            shares = bet / up_fill[1]
            pnl = (1.0 - up_fill[1]) * shares if resolution == "Up" else -bet
            balance += pnl
            trade_log.append(("ONELEG_UP", up_fill[1], 0, 0, resolution, pnl, balance))

    elif not up_fill and down_fill:
        second_came_close = any(
            ask <= LIMIT_PRICE + 0.05
            for ts, bid, ask, mid in up_rows
            if down_fill[0] <= ts <= down_fill[0] + CANCEL_AFTER
        )
        if not second_came_close:
            cancel_bid = None
            for ts, bid, ask, mid in down_rows:
                if ts >= down_fill[0] + CANCEL_AFTER:
                    cancel_bid = bid if bid > 0 else mid * 0.97
                    break
            if cancel_bid is None:
                cancel_bid = down_rows[-1][1] if down_rows[-1][1] > 0 else down_rows[-1][3] * 0.97
            shares = bet / down_fill[1]
            pnl = (cancel_bid - down_fill[1]) * shares
            balance += pnl
            trade_log.append(("WALK_DOWN", down_fill[1], cancel_bid, 0, resolution, pnl, balance))
        else:
            shares = bet / down_fill[1]
            pnl = (1.0 - down_fill[1]) * shares if resolution == "Down" else -bet
            balance += pnl
            trade_log.append(("ONELEG_DOWN", 0, down_fill[1], 0, resolution, pnl, balance))

print(f"\nFirst 20 trades (cancel={CANCEL_AFTER}s):")
print(f"{'Type':<12} {'Entry1':>8} {'Entry2':>8} {'Combined':>10} {'Res':>6} {'PnL':>10} {'Balance':>10}")
print("-"*70)
for t in trade_log[:20]:
    typ, e1, e2, comb, res, pnl, bal = t
    print(f"{typ:<12} {e1:>8.3f} {e2:>8.3f} {comb:>10.3f} {res:>6} {pnl:>+10.2f} {bal:>10.2f}")

print(f"\nSummary:")
print(f"Total trades: {len(trade_log)}")
print(f"Final balance: ${balance:,.2f}")
print(f"Total P&L: ${balance - STARTING_BALANCE:+,.2f}")

both   = sum(1 for t in trade_log if t[0]=="BOTH")
walks  = sum(1 for t in trade_log if t[0].startswith("WALK"))
oneleg = sum(1 for t in trade_log if t[0].startswith("ONELEG"))
wins   = sum(1 for t in trade_log if t[5] > 0)
losses = sum(1 for t in trade_log if t[5] <= 0)

print(f"Both legs: {both} | Walk-aways: {walks} | One-leg: {oneleg}")
print(f"Wins: {wins} | Losses: {losses} | WR: {100*wins/len(trade_log):.1f}%")
