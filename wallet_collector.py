"""
Wallet Trade Collector

Polls Polymarket's activity API for each of the 9 tracked wallets every 60 seconds.
Stores only NEW trades (deduped by transaction hash) in a SQLite DB.

Run:  python3 wallet_collector.py
DB:   /home/opc/wallet_trades.db

Tables:
  trades  — every trade from every wallet
  state   — tracks latest timestamp seen per wallet (for efficient polling)
"""

import requests
import sqlite3
import time
import sys
from datetime import datetime, timezone

# ── Wallets ───────────────────────────────────────────────────────────────────
WALLETS = {
    'wallet_1': '0x61276aba49117fd9299707d5d573652949d5c977',
    'wallet_2': '0x5bde889dc26b097b5eaa2f1f027e01712ebccbb7',
    'wallet_3': '0xd111ced402bac802f74606deca83bbf6a1eaaf32',
    'wallet_4': '0x437bfe05a1e169b1443f16e718525a88b6f283b2',
    'wallet_5': '0x52f8784a81d967a3afb74d2e1608503ff5e261b9',
    'wallet_6': '0xa84edaf1a562eabb463dc6cf4c3e9c407a5edbeb',
    'wallet_7': '0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82',
    'wallet_8': '0xf539c942036cc7633a1e0015209a1343e9b2dda9',
    'wallet_9': '0x37c94ea1b44e01b18a1ce3ab6f8002bd6b9d7e6d',
}

POLL_INTERVAL = 60    # seconds between polls per wallet
DB_PATH       = '/home/opc/wallet_trades.db'
API_URL       = 'https://data-api.polymarket.com/activity'


# ── DB setup ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at REAL   NOT NULL,
            wallet_name TEXT    NOT NULL,
            wallet_addr TEXT    NOT NULL,
            tx_hash     TEXT,
            timestamp   INTEGER NOT NULL,
            time_utc    TEXT    NOT NULL,
            side        TEXT,
            outcome     TEXT,
            price       REAL,
            size        REAL,
            usdc        REAL,
            market      TEXT,
            UNIQUE(wallet_addr, tx_hash, timestamp, side, outcome, price, size)
        );
        CREATE TABLE IF NOT EXISTS state (
            wallet_name TEXT PRIMARY KEY,
            latest_ts   INTEGER DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            last_polled  REAL    DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS trades_wallet  ON trades(wallet_name);
        CREATE INDEX IF NOT EXISTS trades_ts      ON trades(timestamp);
        CREATE INDEX IF NOT EXISTS trades_market  ON trades(market);
    """)
    # Ensure all wallets have a state row
    for name in WALLETS:
        conn.execute(
            'INSERT OR IGNORE INTO state (wallet_name) VALUES (?)', (name,)
        )
    conn.commit()
    conn.close()


def get_latest_ts(wallet_name):
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        'SELECT latest_ts FROM state WHERE wallet_name=?', (wallet_name,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def save_trades(wallet_name, wallet_addr, new_trades):
    if not new_trades:
        return 0

    conn  = sqlite3.connect(DB_PATH)
    now   = time.time()
    added = 0

    for t in new_trades:
        ts = t.get('timestamp', 0)
        if ts > 9_999_999_999:
            ts = int(ts / 1000)
        ts = int(ts)

        time_utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        price    = float(t.get('price',   0) or 0)
        size     = float(t.get('size',    0) or 0)
        usdc     = float(t.get('usdcAmt', 0) or price * size)
        market   = t.get('market', t.get('title', ''))
        tx_hash  = t.get('transactionHash', t.get('txHash', ''))

        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                  (collected_at, wallet_name, wallet_addr, tx_hash,
                   timestamp, time_utc, side, outcome, price, size, usdc, market)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (now, wallet_name, wallet_addr, tx_hash,
                  ts, time_utc, t.get('side', ''), t.get('outcome', ''),
                  price, size, usdc, market))
            if conn.execute('SELECT changes()').fetchone()[0]:
                added += 1
        except Exception:
            pass

    if added:
        # Update state: latest timestamp seen + total count
        conn.execute("""
            UPDATE state
            SET latest_ts    = MAX(latest_ts, ?),
                total_trades = total_trades + ?,
                last_polled  = ?
            WHERE wallet_name = ?
        """, (max(int(t.get('timestamp', 0) or 0) for t in new_trades),
              added, now, wallet_name))
    else:
        conn.execute(
            'UPDATE state SET last_polled=? WHERE wallet_name=?',
            (now, wallet_name)
        )

    conn.commit()
    conn.close()
    return added


# ── API fetch ─────────────────────────────────────────────────────────────────
def fetch_new_trades(wallet_name, wallet_addr, since_ts):
    """
    Fetch trades newer than since_ts.
    Pages through up to 1000 results per poll, stopping when we hit old data.
    """
    new_trades = []

    for offset in range(0, 1000, 100):
        try:
            resp = requests.get(
                API_URL,
                params={'user': wallet_addr, 'limit': 100, 'offset': offset},
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
            print(f"  [{wallet_name}] fetch error: {e}")
            break

    return new_trades


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"  WALLET TRADE COLLECTOR")
    print(f"  Wallets: {len(WALLETS)} | Poll interval: {POLL_INTERVAL}s")
    print(f"  DB: {DB_PATH}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")

    init_db()

    # On first run, do a full backfill for each wallet
    print("  Initial backfill — fetching all historical trades...\n")
    for name, addr in WALLETS.items():
        since = get_latest_ts(name)
        if since == 0:
            # Full backfill: page through all available history
            all_trades = []
            for offset in range(0, 5000, 500):
                try:
                    resp = requests.get(
                        API_URL,
                        params={'user': addr, 'limit': 500, 'offset': offset},
                        timeout=15
                    )
                    if resp.status_code != 200 or not resp.json():
                        break
                    batch = resp.json()
                    all_trades.extend(batch)
                    if len(batch) < 500:
                        break
                except Exception as e:
                    print(f"  [{name}] backfill error: {e}")
                    break

            added = save_trades(name, addr, all_trades)
            print(f"  [{name}] backfill: {added} trades stored")
        else:
            print(f"  [{name}] already has data (latest_ts={since}) — skipping backfill")

    print(f"\n  Backfill complete. Starting continuous collection...\n")

    # Continuous polling loop
    while True:
        cycle_start = time.time()
        total_new   = 0

        for name, addr in WALLETS.items():
            since     = get_latest_ts(name)
            new_trades = fetch_new_trades(name, addr, since)
            added     = save_trades(name, addr, new_trades)
            total_new += added

            if added:
                ts_str = datetime.now(timezone.utc).strftime('%H:%M:%S')
                print(f"  [{ts_str}] {name}: +{added} new trades")

        if total_new == 0:
            ts_str = datetime.now(timezone.utc).strftime('%H:%M:%S')
            print(f"  [{ts_str}] All wallets up to date — sleeping {POLL_INTERVAL}s")

        # Sleep remainder of interval
        elapsed = time.time() - cycle_start
        sleep_for = max(0, POLL_INTERVAL - elapsed)
        time.sleep(sleep_for)


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
