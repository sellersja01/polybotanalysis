import sqlite3
conn = sqlite3.connect(r'C:\Users\James\polybotanalysis\market_btc_151m.db')
try:
    count = conn.execute('SELECT COUNT(*) FROM polymarket_odds').fetchone()[0]
    latest = conn.execute('SELECT MAX(unix_time) FROM polymarket_odds').fetchone()[0]
    print(f'rows: {count:,} | latest: {latest}')
except Exception as e:
    print(f'ERROR: {e}')
conn.close()
