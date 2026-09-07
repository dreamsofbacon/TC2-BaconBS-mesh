import asyncio
import asyncssh
import sys
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "speed_runner"
BBS_PASS = "DontPanic42!"

# FAST opening sequence - escape bedroom before bulldozer
FAST_ESCAPE = [
    "look",
    "take gown",
    "wear gown",
    "south",
]

# After escaping bedroom, actual game exploration & puzzle solving
GAME_PLAY = [
    "look",
    "take towel",
    "south",
    "look",
    "take leaflet",
    "look at leaflet",
    "read leaflet",
    "west",
    "look",
    "north",
    "look",
    "open door",
    "west",
    "look",
    "take item",
    "south",
    "look",
    "take biscuit",
    "north",
    "north",
    "east",
    "look",
    "north",
    "look",
    "east",
    "look",
    "north",
    "look",
    "up",
    "look",
    "down",
    "west",
    "look",
    "take drink",
    "south",
    "look",
    "east",
    "look",
    "open hatch",
    "enter",
    "look",
    "take machine",
    "east",
    "look",
    "push button",
    "look",
    "west",
    "west",
    "look",
    "take wrench",
    "south",
    "south",
    "look",
    "take card",
    "inventory",
    "north",
    "north",
    "east",
    "look",
    "south",
    "look",
    "south",
    "look",
    "up",
    "look",
    "take thing",
]

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
    print(f"\n{'='*70}")
    print(f"HITCHHIKER'S GUIDE - SPEED RUN ATTEMPT")
    print(f"{'='*70}\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("[✓] SSH connected")

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)
        
        async def read_output(timeout=3.0):
            deadline = time.time() + timeout
            output = ""
            while time.time() < deadline:
                try:
                    time_left = max(0.1, deadline - time.time())
                    chunk = await asyncio.wait_for(session.queue.get(), timeout=min(0.5, time_left))
                    if chunk is None:
                        break
                    output += chunk
                    # Stop early on game prompts
                    if any(m in output for m in ["> ", "BBS username: ", "password: ", "Confirm password: "]):
                        await asyncio.sleep(0.15)
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

        # BBS login
        initial = await read_output(3.0)
        if "BBS username:" in initial:
            chan.write(f"{BBS_USER}\r\n")
            res = await read_output(2.0)
            
            if "Create password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(2.0)
                if "Confirm password:" in res2:
                    chan.write(f"{BBS_PASS}\r\n")
                    await read_output(3.0)
            elif "BBS password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                await read_output(3.0)

        await asyncio.sleep(0.5)
        
        async def send_cmd(cmd):
            """Send game command"""
            chan.write(f"{cmd}\r\n")
            return await read_output(2.0)

        # Navigate to game
        print("[+] Loading game...")
        await send_cmd("3")  # Utilities
        await asyncio.sleep(0.2)
        await send_cmd("4")  # Games
        await asyncio.sleep(0.2)
        result = await send_cmd("5")  # Hitchhiker's Guide
        print(result[:200] + "...\n")
        await asyncio.sleep(1.0)

        # SPEED RUN: Fast escape from bedroom
        print("[+] FAST ESCAPE from bedroom...")
        for i, move in enumerate(FAST_ESCAPE, 1):
            result = await send_cmd(move)
            print(f"  [{i}] > {move}")
            # Show game output
            for line in result.split('\n'):
                s = line.strip()
                if s and not s.startswith('>') and 'password' not in s.lower():
                    print(f"      {s}")
            if "You miss the doorway" not in result and "You can't go" not in result:
                if "south" in move:
                    print("    ✓ Left bedroom!")
                    break
            await asyncio.sleep(0.3)

        # Now explore the wider game world
        print("\n[+] EXPLORING wider game world...")
        move_count = 0
        for i, move in enumerate(GAME_PLAY, 1):
            result = await send_cmd(move)
            move_count += 1
            
            # Compact output display
            lines = result.strip().split('\n')
            score_line = [l for l in lines if 'Score:' in l]
            if score_line:
                print(f"  [{i}] > {move:25} {score_line[0]}")
            else:
                print(f"  [{i}] > {move}")
            
            # Check for ACTUAL game end conditions (more specific)
            result_lower = result.lower()
            if ("type restart" in result_lower and "restore" in result_lower) or \
               ("type restart" in result_lower and "quit" in result_lower):
                print("\n[!] Game ended or puzzle failure!")
                print(result[-300:])
                # Try to restart and continue
                await send_cmd("restart")
                await asyncio.sleep(1.0)
                break
            
            # Victory condition
            if any(x in result_lower for x in ["won", "congratulations", "victory"]):
                print(f"\n{'='*70}")
                print("🎉 GAME WON! 🎉")
                print(f"{'='*70}")
                print(result)
                break
            
            await asyncio.sleep(0.4)

        print(f"\n[=] Completed {move_count} gameplay moves")
        
        # Exit gracefully
        await asyncio.sleep(0.5)
        await send_cmd("x")
        await asyncio.sleep(0.3)
        await send_cmd("0")
        print("[✓] Disconnected\n")

if __name__ == "__main__":
    asyncio.run(run_game())
