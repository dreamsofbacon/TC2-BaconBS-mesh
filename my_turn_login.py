import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "arthur8734"
BBS_PASS = "DontPanic42!"

class SSHReaderSession(asyncssh.SSHClientSession):
    def __init__(self):
        self.queue = asyncio.Queue()

    def data_received(self, data, datatype):
        self.queue.put_nowait(data)


async def read_all(session, timeout=2.0):
    deadline = time.time() + timeout
    output = ""
    while time.time() < deadline:
        try:
            chunk = await asyncio.wait_for(
                session.queue.get(),
                timeout=min(0.3, max(0.1, deadline - time.time()))
            )
            if chunk is None:
                break
            output += chunk
        except asyncio.TimeoutError:
            if output:
                break
    return output


async def main():
    print("\n" + "="*70)
    print("🎮 MY TURN - I KNOW THE ACCOUNT EXISTS")
    print("="*70)
    print(f"\n[*] Logging in as: {BBS_USER}\n")

    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("[✓] SSH connected\n")

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)

        # Login with existing account
        banner = await read_all(session, 4.0)
        print(f"[BANNER] {banner.strip()[:100]}\n")

        print(f"[>] Username: {BBS_USER}")
        chan.write(f"{BBS_USER}\r\n")
        await asyncio.sleep(1.0)
        r1 = await read_all(session, 3.0)
        print(f"[<] {r1.strip()[:100]}\n")

        print(f"[>] Password")
        chan.write(f"{BBS_PASS}\r\n")
        await asyncio.sleep(2.0)
        r2 = await read_all(session, 4.0)
        print(f"[<] {r2.strip()[:200]}\n")

        # Navigate to game
        print("[>] Utilities (3)")
        chan.write("3\r\n")
        await asyncio.sleep(1.0)
        r3 = await read_all(session, 2.0)
        print(f"[<] {r3.strip()[:200]}\n")

        print("[>] Games (4)")
        chan.write("4\r\n")
        await asyncio.sleep(1.0)
        r4 = await read_all(session, 2.0)
        print(f"[<] {r4.strip()[:200]}\n")

        print("[>] HHGTTG (5)")
        chan.write("5\r\n")
        await asyncio.sleep(3.0)
        r5 = await read_all(session, 4.0)
        print(f"[<] {r5.strip()[:500]}\n")

        # Play
        print("="*70)
        print("PLAYING")
        print("="*70 + "\n")

        moves = [
            "get up",
            "look",
            "take gown",
            "wear gown",
            "take towel",
            "south",
            "look",
            "south",
            "look",
        ]

        for i, cmd in enumerate(moves, 1):
            print(f"[{i}] >>> {cmd}")
            chan.write(f"{cmd}\r\n")
            await asyncio.sleep(0.8)
            result = await read_all(session, 2.0)

            for line in result.split('\n'):
                s = line.strip()
                if s and s != cmd and not s.startswith('>'):
                    print(f"    {s}")

            rl = result.lower()
            if "bulldozer" in rl and ("demolished" in rl or "collapsing" in rl):
                print("\n💀 BULLDOZER!")
                break
            if "type restart" in rl:
                print("\n💀 GAME OVER!")
                break

            print()

        print("="*70)

asyncio.run(main())
