"""
Multi-Wallet Collector
Polls Polymarket's activity API every 30 seconds for multiple wallets.
Stores ALL trades in a single SQLite DB with wallet_name column.

Run on VPS:  python3 wallet_collector_v2.py
"""
import requests
import sqlite3
import time
import sys
from datetime import datetime, timezone

WALLETS = {
    "galindrast":  "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",
    "wallet_2":    "0x89b5cdaaa4866c1e738406712012a630b4078beb",
    "wallet_3":    "0x1f3472bc20dbdee754d09b2fc292efc8a8f0ba6e",
    "wallet_4":    "0x5d634050ad89f172afb340437ed3170eaa2c9075",
    "wallet_5":    "0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82",
    "wallet_6":    "0x7da07b2a8b009a406198677debda46ad651b6be2",
    "wallet_7":    "0x8c901f67b036b5eebab4e1f2f904b8676743a904",
}

POLL_INTERVAL = 30
DB_PATH = "/root/wallet_trades_v2.db"
API_URL = "https://data-api.polymarket.com/activity"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_name  TEXT    NOT NULL,
            wallet_addr  TEXT    NOT NULL,
            collected_at REAL   NOT NULL,
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
            UNIQUE(wallet_addr, tx_hash, timestamp, side, outcome, price, size)
        );
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wallet ON trades(wallet_name);
        CREATE INDEX IF NOT EXISTS idx_ts     ON trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_slug   ON trades(slug);
    """)
    conn.commit()
    conn.close()


def get_latest_ts(wallet_name):
    key = f"latest_ts_{wallet_name}"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def save_trades(wallet_name, wallet_addr, new_trades):
    if not new_trades:
        return 0
    conn = sqlite3.connect(DB_PATH)
    now = time.time()
    added = 0

    for t in new_trades:
        ts = t.get("timestamp", 0)
        if ts > 9_999_999_999:
            ts = int(ts / 1000)
        ts = int(ts)

        time_utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or 0)
        usdc = price * size
        market = t.get("title", "")
        tx_hash = t.get("transactionHash", "")
        slug = t.get("slug", t.get("eventSlug", ""))
        cond_id = t.get("conditionId", "")
        asset = t.get("asset", "")

        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                  (wallet_name, wallet_addr, collected_at, tx_hash, timestamp, time_utc,
                   side, outcome, price, size, usdc, market, slug, condition_id, asset)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (wallet_name, wallet_addr, now, tx_hash, ts, time_utc,
                  t.get("side", ""), t.get("outcome", ""),
                  price, size, usdc, market, slug, cond_id, asset))
            if conn.execute("SELECT changes()").fetchone()[0]:
                added += 1
        except Exception:
            pass

    if added:
        max_ts = max(int(t.get("timestamp", 0) or 0) for t in new_trades)
        if max_ts > 9_999_999_999:
            max_ts = int(max_ts / 1000)
        key = f"latest_ts_{wallet_name}"
        conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)",
                     (key, str(max(get_latest_ts(wallet_name), max_ts))))

    conn.commit()
    conn.close()
    return added


def fetch_new_trades(wallet_addr, since_ts):
    new_trades = []
    for offset in range(0, 2000, 100):
        try:
            resp = requests.get(
                API_URL,
                params={"user": wallet_addr, "limit": 100, "offset": offset},
                timeout=10
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break

            found_old = False
            for t in batch:
                ts = t.get("timestamp", 0)
                if ts > 9_999_999_999:
                    ts = int(ts / 1000)
                if int(ts) <= since_ts:
                    found_old = True
                    break
                new_trades.append(t)

            if found_old or len(batch) < 100:
                break
        except Exception as e:
            break
        time.sleep(0.1)

    return new_trades


def backfill(wallet_name, wallet_addr):
    print(f"  [{wallet_name}] Backfilling...", flush=True)
    all_trades = []
    for offset in range(0, 10000, 100):
        try:
            resp = requests.get(
                API_URL,
                params={"user": wallet_addr, "limit": 100, "offset": offset},
                timeout=15
            )
            if resp.status_code != 200 or not resp.json():
                break
            batch = resp.json()
            all_trades.extend(batch)
            if len(batch) < 100:
                break
        except Exception:
            break
        time.sleep(0.15)

    added = save_trades(wallet_name, wallet_addr, all_trades)
    print(f"  [{wallet_name}] Backfill: {added} trades stored", flush=True)


def main():
    print(f"\n{'=' * 60}")
    print(f"  MULTI-WALLET COLLECTOR — {len(WALLETS)} wallets")
    for name, addr in WALLETS.items():
        print(f"    {name}: {addr[:12]}...")
    print(f"  Poll: every {POLL_INTERVAL}s | DB: {DB_PATH}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'=' * 60}\n", flush=True)

    init_db()

    # Backfill any wallets with no data
    for name, addr in WALLETS.items():
        if get_latest_ts(name) == 0:
            backfill(name, addr)

    # Continuous polling
    print(f"\n  Polling all wallets...\n", flush=True)
    while True:
        cycle_start = time.time()
        total_added = 0

        for name, addr in WALLETS.items():
            since = get_latest_ts(name)
            new_trades = fetch_new_trades(addr, since)
            added = save_trades(name, addr, new_trades)
            total_added += added
            if added:
                print(f"  [{name}] +{added} new trades", flush=True)

        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if total_added:
            conn = sqlite3.connect(DB_PATH)
            total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            conn.close()
            print(f"  [{ts_str}] +{total_added} total (db: {total})", flush=True)
        else:
            print(f"  [{ts_str}] no new trades", flush=True)

        elapsed = time.time() - cycle_start
        time.sleep(max(0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
        except Exception as e:
            print(f"[CRASH] {e} -- restarting in 10s...")
            time.sleep(10)
