import sqlite3

con = sqlite3.connect('bulletins.db')
cur = con.cursor()

rows = cur.execute("SELECT alias, password_hash FROM accounts WHERE alias IN ('arthurdent', 'speed_runner', 'arthur8734')").fetchall()
for r in rows:
    pw = r[1][:30] + "..." if r[1] and len(r[1]) > 30 else r[1]
    print(f"{r[0]}: {pw}")

# Also check what hash_password produces for "DontPanic42!"
from ssh_auth import hash_password, verify_password
h, s = hash_password("DontPanic42!")
print(f"\nNew hash for DontPanic42!: {h[:30]}...")
print(f"Verify new hash: {verify_password('DontPanic42!', h, s)}")

# Try verifying against stored hash for arthur8734
row = cur.execute("SELECT password_hash, password_salt FROM accounts WHERE alias='arthur8734'").fetchone()
if row and row[0] and row[1]:
    stored_hash = row[0]
    stored_salt = row[1]
    print(f"\nStored hash: {stored_hash[:30]}...")
    print(f"Stored salt: {stored_salt[:30]}...")
    print(f"Verify DontPanic42! against stored: {verify_password('DontPanic42!', stored_hash, stored_salt)}")
    print(f"Verify DontPanic42! (no !) against stored: {verify_password('DontPanic42', stored_hash, stored_salt)}")

con.close()
