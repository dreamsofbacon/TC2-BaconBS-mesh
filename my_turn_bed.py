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


async def read_until(session, marker, timeout=5.0):
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
            if marker in output:
                await asyncio.sleep(0.1)
                while not session.queue.empty():
                    try:
                        output += session.queue.get_nowait()
                    except:
                        pass
                break
        except asyncio.TimeoutError:
            break
    return output


async def drain(session, timeout=2.0):
    deadline = time.time() + timeout
    output = ""
    while time.time() < deadline:
        try:
            chunk = await asyncio.wait_for(
                session.queue.get(),
                timeout=min(0.4, max(0.1, deadline - time.time()))
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
    print("🎮 MY TURN - I WILL GET OUT OF BED")
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

        # Register
        init = await read_until(session, "BBS username:", timeout=5.0)
        chan.write(f"{BBS_USER}\r\n")
        res = await read_until(session, "Create password:", timeout=5.0)
        chan.write(f"{BBS_PASS}\r\n")
        res2 = await read_until(session, "Confirm password:", timeout=5.0)
        chan.write(f"{BBS_PASS}\r\n")
        res3 = await read_until(session, "Main Menu", timeout=10.0)
        print(f"[✓] Registered as {BBS_USER}!\n")

        # Navigate to game
        chan.write("3\r\n")
        await drain(session, 1.5)
        chan.write("4\r\n")
        await drain(session, 1.5)
        chan.write("5\r\n")
        await asyncio.sleep(2.0)
        game_opening = await drain(session, 3.0)

        print("="*70)
        print("📖 GAME STARTED")
        print("="*70)
        print(game_opening[-500:])
        print("="*70 + "\n")

        # The game says "You'll have to get out of the bed first"
        # So "out" doesn't work. Try different commands.
        moves = [
            ("get up",       "Get up from bed"),
            ("stand",        "Stand up"),
            ("exit bed",     "Exit the bed"),
            ("wake up",      "Wake up properly"),
            ("turn on light","Turn on the light"),
            ("open eyes",    "Open eyes"),
            ("look",         "Look around"),
            ("get gown",     "Get gown"),
            ("wear gown",    "Wear gown"),
            ("south",        "Go south"),
            ("look",         "Look"),
        ]

        move_num = 0
        game_over = False

        for cmd, desc in moves:
            if game_over:
                break
            move_num += 1
            print(f"[{move_num:2d}] {desc}")
            print(f"     >>> {cmd}")

            chan.write(f"{cmd}\r\n")
            result = await drain(session, 2.5)

            lines = result.split('\n')
            for line in lines:
                s = line.strip()
                if s and s != cmd and len(s) > 2 and not s.startswith('>'):
                    print(f"     {s}")

            rl = result.lower()

            if "bulldozer" in rl and ("demolished" in rl or "collapsing" in rl):
                print("\n💀 BULLDOZER - GAME OVER")
                game_over = True
            elif "type restart" in rl:
                print("\n💀 GAME OVER")
                game_over = True

            print()
            await asyncio.sleep(0.2)

        print("="*70)
        print(f"Played {move_num} moves")
        print("="*70)

asyncio.run(main())
