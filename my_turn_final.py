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
    print("🎮 MY TURN - LET ME ACTUALLY PLAY")
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

        # ---- REGISTER (plain username, BBS handles new account flow) ----
        init = await read_until(session, "BBS username:", timeout=5.0)
        print(f"[BBS] {init.strip()[:80]}")

        chan.write(f"{BBS_USER}\r\n")
        res = await read_until(session, ":", timeout=5.0)
        print(f"[BBS] {res.strip()[:80]}")

        if "New account" in res or "Create password" in res:
            chan.write(f"{BBS_PASS}\r\n")
            res2 = await read_until(session, "Confirm password:", timeout=5.0)
            print(f"[BBS] {res2.strip()[:80]}")

            chan.write(f"{BBS_PASS}\r\n")
            res3 = await read_until(session, "Main Menu", timeout=10.0)
            if not res3:
                res3 = await drain(session, 3.0)
            print(f"[BBS] {res3.strip()[:150]}")
            print(f"[✓] Registered as {BBS_USER}!\n")
        elif "BBS password" in res:
            chan.write(f"{BBS_PASS}\r\n")
            res2 = await drain(session, 3.0)
            print(f"[BBS] {res2.strip()[:150]}")
            print(f"[✓] Logged in as {BBS_USER}!\n")

        # ---- NAVIGATE TO HHGTTG ----
        print("[*] Utilities → Games → Hitchhiker's Guide\n")

        chan.write("3\r\n")
        await drain(session, 1.5)

        chan.write("4\r\n")
        await drain(session, 1.5)

        chan.write("5\r\n")
        await asyncio.sleep(2.0)
        game_opening = await drain(session, 3.0)

        print("="*70)
        print("📖 GAME OPENING:")
        print("="*70)
        print(game_opening)
        print("="*70 + "\n")

        # ---- PLAY ----
        # Known HHGTTG solution to escape Earth:
        # 1. Get out of bed
        # 2. Get gown, wear it
        # 3. Go south (leave bedroom)
        # 4. Get towel
        # 5. Answer phone when it rings
        # 6. Go south to front porch
        # 7. Wait for Ford Prefect
        # 8. Follow Ford to pub
        # 9. Drink beer
        # 10. Go outside
        # 11. Lie down in front of bulldozer
        # 12. When Vogons arrive, you're beamed aboard

        moves = [
            ("out",          "Get out of bed"),
            ("look",         "See the room"),
            ("take gown",    "Get dressing gown"),
            ("wear gown",    "Put it on"),
            ("take towel",   "Grab towel"),
            ("south",        "Leave bedroom"),
            ("south",        "Go south"),
            ("look",         "See where we are"),
            ("answer phone", "Answer the phone"),
            ("south",        "Go south"),
            ("look",         "Look around"),
            ("south",        "Go south"),
            ("look",         "Look around"),
            ("wait",         "Wait for Ford"),
            ("look",         "Look around"),
            ("follow ford",  "Follow Ford"),
            ("look",         "Look around"),
            ("south",        "Go south"),
            ("look",         "Look"),
            ("enter pub",    "Enter the pub"),
            ("look",         "Look around"),
            ("drink beer",   "Drink beer"),
            ("drink beer",   "Drink more beer"),
            ("drink beer",   "One more beer"),
            ("north",        "Go north"),
            ("look",         "Look"),
            ("lie down",     "Lie in front of bulldozer"),
            ("look",         "Look around"),
            ("wait",         "Wait for Vogons"),
            ("look",         "Look around"),
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

            # Print game response
            lines = result.split('\n')
            for line in lines:
                s = line.strip()
                if s and s != cmd and len(s) > 2:
                    print(f"     {s}")

            rl = result.lower()

            if "bulldozer" in rl and ("demolished" in rl or "collapsing" in rl):
                print("\n💀 BULLDOZER - GAME OVER")
                game_over = True
            elif "type restart" in rl or "restart/restore/quit" in rl:
                print("\n💀 GAME OVER")
                game_over = True
            elif "heart of gold" in rl or "aboard" in rl:
                print("\n🎉 ESCAPED EARTH!")
                game_over = True

            print()
            await asyncio.sleep(0.2)

        print("="*70)
        print(f"Played {move_num} moves")
        print("="*70)

asyncio.run(main())
