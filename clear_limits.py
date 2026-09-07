import sqlite3
con = sqlite3.connect('bulletins.db')
n = con.execute('DELETE FROM link_attempts').rowcount
con.commit()
print(f'cleared {n} rate-limit records')
accts = con.execute('SELECT alias FROM accounts').fetchall()
print('accounts:', [a[0] for a in accts])
con.close()
