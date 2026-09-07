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
    print("🚀 ARTHUR DENT: ESCAPE FROM EARTH - THE REAL PLAYTHROUGH")
    print("="*70 + "\n")

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
        chan.write(f"{BBS_USER}\r\n")
        await asyncio.sleep(1.0)
        r1 = await read_all(session, 2.0)
        print(f"[LOGIN] {r1.strip()[:80]}")

        chan.write(f"{BBS_PASS}\r\n")
        await asyncio.sleep(2.0)
        r2 = await read_all(session, 4.0)
        print(f"[AUTH] {r2.strip()[:120]}")

        if "authentication failed" in r2.lower():
            print("❌ Login failed - aborting")
            return
        print("[✓] Logged in!\n")

        # Navigate to HHGTTG
        chan.write("3\r\n")
        await asyncio.sleep(1.0)
        await read_all(session, 1.5)
        chan.write("4\r\n")
        await asyncio.sleep(1.0)
        await read_all(session, 1.5)
        chan.write("5\r\n")
        await asyncio.sleep(5.0)
        opening = await read_all(session, 5.0)

        # The restored dead-end save replays slowly; probe with 'look' and
        # use the combined text to detect the state we landed in.
        chan.write("look\r\n")
        await asyncio.sleep(1.5)
        opening += await read_all(session, 4.0)

        print("="*70)
        print("📖 GAME OPENING:")
        print("="*70)
        print(opening)
        print("="*70 + "\n")

        # A dead-end save restore dumps us into the death text and then the
        # (RESTART/RESTORE/QUIT) prompt. Restart fresh BEFORE playing.
        if "start over" in opening.lower() or "Saved game restored" in opening or "RESTART" in opening:
            print("[!] Dead-end save restored - restarting fresh game\n")
            chan.write("restart\r\n")
            await asyncio.sleep(2.0)
            r = await read_all(session, 4.0)
            print(r)
            if "confirm" in r.lower() or "y/n" in r.lower():
                chan.write("y\r\n")
                await asyncio.sleep(2.0)
                r = await read_all(session, 4.0)
                print(r)
            print()

        # THE CANONICAL HHGTTG EARTH ESCAPE WALKTHROUGH
        # Learned from the last run: analgesic cures the headache (+10 pts),
        # no towel in bedroom, bulldozer timer is tight, and at Front of
        # House you must LIE DOWN immediately - going south gets you killed
        # by a flying brick. Lie down, wait for Ford, follow him to the
        # pub, drink beer, Vogons beam you up.
        moves = [
            "get out of bed",
            "turn on light",
            "take gown",
            "wear gown",
            "open pocket",
            "take analgesic",
            "take screwdriver",
            "take toothbrush",
            "south",
            "take mail",
            "south",
            "lie down",
            "wait",
            "wait",
            "wait",
            "wait",
            "wait",
            "stand",
            "follow ford",
            "south",
            "west",
            "wait",
            "drink beer",
            "drink beer",
            "wait",
            "look",
            "wait",
            "look",
        ]

        move_num = 0
        game_over = False
        escaped = False

        for cmd in moves:
            if game_over or escaped:
                break
            move_num += 1
            print(f"[{move_num:2d}] >>> {cmd}")

            chan.write(f"{cmd}\r\n")
            await asyncio.sleep(0.6)
            result = await read_all(session, 2.5)

            # Print full game response
            for line in result.split('\n'):
                s = line.rstrip()
                if s.strip() and s.strip() != cmd:
                    print(f"     {s}")

            rl = result.lower()

            # Death = actual death screen, not the echoed (Type RESTART...)
            # prompt fragment that lingers after a fresh restart.
            if "start over" in rl or "peril-sensitive" in rl:
                print("\n💀 GAME OVER")
                game_over = True
            # Success = reached the Vogon ship hold (game continues after
            # Earth's destruction only if Ford beamed us aboard).
            # NOTE: the death text also mentions Vogons, so check the
            # restart prompt is NOT present.
            elif "vogon hold" in rl or "heart of gold" in rl or \
                 ("demolished" in rl and "restart" not in rl and "score" not in rl):
                print("\n🎉 OFF PLANET EARTH!")
                escaped = True

            print()
            await asyncio.sleep(0.2)

        print("="*70)
        if escaped:
            print("🎉 SUCCESS: ARTHUR DENT IS OFF PLANET EARTH!")
        elif game_over:
            print("💀 FAILED: Earth wins again")
        else:
            print(f"⏸ Ran out of planned moves ({move_num})")
        print("="*70)

asyncio.run(main())
