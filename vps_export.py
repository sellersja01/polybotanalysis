import sqlite3
import subprocess

CHUNK = 50000  # rows per batch to avoid OOM

exports = [
    ('market_btc_5m',  1774173925),
    ('market_btc_15m', 1774037958),
    ('market_eth_5m',  1774037945),
    ('market_eth_15m', 1774037958),
    ('market_sol_5m',  0),
    ('market_sol_15m', 0),
    ('market_xrp_5m',  0),
    ('market_xrp_15m', 0),
]

for db_name, min_ts in exports:
    print('Exporting %s...' % db_name, flush=True)
    src = sqlite3.connect('/home/opc/%s.db' % db_name)
    dst = sqlite3.connect('/home/opc/export_%s.db' % db_name)
    dst.execute('PRAGMA journal_mode=WAL')
    dst.execute('PRAGMA synchronous=NORMAL')

    schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='polymarket_odds'").fetchone()
    dst.execute('DROP TABLE IF EXISTS polymarket_odds')
    dst.execute(schema[0])

    total = src.execute('SELECT COUNT(*) FROM polymarket_odds WHERE unix_time > ?', (min_ts,)).fetchone()[0]
    print('  %d rows to export' % total, flush=True)

    offset = 0
    placeholders = None
    while True:
        rows = src.execute(
            'SELECT * FROM polymarket_odds WHERE unix_time > ? ORDER BY unix_time ASC LIMIT ? OFFSET ?',
            (min_ts, CHUNK, offset)
        ).fetchall()
        if not rows:
            break
        if placeholders is None:
            placeholders = ','.join(['?'] * len(rows[0]))
        dst.executemany('INSERT INTO polymarket_odds VALUES (%s)' % placeholders, rows)
        dst.commit()
        offset += len(rows)
        print('  %d / %d' % (offset, total), flush=True)

    src.close()
    dst.close()
    print('%s: done' % db_name, flush=True)

# wallet_7 only
print('Exporting wallet_7...', flush=True)
src = sqlite3.connect('/home/opc/wallet_trades.db')
dst = sqlite3.connect('/home/opc/export_wallet_7.db')
dst.execute('PRAGMA journal_mode=WAL')
schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
dst.execute('DROP TABLE IF EXISTS trades')
dst.execute(schema[0])
total = src.execute("SELECT COUNT(*) FROM trades WHERE wallet_name='wallet_7'").fetchone()[0]
print('  %d wallet_7 rows' % total, flush=True)
offset = 0
placeholders = None
while True:
    rows = src.execute(
        "SELECT * FROM trades WHERE wallet_name='wallet_7' LIMIT ? OFFSET ?",
        (CHUNK, offset)
    ).fetchall()
    if not rows:
        break
    if placeholders is None:
        placeholders = ','.join(['?'] * len(rows[0]))
    dst.executemany('INSERT INTO trades VALUES (%s)' % placeholders, rows)
    dst.commit()
    offset += len(rows)
print('wallet_7: done, %d rows' % total, flush=True)
src.close()
dst.close()

print('Zipping...', flush=True)
files = ['/home/opc/export_%s.db' % n for n, _ in exports] + ['/home/opc/export_wallet_7.db']
subprocess.run(['zip', '-j', '/home/opc/exports.zip'] + files, check=True)
print('All done. exports.zip ready.', flush=True)
