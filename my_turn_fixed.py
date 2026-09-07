import asyncio
import asyncssh
import time
import random
import string

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

def gen_user():
    return "arthur" + ''.join(random.choices(string.digits, k=4))

BBS_USER = gen_user()
BBS_PASS = "DontPanic42!"

class SSHReaderSession(asyncssh.SSHClientSession):
    def __init__(self):
        self.queue = asyncio.Queue()

    def data_received(self, data, datatype):
        self.queue.put_nowait(data)


async def read_all(session, timeout=2.0):
    """Read everything available for timeout seconds."""
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
    print("🎮 MY TURN - FIXED REGISTRATION FLOW")
    print("="*70)
    print(f"\n[*] Account: {BBS_USER}\n")

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

        # ---- STEP 1: Read initial banner ----
        banner = await read_all(session, 4.0)
        print(f"[BANNER] {banner.strip()[:200]}\n")

        # ---- STEP 2: Send username ----
        print(f"[>] Sending username: {BBS_USER}")
        chan.write(f"{BBS_USER}\r\n")
        await asyncio.sleep(1.0)
        r1 = await read_all(session, 3.0)
        print(f"[<] {r1.strip()[:200]}\n")

        # ---- STEP 3: Send password ----
        print(f"[>] Sending password")
        chan.write(f"{BBS_PASS}\r\n")
        await asyncio.sleep(1.0)
        r2 = await read_all(session, 3.0)
        print(f"[<] {r2.strip()[:200]}\n")

        # ---- STEP 4: Confirm password ----
        print(f"[>] Confirming password")
        chan.write(f"{BBS_PASS}\r\n")
        await asyncio.sleep(2.0)
        r3 = await read_all(session, 4.0)
        print(f"[<] {r3.strip()[:300]}\n")

        # ---- STEP 5: Navigate to game ----
        print("[>] Utilities (3)")
        chan.write("3\r\n")
        await asyncio.sleep(1.0)
        r4 = await read_all(session, 2.0)
        print(f"[<] {r4.strip()[:200]}\n")

        print("[>] Games (4)")
        chan.write("4\r\n")
        await asyncio.sleep(1.0)
        r5 = await read_all(session, 2.0)
        print(f"[<] {r5.strip()[:200]}\n")

        print("[>] HHGTTG (5)")
        chan.write("5\r\n")
        await asyncio.sleep(3.0)
        r6 = await read_all(session, 4.0)
        print(f"[<] {r6.strip()[:500]}\n")

        # ---- STEP 6: Play ----
        print("="*70)
        print("PLAYING GAME")
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

            # Show output
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
