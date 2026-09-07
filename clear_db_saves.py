import asyncio
import asyncssh

async def clear_remote_saves():
    async with asyncssh.connect('bbs.local', username='bacon', known_hosts=None) as conn:
        res = await conn.run("python3 -c \"import sqlite3; con=sqlite3.connect('/home/bacon/TC2-BaconBS-mesh/bulletins.db'); con.execute('DELETE FROM zork_saves'); con.commit(); print('zork_saves cleared')\"", check=True)
        print(res.stdout)
        res2 = await conn.run("sudo systemctl restart bacon-ssh.service", check=True)
        print("SSH service restarted")

if __name__ == "__main__":
    asyncio.run(clear_remote_saves())
