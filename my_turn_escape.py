import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

# Known-good account (from speedrun_hhgttg.py which worked)
BBS_USER = "speed_runner"
BBS_PASS = "DontPanic42!"

class SSHReaderSession(asyncssh.SSHClientSession):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    def data_received(self, data, datatype):
        self.queue.put_nowait(data)

    def connection_lost(self, exc):
        self.closed = True
        self.queue.put_nowait(None)


async def run_game():
    print("\n" + "="*70)
    print("🎮 MY TURN - I WILL ESCAPE EARTH")
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

        async def read_output(timeout=3.0):
            deadline = time.time() + timeout
            output = ""
            while time.time() < deadline:
                try:
                    time_left = max(0.1, deadline - time.time())
                    chunk = await asyncio.wait_for(
                        session.queue.get(),
                        timeout=min(0.5, time_left)
                    )
                    if chunk is None:
                        break
                    output += chunk
                    if any(m in output for m in [
                        "> ", "BBS username: ", "password: ",
                        "Confirm password: ", "(Type RESTART"
                    ]):
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

        # ---- BBS LOGIN (exact speedrun flow) ----
        initial = await read_output(3.0)
        print(f"[INIT] {initial.strip()[:100]}")

        if "BBS username:" in initial:
            chan.write(f"{BBS_USER}\r\n")
            res = await read_output(2.0)
            print(f"[USER RES] {res.strip()[:100]}")

            if "Create password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(2.0)
                print(f"[PASS RES] {res2.strip()[:100]}")
                if "Confirm password:" in res2:
                    chan.write(f"{BBS_PASS}\r\n")
                    res3 = await read_output(3.0)
                    print(f"[CONF RES] {res3.strip()[:100]}")
            elif "BBS password:" in res or "password:" in res.lower():
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(3.0)
                print(f"[PASS RES] {res2.strip()[:100]}")

        await asyncio.sleep(0.5)

        async def send(cmd, show=True):
            chan.write(f"{cmd}\r\n")
            result = await read_output(2.0)
            if show:
                # Show raw output for every move
                lines = [l.rstrip() for l in result.split('\n') if l.strip()]
                for l in lines:
                    print(f"  {l}")
            return result

        # ---- NAVIGATE TO GAME ----
        print("\n[*] Navigating: Utilities → Games → HHGTTG\n")
        await send("3", show=False)
        await asyncio.sleep(0.3)
        await send("4", show=False)
        await asyncio.sleep(0.3)
        r = await send("5", show=False)
        print(f"[GAME LAUNCH] {r.strip()[:200]}\n")
        await asyncio.sleep(1.5)

        # Read whatever the game prints on launch
        launch_output = await read_output(3.0)
        print("="*70)
        print("GAME OPENING TEXT:")
        print("="*70)
        print(launch_output)
        print("="*70 + "\n")

        # ---- MY ACTUAL MOVES ----
        # I know from watching:
        # 1. The game starts pitch black in bed
        # 2. "It is pitch black" means I need to get out of bed first
        # 3. The speedrun worked with: look → take gown → wear gown → south
        # 4. The bulldozer comes if you take too long
        # 5. I need to be FAST

        moves = [
            # --- ESCAPE BEDROOM (do this FAST before bulldozer) ---
            ("out",    "Get out of bed"),
            ("south",  "Head for the door"),
            ("south",  "Keep moving south"),
            ("south",  "Keep moving south"),
            ("south",  "Keep moving south"),
        ]

        escaped = False
        move_count = 0

        for cmd, desc in moves:
            move_count += 1
            print(f"[{move_count:2d}] {desc}")
            print(f"     >>> {cmd}")

            result = await send(cmd)
            print()

            rl = result.lower()

            # Check for bulldozer / death
            if "bulldozer" in rl or "demolished" in rl:
                print("❌ THE BULLDOZER GOT ME")
                print(f"   Game says: {result[-200:]}")
                break

            # Check for game over prompt
            if "type restart" in rl:
                print("❌ GAME OVER")
                break

            # Check if we're out of the bedroom
            if "front porch" in rl or "outside" in rl or "driveway" in rl:
                print("✅ OUT OF THE HOUSE!")
                escaped = True
                break

            await asyncio.sleep(0.3)

        if not escaped:
            print("\n[*] Trying more exploration moves...\n")
            # More moves - explore what's around
            extra_moves = [
                "look", "west", "east", "north",
                "examine house", "examine bulldozer",
                "yell", "wait", "look",
            ]
            for cmd in extra_moves:
                move_count += 1
                print(f"[{move_count:2d}] >>> {cmd}")
                result = await send(cmd)
                print()
                rl = result.lower()
                if "bulldozer" in rl or "type restart" in rl:
                    print("❌ DEAD")
                    break
                if any(x in rl for x in ["won", "victory", "aboard"]):
                    print("✅ ESCAPED!")
                    escaped = True
                    break
                await asyncio.sleep(0.3)

        print()
        print("="*70)
        if escaped:
            print("🎉 I ESCAPED EARTH!")
        else:
            print("💀 EARTH WINS THIS ROUND")
        print(f"    Moves used: {move_count}")
        print("="*70)

if __name__ == "__main__":
    asyncio.run(run_game())
