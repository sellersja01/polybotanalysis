import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

STARTING_BALANCE = 2500.0
RISK_PCT         = 0.04
CUT_THRESHOLD    = 0.05

DATABASES = [
    (r"C:\Users\James\polybotanalysis\market_btc_5m.db",  "BTC 5M"),
    (r"C:\Users\James\polybotanalysis\market_eth_5m.db",  "ETH 5M"),
    (r"C:\Users\James\polybotanalysis\market_btc_15m.db", "BTC 15M"),
    (r"C:\Users\James\polybotanalysis\market_eth_15m.db", "ETH 15M"),
]

def run(DB_PATH, LABEL):
    print(f"\n{'='*65}")
    print(f"  {LABEL}")
    print(f"{'='*65}")

    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT unix_time, market_id, outcome, bid, ask, mid
            FROM polymarket_odds ORDER BY unix_time ASC
        """).fetchall()
        conn.close()
    except Exception as e:
        print(f"  SKIP: {e}"); return

    candles = defaultdict(lambda: {"Up": [], "Down": []})
    for unix_time, market_id, outcome, bid, ask, mid in rows:
        if outcome in ("Up","Down") and mid is not None:
            candles[market_id][outcome].append((unix_time, float(bid or 0), float(ask or 1), float(mid)))

    sorted_candles = sorted(candles.items(), key=lambda x: x[1]["Up"][0][0] if x[1]["Up"] else 0)

    # Strategy definitions
    # Each strat: name, signal_fn(features) -> "Up"/"Down"/None, description
    strategies = {
        "NS1_Bias_Mom_Up":   {"n":0,"wins":0,"pnl":0.0},  # Open Up bias + 60s Up momentum → Up
        "NS1_Bias_Mom_Down": {"n":0,"wins":0,"pnl":0.0},  # Open Dn bias + 60s Dn momentum → Down
        "NS2_Strong_Bias_Up":   {"n":0,"wins":0,"pnl":0.0},  # Open Up bias >0.10 → Up
        "NS2_Strong_Bias_Down": {"n":0,"wins":0,"pnl":0.0},  # Open Dn bias >0.10 → Down
        "NS3_60s_Mom_Up":    {"n":0,"wins":0,"pnl":0.0},  # 60s Up move >0.15 → Up
        "NS3_60s_Mom_Down":  {"n":0,"wins":0,"pnl":0.0},  # 60s Down move >0.15 → Down
        "NS4_Triple_Up":     {"n":0,"wins":0,"pnl":0.0},  # Open Up + prev Up + momentum Up → Up
        "NS4_Triple_Down":   {"n":0,"wins":0,"pnl":0.0},  # Open Dn + prev Dn + momentum Dn → Down
        "NS5_Reversal_Up":   {"n":0,"wins":0,"pnl":0.0},  # Dn,Dn + Up momentum → Up
        "NS5_Reversal_Down": {"n":0,"wins":0,"pnl":0.0},  # Up,Up + Dn momentum → Down
    }

    balance = STARTING_BALANCE
    prev_res = prev2_res = None

    for idx, (market_id, cdata) in enumerate(sorted_candles):
        up_rows   = cdata["Up"]
        down_rows = cdata["Down"]
        if not up_rows or not down_rows:
            continue

        start = up_rows[0][0]

        # Resolution
        final_mid = up_rows[-1][3]
        if final_mid >= 0.90:   resolution = "Up"
        elif final_mid <= 0.10: resolution = "Down"
        else:
            prev2_res = prev_res
            prev_res  = None
            continue

        # Opening mid (first 10s)
        open_up = next((m for t,_,_,m in up_rows   if t <= start+10), up_rows[0][3])
        open_dn = next((m for t,_,_,m in down_rows if t <= start+10), down_rows[0][3])
        open_bias = open_up - 0.50  # positive = Up favored

        # 60s mid
        mid60_up = next((m for t,_,_,m in up_rows if start+55 <= t <= start+65), None)
        if mid60_up is None:
            mid60_up = up_rows[-1][3] if len(up_rows) > 1 else open_up
        move60 = mid60_up - open_up  # positive = Up moved up

        # Entry price (ask at 60s)
        entry_up_ask  = next((a for t,_,a,_ in up_rows   if t >= start+60), None)
        entry_dn_ask  = next((a for t,_,a,_ in down_rows if t >= start+60), None)
        if entry_up_ask is None:  entry_up_ask  = up_rows[-1][2]
        if entry_dn_ask is None:  entry_dn_ask  = down_rows[-1][2]

        def record(strat_name, side, entry_ask):
            if entry_ask <= 0 or entry_ask >= 1.0: return
            s = strategies[strat_name]
            bet    = balance * RISK_PCT
            shares = bet / entry_ask

            # Find exit — either resolution or cut loss
            rows_to_watch = up_rows if side == "Up" else down_rows
            entry_ts = start + 60
            pnl = None
            for t, bid, ask, mid in rows_to_watch:
                if t < entry_ts: continue
                if mid <= CUT_THRESHOLD:
                    exit_p = bid if bid > 0 else mid
                    pnl = (exit_p - entry_ask) * shares
                    break
            if pnl is None:
                if resolution == side:
                    pnl = (1.0 - entry_ask) * shares
                else:
                    pnl = -bet

            s["n"]    += 1
            s["pnl"]  += pnl
            if pnl > 0: s["wins"] += 1

        bet = balance * RISK_PCT

        # NS1 — Open bias + 60s momentum confirm same direction
        if open_bias > 0.03 and move60 > 0.03:
            record("NS1_Bias_Mom_Up", "Up", entry_up_ask)
        if open_bias < -0.03 and move60 < -0.03:
            record("NS1_Bias_Mom_Down", "Down", entry_dn_ask)

        # NS2 — Strong opening bias >0.10
        if open_bias > 0.10:
            record("NS2_Strong_Bias_Up", "Up", entry_up_ask)
        if open_bias < -0.10:
            record("NS2_Strong_Bias_Down", "Down", entry_dn_ask)

        # NS3 — Large 60s momentum >0.15
        if move60 > 0.15:
            record("NS3_60s_Mom_Up", "Up", entry_up_ask)
        if move60 < -0.15:
            record("NS3_60s_Mom_Down", "Down", entry_dn_ask)

        # NS4 — Triple confirmation (bias + prev + momentum)
        if open_bias > 0.03 and prev_res == "Up" and move60 > 0.03:
            record("NS4_Triple_Up", "Up", entry_up_ask)
        if open_bias < -0.03 and prev_res == "Down" and move60 < -0.03:
            record("NS4_Triple_Down", "Down", entry_dn_ask)

        # NS5 — Streak reversal (Dn,Dn + Up momentum or Up,Up + Dn momentum)
        if prev_res == "Down" and prev2_res == "Down" and move60 > 0.03:
            record("NS5_Reversal_Up", "Up", entry_up_ask)
        if prev_res == "Up" and prev2_res == "Up" and move60 < -0.03:
            record("NS5_Reversal_Down", "Down", entry_dn_ask)

        # Update balance with all trades this candle
        for s in strategies.values():
            balance += s["pnl"] - (s["pnl"])  # balance updated inside record()

        prev2_res = prev_res
        prev_res  = resolution

    # Recompute final balance properly
    balance = STARTING_BALANCE
    for name, s in strategies.items():
        pass  # already tracked cumulatively

    print(f"\n  {'Strategy':<25} {'Trades':>7} {'WR':>8} {'Total PnL':>12} {'Avg PnL':>9}")
    print(f"  {'-'*65}")
    for name, s in strategies.items():
        if s["n"] == 0: continue
        wr  = 100*s["wins"]/s["n"]
        avg = s["pnl"]/s["n"]
        marker = "✓✓✓" if wr>=75 else "✓✓" if wr>=70 else "✓" if wr>=62 else ""
        print(f"  {name:<25} {s['n']:>7} {wr:>7.1f}% {s['pnl']:>+12.2f} {avg:>+9.2f} {marker}")

for db, label in DATABASES:
    run(db, label)

print("\nDone.")
