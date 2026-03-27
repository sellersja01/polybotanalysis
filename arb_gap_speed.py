"""
arb_gap_speed.py — How fast do arb gaps open and close?
"""

import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

DB = 'databases/arb_collector.db'
conn = sqlite3.connect(DB)

def poly_fee(price):
    return price * 0.25 * (price * (1 - price)) ** 2

def kalshi_fee(price):
    return 0.07 * price * (1 - price)

# Load all active rows
rows = conn.execute("""
    SELECT ts, asset, candle_id,
           p_up_bid, p_up_ask, p_dn_bid, p_dn_ask,
           k_up_bid, k_up_ask, k_dn_bid, k_dn_ask
    FROM snapshots
    WHERE p_up_bid > 0 AND p_up_ask > 0 AND p_dn_bid > 0 AND p_dn_ask > 0
      AND k_up_bid > 0 AND k_up_ask > 0 AND k_dn_bid > 0 AND k_dn_ask > 0
      AND p_up_ask < 0.95 AND p_dn_ask < 0.95
      AND k_up_ask < 0.95 AND k_dn_ask < 0.95
    ORDER BY asset, ts
""").fetchall()
conn.close()

# Compute net gap for each tick
ticks = []
for ts, asset, candle, pub, pua, pdb, pda, kub, kua, kdb, kda in rows:
    # Dir A: Poly Up + Kalshi Down
    net_a = 1.0 - pua - kda - poly_fee(pua) - kalshi_fee(kda)
    # Dir B: Poly Down + Kalshi Up
    net_b = 1.0 - pda - kua - poly_fee(pda) - kalshi_fee(kua)
    best_net = max(net_a, net_b)
    best_dir = 'A' if net_a >= net_b else 'B'
    ticks.append((ts, asset, candle, best_net, best_dir, net_a, net_b))

# ── Per-asset breakdown (fix case) ───────────────────────────────────────────
print(f"{'='*65}")
print(f"  PER-ASSET BREAKDOWN")
print(f"{'='*65}")
print(f"  {'Asset':<6} {'Ticks':>7} {'Prof%':>6} {'AvgNet':>8} {'MaxNet':>8}")
print(f"  {'-'*40}")
for asset in sorted(set(t[1] for t in ticks)):
    ar = [t for t in ticks if t[1] == asset]
    pr = [t for t in ar if t[3] > 0]
    pct = len(pr)/len(ar)*100 if ar else 0
    avg_n = sum(t[3] for t in pr)/len(pr) if pr else 0
    max_n = max(t[3] for t in pr) if pr else 0
    print(f"  {asset:<6} {len(ar):>7,} {pct:>5.1f}% {avg_n:>+.4f} {max_n:>+.4f}")

# ── Streak analysis (consecutive profitable ticks) ───────────────────────────
print(f"\n{'='*65}")
print(f"  GAP DURATION (consecutive net-profitable streaks)")
print(f"{'='*65}")

streaks = []
for asset in sorted(set(t[1] for t in ticks)):
    ar = [t for t in ticks if t[1] == asset]
    in_streak = False
    s_start = 0
    s_nets = []
    prev_ts = 0
    for ts, a, c, net, d, na, nb in ar:
        if net > 0:
            if not in_streak:
                s_start = ts
                s_nets = []
                in_streak = True
            s_nets.append(net)
            prev_ts = ts
        else:
            if in_streak:
                duration = prev_ts - s_start
                streaks.append({
                    'asset': asset, 'duration': duration,
                    'avg_net': sum(s_nets)/len(s_nets), 'max_net': max(s_nets),
                    'n_ticks': len(s_nets), 'start': s_start,
                })
                in_streak = False
    if in_streak:
        streaks.append({
            'asset': asset, 'duration': prev_ts - s_start,
            'avg_net': sum(s_nets)/len(s_nets), 'max_net': max(s_nets),
            'n_ticks': len(s_nets), 'start': s_start,
        })

print(f"  Total streaks         : {len(streaks)}")
if streaks:
    durs = [s['duration'] for s in streaks]
    print(f"  Avg duration          : {sum(durs)/len(durs):.1f}s")
    print(f"  Median duration       : {sorted(durs)[len(durs)//2]:.1f}s")
    print(f"  Max duration          : {max(durs):.1f}s")

    print(f"\n  Duration distribution:")
    for thresh in [0, 1, 5, 10, 30, 60, 120, 300, 600]:
        c = sum(1 for s in streaks if s['duration'] >= thresh)
        print(f"    >= {thresh:>4}s : {c:>5} ({c/len(streaks)*100:>5.1f}%)")

    print(f"\n  Duration by asset:")
    print(f"  {'Asset':<6} {'Streaks':>8} {'Avg(s)':>7} {'Med(s)':>7} {'Max(s)':>7}")
    print(f"  {'-'*42}")
    for asset in sorted(set(s['asset'] for s in streaks)):
        ss = [s for s in streaks if s['asset'] == asset]
        ds = sorted([s['duration'] for s in ss])
        print(f"  {asset:<6} {len(ss):>8} {sum(ds)/len(ds):>7.1f} {ds[len(ds)//2]:>7.1f} {max(ds):>7.1f}")

    # Top 20 longest
    print(f"\n  Top 20 longest profitable windows:")
    print(f"  {'Asset':<5} {'Dur':>7} {'Ticks':>6} {'AvgNet':>8} {'MaxNet':>8} {'Start (UTC)'}")
    print(f"  {'-'*60}")
    for s in sorted(streaks, key=lambda x: -x['duration'])[:20]:
        t = datetime.fromtimestamp(s['start'], tz=timezone.utc).strftime('%H:%M:%S')
        print(f"  {s['asset']:<5} {s['duration']:>6.0f}s {s['n_ticks']:>6} {s['avg_net']:>+.4f} {s['max_net']:>+.4f}  {t}")

# ── Gap opening/closing speed ─────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  GAP CLOSING SPEED (how fast does it go from peak to zero?)")
print(f"{'='*65}")

# For each streak, track the gap trajectory: time from peak net to gap closing
close_times = []
for asset in sorted(set(t[1] for t in ticks)):
    ar = [t for t in ticks if t[1] == asset]
    in_streak = False
    s_data = []  # (ts, net)
    for ts, a, c, net, d, na, nb in ar:
        if net > 0:
            if not in_streak:
                s_data = []
                in_streak = True
            s_data.append((ts, net))
        else:
            if in_streak and len(s_data) >= 3:
                # Find peak
                peak_idx = max(range(len(s_data)), key=lambda i: s_data[i][1])
                peak_ts, peak_net = s_data[peak_idx]
                close_ts = ts  # first tick after gap closed
                time_peak_to_close = close_ts - peak_ts

                # Time from open to peak
                open_ts = s_data[0][0]
                time_open_to_peak = peak_ts - open_ts

                close_times.append({
                    'asset': asset,
                    'peak_net': peak_net,
                    'open_to_peak': time_open_to_peak,
                    'peak_to_close': time_peak_to_close,
                    'total': close_ts - open_ts,
                    'n_ticks': len(s_data),
                })
            in_streak = False

if close_times:
    avg_o2p = sum(c['open_to_peak'] for c in close_times)/len(close_times)
    avg_p2c = sum(c['peak_to_close'] for c in close_times)/len(close_times)
    print(f"  Gaps analyzed              : {len(close_times)}")
    print(f"  Avg time: open -> peak     : {avg_o2p:.1f}s")
    print(f"  Avg time: peak -> close    : {avg_p2c:.1f}s")
    print(f"  Avg total gap lifetime     : {avg_o2p + avg_p2c:.1f}s")

    # By peak size
    print(f"\n  Closing speed by gap size:")
    print(f"  {'Peak Gap':>10} {'Count':>6} {'Open->Peak':>11} {'Peak->Close':>12} {'Total':>8}")
    print(f"  {'-'*52}")
    for lo, hi, lbl in [(0, 0.02, '<2c'), (0.02, 0.04, '2-4c'), (0.04, 0.06, '4-6c'),
                         (0.06, 0.10, '6-10c'), (0.10, 0.20, '10-20c'), (0.20, 1.0, '>20c')]:
        bucket = [c for c in close_times if lo <= c['peak_net'] < hi]
        if bucket:
            a_o = sum(c['open_to_peak'] for c in bucket)/len(bucket)
            a_c = sum(c['peak_to_close'] for c in bucket)/len(bucket)
            print(f"  {lbl:>10} {len(bucket):>6} {a_o:>10.1f}s {a_c:>11.1f}s {a_o+a_c:>7.1f}s")

    # Reaction time needed: if you see a gap > X, how long until it closes?
    print(f"\n  IF YOU SEE a gap of size X, how long do you have?")
    print(f"  (time remaining from when gap first exceeds threshold to when it closes)")
    print(f"  {'Threshold':>10} {'Occurrences':>12} {'Avg remain':>11} {'Med remain':>11}")
    print(f"  {'-'*48}")
    for thresh in [0.01, 0.02, 0.03, 0.04, 0.05, 0.08, 0.10]:
        remains = []
        for asset in sorted(set(t[1] for t in ticks)):
            ar = [t for t in ticks if t[1] == asset]
            above = False
            first_above_ts = 0
            for ts, a, c, net, d, na, nb in ar:
                if net >= thresh and not above:
                    first_above_ts = ts
                    above = True
                elif net < thresh and above:
                    remains.append(ts - first_above_ts)
                    above = False
        if remains:
            remains.sort()
            med = remains[len(remains)//2]
            avg_r = sum(remains)/len(remains)
            print(f"  {thresh:>9.2f}c {len(remains):>12} {avg_r:>10.1f}s {med:>10.1f}s")

print(f"\n{'='*65}")
