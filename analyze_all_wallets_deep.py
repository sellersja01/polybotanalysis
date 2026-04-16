"""
Deep per-wallet analysis of wallet_trades_full.db
Outputs a full report for each wallet covering:
  - Basic stats
  - Asset + timeframe preferences
  - Entry price distribution
  - Position sizing
  - BUY vs SELL behavior
  - Candle-level behavior (DCA, both-sides, resolution scalp)
  - Timing within candle
  - Win rate estimation from price action
  - Strategy classification
"""

import sqlite3
import os
from collections import defaultdict
from datetime import datetime, timezone
import re
import math

DB_PATH = "wallet_trades_full.db"
OUT_DIR  = "wallet_reports"
os.makedirs(OUT_DIR, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# ── helpers ─────────────────────────────────────────────────────────────────

def pct(n, d):
    return f"{100*n/d:.1f}%" if d else "N/A"

def avg(lst):
    return sum(lst)/len(lst) if lst else 0

def median(lst):
    if not lst: return 0
    s = sorted(lst)
    n = len(s)
    return s[n//2] if n % 2 else (s[n//2-1]+s[n//2])/2

def stddev(lst):
    if len(lst) < 2: return 0
    m = avg(lst)
    return math.sqrt(sum((x-m)**2 for x in lst)/(len(lst)-1))

def parse_asset(market):
    m = market.lower()
    if "bitcoin" in m:   return "BTC"
    if "ethereum" in m:  return "ETH"
    if "solana" in m:    return "SOL"
    if "xrp" in m or "ripple" in m: return "XRP"
    return "OTHER"

def parse_timeframe(market):
    m = market.lower()
    # Look for time range patterns like "6:35PM-6:40PM" (5m), "6:00PM-6:15PM" (15m)
    match = re.search(r'(\d+:\d+)(am|pm)-(\d+:\d+)(am|pm)', m)
    if match:
        t1 = match.group(1); t2 = match.group(3)
        h1,mn1 = map(int, t1.split(':'))
        h2,mn2 = map(int, t2.split(':'))
        diff = abs((h2*60+mn2) - (h1*60+mn1))
        if diff == 0: diff = 60  # midnight rollover
        if diff <= 5:  return "5m"
        if diff <= 15: return "15m"
        if diff <= 60: return "1h"
        return "4h+"
    return "unknown"

def candle_start_ts(market, trade_ts):
    """Estimate candle start unix ts from market name text + trade timestamp."""
    tf = parse_timeframe(market)
    intervals = {"5m": 300, "15m": 900, "1h": 3600, "4h+": 14400, "unknown": 300}
    secs = intervals[tf]
    return (trade_ts // secs) * secs

def price_bucket(p):
    if p <= 0.10: return "0-10"
    if p <= 0.20: return "10-20"
    if p <= 0.30: return "20-30"
    if p <= 0.40: return "30-40"
    if p <= 0.50: return "40-50"
    if p <= 0.60: return "50-60"
    if p <= 0.70: return "60-70"
    if p <= 0.80: return "70-80"
    if p <= 0.90: return "80-90"
    if p <= 0.95: return "90-95"
    return "95-100"

BUCKETS = ["0-10","10-20","20-30","30-40","40-50","50-60","60-70","70-80","80-90","90-95","95-100"]

# ── load all wallets ─────────────────────────────────────────────────────────

c.execute("SELECT DISTINCT wallet_name FROM trades ORDER BY wallet_name")
wallets = [r[0] for r in c.fetchall()]

print(f"Analyzing {len(wallets)} wallets...\n")

# ── per-wallet analysis ──────────────────────────────────────────────────────

for wallet_name in wallets:
    print(f"  Processing {wallet_name}...")

    c.execute("SELECT * FROM trades WHERE wallet_name=? ORDER BY timestamp", (wallet_name,))
    trades = [dict(r) for r in c.fetchall()]
    if not trades: continue

    total = len(trades)
    buys  = [t for t in trades if t['side'] == 'BUY']
    sells = [t for t in trades if t['side'] == 'SELL']

    total_usdc = sum(t['usdc'] for t in buys)
    sell_usdc  = sum(t['usdc'] for t in sells)

    first_ts = trades[0]['time_utc']
    last_ts  = trades[-1]['time_utc']
    wallet_addr = trades[0]['wallet_addr']

    # ── asset / timeframe breakdown ──
    asset_counts = defaultdict(int)
    tf_counts    = defaultdict(int)
    for t in buys:
        asset_counts[parse_asset(t['market'])] += 1
        tf_counts[parse_timeframe(t['market'])] += 1

    # ── price distribution (buys only) ──
    bucket_counts = defaultdict(int)
    bucket_usdc   = defaultdict(float)
    for t in buys:
        b = price_bucket(t['price'])
        bucket_counts[b] += 1
        bucket_usdc[b]   += t['usdc']

    # ── Up vs Down ──
    up_buys   = [t for t in buys if t['outcome'] == 'Up']
    dn_buys   = [t for t in buys if t['outcome'] == 'Down']
    up_usdc   = sum(t['usdc'] for t in up_buys)
    dn_usdc   = sum(t['usdc'] for t in dn_buys)

    # ── position sizing ──
    buy_usdcs = [t['usdc'] for t in buys]
    buy_sizes = [t['size']  for t in buys]
    buy_prices= [t['price'] for t in buys]

    # ── resolution scalp detection ──
    scalp_buys = [t for t in buys if t['price'] >= 0.90]
    scalp_usdc = sum(t['usdc'] for t in scalp_buys)

    # ── candle-level grouping ──
    candles = defaultdict(list)
    for t in buys:
        key = (t['market'], candle_start_ts(t['market'], t['timestamp']))
        candles[key].append(t)

    n_candles        = len(candles)
    trades_per_candle= [len(v) for v in candles.values()]
    usdc_per_candle  = [sum(t['usdc'] for t in v) for v in candles.values()]

    # both-sides candles
    both_sides = 0
    up_heavy   = 0
    dn_heavy   = 0
    for key, ctrades in candles.items():
        has_up = any(t['outcome'] == 'Up' for t in ctrades)
        has_dn = any(t['outcome'] == 'Down' for t in ctrades)
        if has_up and has_dn:
            both_sides += 1
            u = sum(t['usdc'] for t in ctrades if t['outcome']=='Up')
            d = sum(t['usdc'] for t in ctrades if t['outcome']=='Down')
            if u > d*1.5: up_heavy += 1
            elif d > u*1.5: dn_heavy += 1
        elif has_up:
            up_heavy += 1
        else:
            dn_heavy += 1

    # DCA detection: within a candle, do prices increase over time?
    dca_candles = 0
    for key, ctrades in candles.items():
        if len(ctrades) < 3: continue
        sorted_c = sorted(ctrades, key=lambda x: x['timestamp'])
        # check if later trades have higher prices on average
        first_half = sorted_c[:len(sorted_c)//2]
        second_half= sorted_c[len(sorted_c)//2:]
        avg_first = avg([t['price'] for t in first_half])
        avg_second= avg([t['price'] for t in second_half])
        if avg_second > avg_first + 0.05:
            dca_candles += 1

    # Timing within candle (seconds from candle start)
    timing_offsets = []
    for t in buys:
        cstart = candle_start_ts(t['market'], t['timestamp'])
        offset = t['timestamp'] - cstart
        if 0 <= offset <= 900:
            timing_offsets.append(offset)

    early_entries = sum(1 for x in timing_offsets if x <= 30)   # first 30s
    late_entries  = sum(1 for x in timing_offsets if x >= 240)  # last 60s

    # ── candle outcome estimation ──
    # For each candle, look at last traded prices for Up and Down sides
    # Whichever side had the last price closest to 1.0 likely won
    # Estimate win rate for candles where wallet had a dominant side
    wins = 0; losses = 0
    for key, ctrades in candles.items():
        # determine wallet's primary side in this candle
        u_usdc = sum(t['usdc'] for t in ctrades if t['outcome']=='Up')
        d_usdc = sum(t['usdc'] for t in ctrades if t['outcome']=='Down')
        if u_usdc == 0 and d_usdc == 0: continue
        wallet_side = 'Up' if u_usdc >= d_usdc else 'Down'

        # find last known price for each side across ALL trades in this candle market
        # (use all wallets' trades to get better price signal)
        market_name = key[0]
        cstart_ts   = key[1]
        c2 = conn.cursor()
        c2.execute("""
            SELECT outcome, price, timestamp FROM trades
            WHERE market=? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp DESC
        """, (market_name, cstart_ts, cstart_ts+900))
        all_candle_trades = c2.fetchall()
        if not all_candle_trades: continue

        last_up_price = None; last_dn_price = None
        for row in all_candle_trades:
            outcome, price, ts = row
            if last_up_price is None and outcome == 'Up':  last_up_price = price
            if last_dn_price is None and outcome == 'Down': last_dn_price = price
            if last_up_price and last_dn_price: break

        if last_up_price is None or last_dn_price is None: continue

        # winner = whichever side had highest last observed price
        winner = 'Up' if last_up_price >= last_dn_price else 'Down'
        if wallet_side == winner:
            wins += 1
        else:
            losses += 1

    total_outcome = wins + losses
    win_rate = 100*wins/total_outcome if total_outcome else 0

    # ── sell behavior ──
    sell_prices = [t['price'] for t in sells]
    early_sells = [t for t in sells if t['price'] <= 0.25]  # cutting losers early

    # ── burst detection (3+ trades within 3 seconds) ──
    buy_times = sorted(t['timestamp'] for t in buys)
    bursts = 0
    i = 0
    while i < len(buy_times):
        j = i
        while j < len(buy_times) and buy_times[j] - buy_times[i] <= 3:
            j += 1
        if j - i >= 3:
            bursts += 1
        i = max(i+1, j-1)

    # ── strategy classification ──
    def classify():
        notes = []

        if scalp_usdc / total_usdc > 0.50 if total_usdc else False:
            notes.append("RESOLUTION SCALPER: >50% of volume at price 0.90+")
        elif scalp_usdc / total_usdc > 0.25 if total_usdc else False:
            notes.append("PARTIAL RESOLUTION SCALPER: 25-50% volume at 0.90+")

        if both_sides / n_candles > 0.70 if n_candles else False:
            notes.append("BOTH-SIDES BUYER: buys Up AND Down in >70% of candles")

        if dca_candles / n_candles > 0.50 if n_candles else False:
            notes.append("DCA SCALER: prices increase through candle in >50% of candles")

        cheap_pct = (bucket_counts.get("0-10",0)+bucket_counts.get("10-20",0)+bucket_counts.get("20-30",0)) / total if total else 0
        if cheap_pct > 0.50:
            notes.append("CHEAP SIDE BUYER: >50% of trades at price <0.30 (contrarian/lottery)")

        exp_pct = (bucket_counts.get("80-90",0)+bucket_counts.get("90-95",0)+bucket_counts.get("95-100",0)) / total if total else 0
        if exp_pct > 0.40:
            notes.append("EXPENSIVE SIDE BUYER: >40% of trades at price >0.80 (momentum/scalp)")

        if early_entries / len(timing_offsets) > 0.60 if timing_offsets else False:
            notes.append("EARLY CANDLE ENTRANT: >60% of entries in first 30s of candle")

        if sells and len(early_sells)/len(sells) > 0.50:
            notes.append("CUTS LOSERS: >50% of sells are at price <=0.25")

        if avg(buy_usdcs) > 200:
            notes.append(f"HIGH ROLLER: avg {avg(buy_usdcs):.0f} USDC per trade")
        elif avg(buy_usdcs) < 5:
            notes.append(f"SMALL TRADER: avg {avg(buy_usdcs):.2f} USDC per trade")

        if bursts > 50:
            notes.append(f"BURST TRADER: {bursts} bursts of 3+ trades in 3s (likely bot)")

        if not notes:
            notes.append("UNCLEAR / MIXED STRATEGY")
        return notes

    strategy_notes = classify()

    # ── write report ──────────────────────────────────────────────────────────
    out_path = os.path.join(OUT_DIR, f"{wallet_name}_report.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        def w(line=""): f.write(line + "\n")

        w("=" * 70)
        w(f"  WALLET: {wallet_name.upper()}")
        w(f"  Address: {wallet_addr}")
        w("=" * 70)
        w()

        w("── STRATEGY CLASSIFICATION ─────────────────────────────────────────")
        for note in strategy_notes:
            w(f"  * {note}")
        w()

        w("── BASIC STATS ─────────────────────────────────────────────────────")
        w(f"  Total trades:      {total:,}")
        w(f"  BUY trades:        {len(buys):,}  ({pct(len(buys),total)})")
        w(f"  SELL trades:       {len(sells):,}  ({pct(len(sells),total)})")
        w(f"  Total USDC bought: ${total_usdc:,.2f}")
        w(f"  Total USDC sold:   ${sell_usdc:,.2f}")
        w(f"  Date range:        {first_ts}  ->  {last_ts}")
        w()

        w("── ESTIMATED WIN RATE ──────────────────────────────────────────────")
        w(f"  Candles analyzed:  {total_outcome}")
        w(f"  Wins:              {wins}  ({pct(wins,total_outcome)})")
        w(f"  Losses:            {losses}  ({pct(losses,total_outcome)})")
        w(f"  Est. Win Rate:     {win_rate:.1f}%")
        w()

        w("── ASSETS TRADED ───────────────────────────────────────────────────")
        for asset, cnt in sorted(asset_counts.items(), key=lambda x:-x[1]):
            w(f"  {asset:6s}  {cnt:>8,} trades  ({pct(cnt,len(buys))})")
        w()

        w("── TIMEFRAMES ──────────────────────────────────────────────────────")
        for tf, cnt in sorted(tf_counts.items(), key=lambda x:-x[1]):
            w(f"  {tf:8s}  {cnt:>8,} trades  ({pct(cnt,len(buys))})")
        w()

        w("── ENTRY PRICE DISTRIBUTION (buys only) ────────────────────────────")
        w(f"  {'Range':>10}  {'# trades':>10}  {'% trades':>9}  {'USDC':>12}  {'% vol':>8}")
        w(f"  {'-'*10}  {'-'*10}  {'-'*9}  {'-'*12}  {'-'*8}")
        for b in BUCKETS:
            cnt  = bucket_counts.get(b,0)
            vol  = bucket_usdc.get(b,0)
            bar  = "#" * int(30 * cnt / max(bucket_counts.values(), default=1))
            w(f"  {b:>10}  {cnt:>10,}  {pct(cnt,len(buys)):>9}  ${vol:>11,.2f}  {pct(vol,total_usdc):>8}  {bar}")
        w()

        w("── POSITION SIZING ─────────────────────────────────────────────────")
        w(f"  Avg  USDC/trade:   ${avg(buy_usdcs):>10.2f}")
        w(f"  Med  USDC/trade:   ${median(buy_usdcs):>10.2f}")
        w(f"  Max  USDC/trade:   ${max(buy_usdcs,default=0):>10.2f}")
        w(f"  Avg  shares/trade: {avg(buy_sizes):>10.1f}")
        w(f"  Med  price:        {median(buy_prices):>10.3f}")
        w(f"  Avg  price:        {avg(buy_prices):>10.3f}")
        w()

        w("── UP vs DOWN PREFERENCE ───────────────────────────────────────────")
        w(f"  Up  trades:  {len(up_buys):,}  ({pct(len(up_buys),len(buys))}) | ${up_usdc:,.2f} USDC ({pct(up_usdc,total_usdc)})")
        w(f"  Down trades: {len(dn_buys):,}  ({pct(len(dn_buys),len(buys))}) | ${dn_usdc:,.2f} USDC ({pct(dn_usdc,total_usdc)})")
        w()

        w("── CANDLE-LEVEL BEHAVIOR ────────────────────────────────────────────")
        w(f"  Unique candles traded:    {n_candles:,}")
        w(f"  Avg  trades/candle:       {avg(trades_per_candle):.1f}")
        w(f"  Med  trades/candle:       {median(trades_per_candle):.1f}")
        w(f"  Max  trades/candle:       {max(trades_per_candle,default=0)}")
        w(f"  Avg  USDC/candle:         ${avg(usdc_per_candle):,.2f}")
        w(f"  Med  USDC/candle:         ${median(usdc_per_candle):,.2f}")
        w(f"  Candles both sides:       {both_sides}  ({pct(both_sides,n_candles)})")
        w(f"  Candles UP heavy:         {up_heavy}  ({pct(up_heavy,n_candles)})")
        w(f"  Candles DOWN heavy:       {dn_heavy}  ({pct(dn_heavy,n_candles)})")
        w(f"  DCA scaling candles:      {dca_candles}  ({pct(dca_candles,n_candles)})")
        w()

        w("── TIMING WITHIN CANDLE ─────────────────────────────────────────────")
        w(f"  Avg  offset from start:   {avg(timing_offsets):.1f}s")
        w(f"  Med  offset from start:   {median(timing_offsets):.1f}s")
        w(f"  Early entries (0-30s):    {early_entries}  ({pct(early_entries,len(timing_offsets))})")
        w(f"  Late  entries (240s+):    {late_entries}   ({pct(late_entries,len(timing_offsets))})")
        w()

        timing_buckets = defaultdict(int)
        for x in timing_offsets:
            b = min(int(x // 30) * 30, 270)
            timing_buckets[b] += 1
        w("  Timing histogram (30s bins):")
        for sec in range(0, 301, 30):
            cnt = timing_buckets.get(sec, 0)
            bar = "#" * int(30 * cnt / max(timing_buckets.values(), default=1))
            w(f"    {sec:>3}-{sec+30:<3}s  {cnt:>6,}  {bar}")
        w()

        w("── RESOLUTION SCALP BEHAVIOR ────────────────────────────────────────")
        w(f"  Trades at 0.90+:  {len(scalp_buys):,}  ({pct(len(scalp_buys),len(buys))})")
        w(f"  USDC  at 0.90+:   ${scalp_usdc:,.2f}  ({pct(scalp_usdc,total_usdc)})")
        w()

        w("── SELL BEHAVIOR ────────────────────────────────────────────────────")
        if sells:
            w(f"  Total sells:       {len(sells):,}")
            w(f"  Avg  sell price:   {avg(sell_prices):.3f}")
            w(f"  Med  sell price:   {median(sell_prices):.3f}")
            w(f"  Sells at <=0.25:   {len(early_sells)}  ({pct(len(early_sells),len(sells))})  [cutting losers]")
            w(f"  Sells at >=0.90:   {sum(1 for p in sell_prices if p>=0.90)}  ({pct(sum(1 for p in sell_prices if p>=0.90),len(sells))})  [taking profits]")
        else:
            w("  No sell trades found — holds everything to resolution.")
        w()

        w("── BURST TRADING ────────────────────────────────────────────────────")
        w(f"  Bursts (3+ trades in 3s): {bursts}")
        w(f"  Likely bot:               {'YES — high burst count' if bursts > 20 else 'UNCLEAR' if bursts > 5 else 'NO or manual'}")
        w()

        w("=" * 70)

    print(f"    -> {out_path}  ({total:,} trades)")

print(f"\nDone. Reports saved to ./{OUT_DIR}/")
conn.close()
