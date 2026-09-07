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
    """Read until a specific marker appears in output."""
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


async def drain(session, timeout=1.5):
    """Just read whatever comes for timeout seconds."""
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
    print("🚀 MY TURN - PROPER REGISTRATION + ESCAPE EARTH")
    print("="*70)
    print(f"\n[*] New account: {BBS_USER}\n")

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

        # ---- STEP 1: Wait for BBS username prompt ----
        init = await read_until(session, "BBS username:", timeout=5.0)
        print(f"[1] Got: {init.strip()[:80]}")

        # ---- STEP 2: Send new:username ----
        chan.write(f"new:{BBS_USER}\r\n")
        step2 = await read_until(session, "Create password:", timeout=5.0)
        print(f"[2] Got: {step2.strip()[:80]}")

        # ---- STEP 3: Send password ----
        chan.write(f"{BBS_PASS}\r\n")
        step3 = await read_until(session, "Confirm password:", timeout=5.0)
        print(f"[3] Got: {step3.strip()[:80]}")

        # ---- STEP 4: Confirm password ----
        chan.write(f"{BBS_PASS}\r\n")
        # Wait for main menu — look for a menu keyword
        step4 = await read_until(session, "Main Menu", timeout=8.0)
        if not step4:
            step4 = await drain(session, 3.0)
        print(f"[4] Got: {step4.strip()[:200]}")
        print("[✓] Registered and logged in!\n")

        # ---- STEP 5: Navigate to HHGTTG ----
        print("[*] Going to: Utilities → Games → Hitchhiker's Guide\n")

        chan.write("3\r\n")
        nav1 = await drain(session, 1.5)
        print(f"[NAV 3] {nav1.strip()[:100]}")

        chan.write("4\r\n")
        nav2 = await drain(session, 1.5)
        print(f"[NAV 4] {nav2.strip()[:100]}")

        chan.write("5\r\n")
        # Game takes a moment to load
        await asyncio.sleep(2.0)
        game_opening = await drain(session, 3.0)
        print(f"\n{'='*70}")
        print("GAME OPENING:")
        print(game_opening)
        print("="*70 + "\n")

        # ---- STEP 6: Play the game ----
        # The actual known solution for escaping Earth in HHGTTG:
        #
        # 1. Get out of bed
        # 2. Get the gown, wear it
        # 3. Go south to leave bedroom
        # 4. Get the towel (CRITICAL)
        # 5. Answer the phone when it rings
        # 6. Go to front porch, wait for Ford
        # 7. Follow Ford to the pub
        # 8. Drink beer (2-3 times)
        # 9. Go outside, lie down in front of bulldozer
        # 10. When Vogon fleet arrives, get in the ship

        moves = [
            ("out",          "Get out of bed"),
            ("look",         "See what's in the room"),
            ("take gown",    "Get the dressing gown"),
            ("wear gown",    "Put it on"),
            ("take towel",   "Grab towel from rack"),
            ("south",        "Leave bedroom"),
            ("south",        "Keep going south"),
            ("east",         "Head east"),
            ("look",         "See where we are"),
            ("south",        "Keep moving"),
            ("look",         "Survey surroundings"),
            ("answer phone", "Answer the phone!"),
            ("look",         "Look around"),
            ("south",        "Go south"),
            ("west",         "Go west"),
            ("look",         "Look around"),
            ("south",        "Go south"),
            ("look",         "Look around"),
            ("wait",         "Wait for Ford"),
            ("look",         "Look around"),
        ]

        move_num = 0
        alive = True

        for cmd, desc in moves:
            if not alive:
                break
            move_num += 1
            print(f"[{move_num:2d}] {desc}")
            print(f"     >>> {cmd}")

            chan.write(f"{cmd}\r\n")
            result = await drain(session, 2.0)

            # Print game response (skip echoed command and blank lines)
            lines = result.split('\n')
            for line in lines:
                s = line.strip()
                if s and s != cmd and len(s) > 2:
                    print(f"     {s}")

            rl = result.lower()

            # Death checks
            if "bulldozer" in rl and "demolished" in rl:
                print("\n💀 BULLDOZER GOT ME - GAME OVER")
                alive = False
            elif "type restart" in rl:
                print("\n💀 GAME OVER PROMPT")
                alive = False
            elif "you have died" in rl or "you are dead" in rl:
                print("\n💀 DIED")
                alive = False

            # Victory checks
            elif "heart of gold" in rl:
                print("\n🎉 ABOARD THE HEART OF GOLD!")
                alive = False  # Stop moving, we made it
                print("✅ ESCAPED EARTH!")
            elif "aboard" in rl or "spaceship" in rl:
                print("\n🎉 ON A SHIP!")
                alive = False
                print("✅ ESCAPED EARTH!")

            print()
            await asyncio.sleep(0.2)

        print("="*70)
        if alive:
            print(f"Game still running after {move_num} moves")
        print("="*70)

asyncio.run(main())
