import sys
sys.path.insert(0, '/home/bacon/TC2-BaconBS-mesh')
import db_operations

# Check what path is being used
print('BBS_DB_PATH env:', __import__('os').getenv('BBS_DB_PATH'))

# Check the actual connection path
conn = db_operations.get_db_connection()
print('DB connection:', conn)

# Try to find accounts
import sqlite3
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('Tables:', cur.fetchall())

cur.execute("SELECT alias, alias_normalized FROM accounts LIMIT 5")
print('Accounts:', cur.fetchall())
