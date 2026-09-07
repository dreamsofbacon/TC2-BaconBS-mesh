import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

# Create a fresh unique username so there's zero chance of existing save files
USER_ID = "speed_runner"
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


async def main():
    print("\n" + "="*70)
    print(f"🚀 ARTHUR DENT: HITCHHIKER'S GUIDE TO THE GALAXY (User: {USER_ID})")
    print("="*70 + "\n")

    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("[✓] Connected via SSH\n")

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)

        # Registration / Login
        init = await read_until(session, "BBS username:", timeout=5.0)
        print(f"[AUTH INIT] {init.strip()[:100]}")
        
        chan.write(f"{USER_ID}\r\n")
        await asyncio.sleep(1.0)
        r1 = await read_all(session, 2.0)
        print(f"[AUTH R1] {r1.strip()[:100]}")
        
        if "BBS password:" in r1 or "password:" in r1.lower():
            chan.write(f"{BBS_PASS}\r\n")
            await asyncio.sleep(2.0)
            r2 = await read_all(session, 3.0)
            print(f"[AUTH R2] {r2.strip()[:100]}")
            if "authentication failed" in r2.lower():
                print("[!] Retrying auth...")
                chan.write(f"{USER_ID}\r\n")
                await asyncio.sleep(1.0)
                await read_all(session, 2.0)
                chan.write(f"{BBS_PASS}\r\n")
                await asyncio.sleep(2.0)
                r2 = await read_all(session, 3.0)
                print(f"[AUTH RETRY] {r2.strip()[:100]}")

        print(f"[✓] Authenticated as {USER_ID}\n")

        # Navigate to HHGTTG
        chan.write("3\r\n")
        await read_all(session, 1.5)

        chan.write("4\r\n")
        await read_all(session, 1.5)

        chan.write("5\r\n")
        await asyncio.sleep(2.0)
        opening = await read_all(session, 3.0)

        print("="*70)
        print("📖 GAME START:")
        print("="*70)
        print(opening.strip())
        print("="*70 + "\n")

        # Walkthrough sequence to escape Earth
        moves = [
            ("get out of bed", "Stand up from bed"),
            ("turn on light", "Turn on bedroom light"),
            ("take gown", "Take dressing gown"),
            ("wear gown", "Wear dressing gown"),
            ("open pocket", "Open pocket"),
            ("take analgesic", "Take headache medicine"),
            ("eat analgesic", "Swallow medicine"),
            ("take screwdriver", "Take screwdriver"),
            ("take toothbrush", "Take toothbrush"),
            ("south", "Go south to hallway"),
            ("take mail", "Take junk mail"),
            ("south", "Go out to front of house"),
            ("lie down", "Lie down in front of bulldozer"),
            ("wait", "Wait for Mr. Prosser"),
            ("wait", "Wait for Ford Prefect"),
            ("wait", "Wait for Ford to arrive"),
            ("wait", "Wait when Ford offers towel"),
            ("wait", "Wait for Ford to notice bulldozer"),
            ("wait", "Wait for Ford to talk to Prosser"),
            ("wait", "Wait for Prosser to lie down in mud"),
            ("follow ford", "Follow Ford toward the pub"),
            ("follow ford", "Follow Ford into the Horse 'n Groom"),
            ("drink beer", "Drink 1st pint of bitter"),
            ("drink beer", "Drink 2nd pint of bitter"),
            ("drink beer", "Drink 3rd pint of bitter"),
            ("out", "Leave the Pub immediately after house crashes!"),
            ("look", "Look at Country Lane with yapping dog"),
            ("take towel", "Take towel from Ford"),
            ("wait", "Wait for dog to gulp"),
            ("wait", "Wait for Vogon fleet arrival"),
            ("wait", "Wait for Ford to drop small black device"),
            ("take device", "TAKE THE SUB-ETHA SENS-O-MATIC DEVICE!"),
            ("press button", "PRESS THE BUTTON TO BEAM ABOARD!"),
            ("wait", "Beam aboard Vogon Constructor Ship!"),
            ("look", "Look around inside Vogon Hold!"),
        ]

        move_num = 0
        escaped = False

        for cmd, desc in moves:
            if escaped:
                break
            move_num += 1
            print(f"[{move_num:2d}] {desc}")
            print(f"     >>> {cmd}")

            chan.write(f"{cmd}\r\n")
            await asyncio.sleep(0.8)
            result = await read_all(session, 2.5)

            # Print game response lines
            lines = result.split('\n')
            for line in lines:
                s = line.rstrip()
                if s.strip() and s.strip() != cmd and not s.startswith('>'):
                    print(f"     {s}")

            rl = result.lower()

            if "vogon hold" in rl or "vogon constructor" in rl or "beam" in rl or "heart of gold" in rl:
                if "demolished" in rl or "ship" in rl or "hold" in rl:
                    print("\n✨ BEAMED ABOARD! EARTH IS DEMOLISHED!")

            if "start over" in rl or "peril-sensitive" in rl:
                print("\n💀 GAME OVER")
                break

            print()
            await asyncio.sleep(0.3)

        print("="*70)
        print(f"Completed {move_num} moves.")
        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
