import sqlite3

conn = sqlite3.connect(r"C:\Users\James\BTC 5m poly trader\market_btc_5m.db")
print("Tables:", conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
print("\npolymarket_odds columns:")
for row in conn.execute("PRAGMA table_info(polymarket_odds)").fetchall():
    print(" ", row)
print("\nasset_price columns:")
for row in conn.execute("PRAGMA table_info(asset_price)").fetchall():
    print(" ", row)
print("\nSample polymarket_odds row:")
print(conn.execute("SELECT * FROM polymarket_odds LIMIT 1").fetchone())
print("\nSample asset_price row:")
print(conn.execute("SELECT * FROM asset_price LIMIT 1").fetchone())
conn.close()
