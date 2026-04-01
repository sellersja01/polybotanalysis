"""
Galindrast Wallet Collector
Polls Polymarket's activity API every 30 seconds for wallet 0xeebde7a.
Stores ALL trades in SQLite for strategy analysis.

Run on VPS:  python3 galindrast_collector.py
DB:          /home/opc/galindrast_trades.db
"""

import requests
import sqlite3
import time
import sys
from datetime import datetime, timezone

WALLET_ADDR = '0xeebde7a0e019a63e6b476eb425505b7b3e6eba30'
WALLET_NAME = 'galindrast'
POLL_INTERVAL = 30
DB_PATH = '/home/opc/galindrast_trades.db'
API_URL = 'https://data-api.polymarket.com/activity'


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at REAL    NOT NULL,
            tx_hash      TEXT,
            timestamp    INTEGER NOT NULL,
            time_utc     TEXT    NOT NULL,
            side         TEXT,
            outcome      TEXT,
            price        REAL,
            size         REAL,
            usdc         REAL,
            market       TEXT,
            slug         TEXT,
            condition_id TEXT,
            asset        TEXT,
            UNIQUE(tx_hash, timestamp, side, outcome, price, size)
        );
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS trades_ts     ON trades(timestamp);
        CREATE INDEX IF NOT EXISTS trades_market ON trades(market);
        CREATE INDEX IF NOT EXISTS trades_slug   ON trades(slug);
    """)
    conn.execute("INSERT OR IGNORE INTO state VALUES ('latest_ts', '0')")
    conn.execute("INSERT OR IGNORE INTO state VALUES ('total_trades', '0')")
    conn.commit()
    conn.close()


def get_latest_ts():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM state WHERE key='latest_ts'").fetchone()
    conn.close()
    return int(row[0]) if row else 0


def save_trades(new_trades):
    if not new_trades:
        return 0
    conn = sqlite3.connect(DB_PATH)
    now = time.time()
    added = 0

    for t in new_trades:
        ts = t.get('timestamp', 0)
        if ts > 9_999_999_999:
            ts = int(ts / 1000)
        ts = int(ts)

        time_utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        price = float(t.get('price', 0) or 0)
        size = float(t.get('size', 0) or 0)
        usdc = price * size
        market = t.get('title', '')
        tx_hash = t.get('transactionHash', '')
        slug = t.get('slug', t.get('eventSlug', ''))
        cond_id = t.get('conditionId', '')
        asset = t.get('asset', '')

        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                  (collected_at, tx_hash, timestamp, time_utc, side, outcome,
                   price, size, usdc, market, slug, condition_id, asset)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (now, tx_hash, ts, time_utc, t.get('side', ''),
                  t.get('outcome', ''), price, size, usdc, market,
                  slug, cond_id, asset))
            if conn.execute('SELECT changes()').fetchone()[0]:
                added += 1
        except Exception:
            pass

    if added:
        max_ts = max(int(t.get('timestamp', 0) or 0) for t in new_trades)
        if max_ts > 9_999_999_999:
            max_ts = int(max_ts / 1000)
        conn.execute("UPDATE state SET value=MAX(CAST(value AS INTEGER), ?) WHERE key='latest_ts'",
                      (max_ts,))
        conn.execute("UPDATE state SET value=CAST(CAST(value AS INTEGER) + ? AS TEXT) WHERE key='total_trades'",
                      (added,))

    conn.commit()
    conn.close()
    return added


def fetch_new_trades(since_ts):
    new_trades = []
    for offset in range(0, 2000, 100):
        try:
            resp = requests.get(
                API_URL,
                params={'user': WALLET_ADDR, 'limit': 100, 'offset': offset},
                timeout=10
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break

            found_old = False
            for t in batch:
                ts = t.get('timestamp', 0)
                if ts > 9_999_999_999:
                    ts = int(ts / 1000)
                if int(ts) <= since_ts:
                    found_old = True
                    break
                new_trades.append(t)

            if found_old or len(batch) < 100:
                break
        except Exception as e:
            print(f"  fetch error: {e}")
            break
        time.sleep(0.1)

    return new_trades


def backfill():
    """Full backfill — page through all available history."""
    print("  Running full backfill...", flush=True)
    all_trades = []
    for offset in range(0, 10000, 100):
        try:
            resp = requests.get(
                API_URL,
                params={'user': WALLET_ADDR, 'limit': 100, 'offset': offset},
                timeout=15
            )
            if resp.status_code != 200 or not resp.json():
                break
            batch = resp.json()
            all_trades.extend(batch)
            if offset % 500 == 0:
                print(f"    offset={offset}: {len(all_trades)} trades", flush=True)
            if len(batch) < 100:
                break
        except Exception as e:
            print(f"    backfill error at offset {offset}: {e}")
            break
        time.sleep(0.15)

    added = save_trades(all_trades)
    print(f"  Backfill complete: {added} trades stored", flush=True)
    return added


def main():
    print(f"\n{'=' * 60}")
    print(f"  GALINDRAST WALLET COLLECTOR")
    print(f"  Wallet: {WALLET_ADDR}")
    print(f"  Poll: every {POLL_INTERVAL}s | DB: {DB_PATH}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'=' * 60}\n", flush=True)

    init_db()

    # Backfill if empty
    if get_latest_ts() == 0:
        backfill()

    # Continuous polling
    print(f"\n  Starting continuous collection...\n", flush=True)
    while True:
        cycle_start = time.time()
        since = get_latest_ts()
        new_trades = fetch_new_trades(since)
        added = save_trades(new_trades)

        ts_str = datetime.now(timezone.utc).strftime('%H:%M:%S')
        if added:
            # Quick summary
            conn = sqlite3.connect(DB_PATH)
            total = conn.execute("SELECT value FROM state WHERE key='total_trades'").fetchone()[0]
            conn.close()
            print(f"  [{ts_str}] +{added} new trades (total: {total})", flush=True)
        else:
            print(f"  [{ts_str}] no new trades", flush=True)

        elapsed = time.time() - cycle_start
        time.sleep(max(0, POLL_INTERVAL - elapsed))


if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[CRASH] {e} — restarting in 10s...")
            time.sleep(10)
