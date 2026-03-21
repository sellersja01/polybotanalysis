import sqlite3
from datetime import datetime, timezone

dbs = [
    'market_btc_5m.db',
    'market_btc_15m.db',
    'market_eth_5m.db',
    'market_eth_15m.db',
]

for db in dbs:
    try:
        conn = sqlite3.connect(db)
        ts = conn.execute('SELECT MAX(unix_time) FROM polymarket_odds').fetchone()[0]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        print(f'{db}: latest = {dt} | unix = {int(ts)}')
        conn.close()
    except Exception as e:
        print(f'{db}: ERROR - {e}')
