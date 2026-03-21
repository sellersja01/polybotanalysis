import sqlite3
from collections import defaultdict

STARTING_BALANCE = 2500.0
RISK_PCT         = 0.04
MAX_COMBINED     = 0.95   # enter when combined ask <= this
ENTRY_WINDOW     = 60     # only enter within first 60 seconds of candle

DATABASES = [
    (r"C:\Users\James\polybotanalysis\market_btc_5m.db",  "BTC 5M"),
    (r"C:\Users\James\polybotanalysis\market_btc_15m.db", "BTC 15M"),
    (r"C:\Users\James\polybotanalysis\market_eth_5m.db",  "ETH 5M"),
    (r"C:\Users\James\polybotanalysis\market_eth_15m.db", "ETH 15M"),
]

def scan(db_path, label):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT unix_time, market_id, outcome, bid, ask, mid FROM polymarket_odds ORDER BY unix_time ASC").fetchall()
        conn.close()
    except Exception as e:
        print(f"  SKIP: {e}"); return

    print(f"  Rows: {len(rows):,}")

    # Group by candle
    candles = defaultdict(lambda: {"Up": [], "Down": []})
    for unix_time, market_id, outcome, bid, ask, mid in rows:
        if outcome in ("Up","Down") and ask and float(ask) > 0:
            candles[market_id][outcome].append((unix_time, float(bid or 0), float(ask), float(mid or 0)))

    print(f"  Candles: {len(candles)}")

    balance   = STARTING_BALANCE
    opps = wins = losses = 0
    total_pnl = 0.0
    edges      = []
    entry_times = []
    no_opp     = 0

    for market_id, sides in sorted(candles.items(), key=lambda x: x[1]["Up"][0][0] if x[1]["Up"] else 0):
        up_rows   = sides["Up"]
        down_rows = sides["Down"]
        if not up_rows or not down_rows: continue

        final_mid = up_rows[-1][3]
        if final_mid >= 0.90:   resolution = "Up"
        elif final_mid <= 0.10: resolution = "Down"
        else: continue

        candle_start = up_rows[0][0]

        # Build per-second snapshot of both asks
        # Index both sides by timestamp
        up_dict   = {ts: ask for ts, bid, ask, mid in up_rows}
        down_dict = {ts: ask for ts, bid, ask, mid in down_rows}

        # Get all timestamps within entry window
        all_ts = sorted(set(up_dict.keys()) | set(down_dict.keys()))

        latest_up   = 1.0
        latest_down = 1.0
        opp_found   = False

        for ts in all_ts:
            if ts - candle_start > ENTRY_WINDOW:
                break
            if ts in up_dict:   latest_up   = up_dict[ts]
            if ts in down_dict: latest_down = down_dict[ts]

            combined = latest_up + latest_down
            if combined <= MAX_COMBINED and latest_up >= 0.10 and latest_down >= 0.10:
                edge = 1.0 - combined
                opps += 1
                edges.append(edge)
                entry_times.append(ts - candle_start)

                bet          = balance * RISK_PCT
                up_shares    = bet / latest_up
                down_shares  = bet / latest_down

                if resolution == "Up":
                    pnl = (1.0 - latest_up) * up_shares - bet
                else:
                    pnl = (1.0 - latest_down) * down_shares - bet

                total_pnl += pnl
                balance   += pnl
                if pnl > 0: wins += 1
                else:       losses += 1
                opp_found = True
                break

        if not opp_found:
            no_opp += 1

    total = wins + losses
    wr = 100*wins/total if total > 0 else 0
    print(f"\n  Opportunities: {opps}/{len(candles)} candles ({100*opps//(len(candles)+1)}%)")
    print(f"  No opportunity: {no_opp} candles")
    print(f"  Final balance:  ${balance:,.2f} | P&L: ${total_pnl:+,.2f}")
    print(f"  Trades: {total} | WR: {wr:.1f}%")
    if edges:
        print(f"  Avg edge: {sum(edges)/len(edges)*100:.2f}¢ | Max: {max(edges)*100:.2f}¢ | Min: {min(edges)*100:.2f}¢")
    if entry_times:
        print(f"  Avg entry: {sum(entry_times)/len(entry_times):.1f}s | Median: {sorted(entry_times)[len(entry_times)//2]:.1f}s")
        print(f"  Under 10s: {sum(1 for t in entry_times if t<10)}/{len(entry_times)} | Under 30s: {sum(1 for t in entry_times if t<30)}/{len(entry_times)}")

if __name__ == "__main__":
    print("Candle Open Arb Scanner")
    print(f"Max combined ask: {MAX_COMBINED} | Entry window: {ENTRY_WINDOW}s\n")
    for db_path, label in DATABASES:
        scan(db_path, label)
    print("\nDone.")
