"""Per-asset tick-by-tick CSV exporter for a specific 5m candle.
Outputs 4 separate CSVs: candle_<asset>_<time>.csv — one per BTC/ETH/SOL/XRP.
Each contains all collector ticks for that asset + any bot ENTRY events in that asset's log."""
import sqlite3, re, csv, os
from datetime import datetime, timezone, timedelta

# ── EDIT THESE TWO LINES FOR THE TARGET CANDLE ────────────────────────────────
CANDLE_START = datetime(2026, 4, 22, 2, 30, tzinfo=timezone.utc)
CANDLE_END   = datetime(2026, 4, 22, 2, 35, tzinfo=timezone.utc)
# ──────────────────────────────────────────────────────────────────────────────

LOG    = '/root/live_s13_v3.log'
OUT_DIR = '/tmp'
ASSETS = ['btc', 'eth', 'sol', 'xrp']

cs_ts = CANDLE_START.timestamp()
ce_ts = CANDLE_END.timestamp()
tag   = CANDLE_START.strftime('%Y%m%d_%H%M')

def backup(src_path, dst_path):
    a = sqlite3.connect(src_path, timeout=30)
    b = sqlite3.connect(dst_path)
    a.backup(b); a.close(); b.close()

# Parse bot log once for all entries
entry_re = re.compile(
    r'\[(\d+):(\d+):(\d+)\.(\d+)\] ENTRY\[(DRY|LIVE)\] \[(\w+)\] (\w+) @([\d.]+) mid=([\d.]+) mv=([+-][\d.]+)%')

with open(LOG) as f:
    log_lines = f.readlines()
cur_date = datetime.now(timezone.utc).date()
prev_h = None
bot_entries_by_asset = {a.upper(): [] for a in ASSETS}
for line in reversed(log_lines):
    m = re.search(r'\[(\d+):(\d+):(\d+)', line)
    if not m: continue
    h = int(m.group(1))
    if prev_h is not None and h > prev_h:
        cur_date -= timedelta(days=1)
    prev_h = h
    em = entry_re.search(line)
    if em:
        h,mi,s,frac = em.group(1), em.group(2), em.group(3), em.group(4)
        micros = int(frac.ljust(6,'0')[:6])
        dt = datetime.combine(cur_date, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=int(h),minute=int(mi),second=int(s),microsecond=micros)
        ut = dt.timestamp()
        if cs_ts <= ut < ce_ts:
            asset = em.group(6).upper()
            if asset in bot_entries_by_asset:
                bot_entries_by_asset[asset].append({
                    'ts': ut, 'dt': dt, 'ms_since_start': (ut-cs_ts)*1000,
                    'mode': em.group(5), 'side': em.group(7),
                    'entry_log_ask': float(em.group(8)),
                    'entry_log_mid': float(em.group(9)),
                    'entry_mv_pct': float(em.group(10)),
                })

# For each asset: query collector DB and merge with bot entries, write CSV
summary = []
for asset in ASSETS:
    A = asset.upper()
    src = f'/root/market_{asset}_5m.db'
    snap = f'{OUT_DIR}/snap_pa_{asset}.db'
    backup(src, snap)
    c = sqlite3.connect(snap)
    rows = c.execute(
        "SELECT unix_time, outcome, bid, ask FROM polymarket_odds "
        "WHERE unix_time >= ? AND unix_time < ? AND outcome IN ('Up','Down') "
        "ORDER BY unix_time", (cs_ts, ce_ts)).fetchall()
    c.close()
    os.remove(snap)

    all_rows = []
    up_bid = up_ask = dn_bid = dn_ask = None
    for ut, outcome, bid, ask in rows:
        ut = float(ut); bid = float(bid); ask = float(ask)
        if outcome == 'Up':  up_bid, up_ask = bid, ask
        else:                dn_bid, dn_ask = bid, ask
        up_mid = (up_bid+up_ask)/2 if up_bid and up_ask else None
        dn_mid = (dn_bid+dn_ask)/2 if dn_bid and dn_ask else None
        all_rows.append({
            'ts': ut, 'dt': datetime.fromtimestamp(ut, tz=timezone.utc),
            'ms_since_start': (ut - cs_ts) * 1000,
            'event_type': 'TICK', 'side': outcome,
            'up_bid': up_bid, 'up_ask': up_ask, 'up_mid': up_mid,
            'dn_bid': dn_bid, 'dn_ask': dn_ask, 'dn_mid': dn_mid,
            'entry_log_ask': '', 'entry_log_mid': '', 'entry_mv_pct': '', 'entry_mode': '',
        })

    # Inject any bot entries for this asset
    for e in bot_entries_by_asset[A]:
        all_rows.append({
            'ts': e['ts'], 'dt': e['dt'], 'ms_since_start': e['ms_since_start'],
            'event_type': f'ENTRY_{e["mode"]}', 'side': e['side'],
            'up_bid': '', 'up_ask': '', 'up_mid': '',
            'dn_bid': '', 'dn_ask': '', 'dn_mid': '',
            'entry_log_ask': e['entry_log_ask'],
            'entry_log_mid': e['entry_log_mid'],
            'entry_mv_pct': e['entry_mv_pct'], 'entry_mode': e['mode'],
        })

    all_rows.sort(key=lambda r: r['ts'])

    out_path = f'{OUT_DIR}/candle_{asset}_{tag}.csv'
    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['timestamp_utc', 'ms_since_candle_start', 'event_type',
                    'side', 'up_bid', 'up_ask', 'up_mid', 'dn_bid', 'dn_ask', 'dn_mid',
                    'entry_log_ask', 'entry_log_mid', 'entry_mv_pct'])
        for r in all_rows:
            w.writerow([
                r['dt'].strftime('%H:%M:%S.%f')[:-3],
                f"{r['ms_since_start']:.0f}",
                r['event_type'], r['side'],
                f"{r['up_bid']:.4f}" if isinstance(r['up_bid'], float) else '',
                f"{r['up_ask']:.4f}" if isinstance(r['up_ask'], float) else '',
                f"{r['up_mid']:.4f}" if isinstance(r['up_mid'], float) else '',
                f"{r['dn_bid']:.4f}" if isinstance(r['dn_bid'], float) else '',
                f"{r['dn_ask']:.4f}" if isinstance(r['dn_ask'], float) else '',
                f"{r['dn_mid']:.4f}" if isinstance(r['dn_mid'], float) else '',
                r['entry_log_ask'], r['entry_log_mid'], r['entry_mv_pct'],
            ])

    n_ticks = sum(1 for r in all_rows if r['event_type']=='TICK')
    n_entries = len(bot_entries_by_asset[A])
    summary.append((A, out_path, n_ticks, n_entries))

print(f'\nCandle window: {CANDLE_START.strftime("%H:%M")} - {CANDLE_END.strftime("%H:%M")} UTC\n')
print(f'{"Asset":<6} {"ticks":>7} {"bot entries":>13}  CSV')
for A, path, n_ticks, n_entries in summary:
    print(f'{A:<6} {n_ticks:>7} {n_entries:>13}  {path}')
