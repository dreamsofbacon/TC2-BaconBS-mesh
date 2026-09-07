import sqlite3
con = sqlite3.connect('bulletins.db')
rows = con.execute('SELECT * FROM zork_saves').fetchall()
cols = [d[0] for d in con.execute('SELECT * FROM zork_saves LIMIT 1').description]
print('zork_saves columns:', cols)
for r in rows:
    print(' ', r)
n = con.execute('DELETE FROM zork_saves').rowcount
con.commit()
print(f'deleted {n} save(s)')
con.close()
