import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

class SSHReaderSession(asyncssh.SSHClientSession):
    def __init__(self):
        self.queue = asyncio.Queue()

    def data_received(self, data, datatype):
        self.queue.put_nowait(data)


async def read_output(session, timeout=3.0):
    deadline = time.time() + timeout
    output = ""
    while time.time() < deadline:
        try:
            chunk = await asyncio.wait_for(
                session.queue.get(),
                timeout=min(0.5, max(0.1, deadline - time.time()))
            )
            if chunk is None:
                break
            output += chunk
            if any(m in output for m in ["> ", "BBS username: ", "password: ", "Confirm password: "]):
                await asyncio.sleep(0.2)
                while not session.queue.empty():
                    try:
                        output += session.queue.get_nowait()
                    except:
                        pass
                break
        except asyncio.TimeoutError:
            if output:
                break
    return output


# Try known accounts
ACCOUNTS = [
    ("arthurdent", "DontPanic42!"),
    ("arthurdent", "dontpanic42!"),
    ("nan", "nannannan1"),
    ("baconbot", "baconbot123"),
]

async def try_account(user, password):
    print(f"\n[*] Trying: {user}")
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(HOST, port=PORT, username=SSH_USER,
                           password=SSH_PASS, known_hosts=None),
            timeout=5.0
        )
        async with conn:
            session_factory = lambda: SSHReaderSession()
            chan, session = await conn.create_session(session_factory)

            init = await read_output(session, 3.0)
            if "BBS username:" not in init:
                print(f"    No BBS prompt")
                return False

            chan.write(f"{user}\r\n")
            res = await read_output(session, 2.0)
            print(f"    Response: {res.strip()[:80]}")

            if "BBS password:" in res or "password:" in res.lower():
                chan.write(f"{password}\r\n")
                res2 = await read_output(session, 3.0)
                print(f"    Auth result: {res2.strip()[:80]}")
                if "failed" not in res2.lower() and "error" not in res2.lower():
                    print(f"    ✅ LOGIN WORKED!")
                    return True
                else:
                    print(f"    ❌ Auth failed")
                    return False
            elif "New account" in res:
                print(f"    → Account doesn't exist")
                return False
            else:
                print(f"    Unknown response")
                return False

    except Exception as e:
        print(f"    Error: {e}")
        return False


async def main():
    print("Checking known accounts...")
    working = []
    for user, pw in ACCOUNTS:
        ok = await try_account(user, pw)
        if ok:
            working.append((user, pw))
        await asyncio.sleep(0.5)

    print(f"\n{'='*50}")
    if working:
        print("Working accounts:")
        for u, p in working:
            print(f"  {u} / {p}")
    else:
        print("No working accounts found!")
    print("="*50)

asyncio.run(main())
