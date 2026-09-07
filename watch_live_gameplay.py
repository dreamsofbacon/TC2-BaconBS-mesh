import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "speed_runner"
BBS_PASS = "DontPanic42!"

# Extended escape sequence
MOVES = [
    # Escape bedroom
    "look",
    "take gown",
    "wear gown",
    "south",
    
    # Explore and gather items
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
    print(f"🎬 WATCHING ARTHUR DENT'S ADVENTURE")
    print(f"{'='*70}\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("[✓] Connected to bbs.local:2222")

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)
        
        async def read_output(timeout=2.5):
            deadline = time.time() + timeout
            output = ""
            while time.time() < deadline:
                try:
                    time_left = max(0.1, deadline - time.time())
                    chunk = await asyncio.wait_for(session.queue.get(), timeout=min(0.5, time_left))
                    if chunk is None:
                        break
                    output += chunk
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
        print(f"[+] Logging in as {BBS_USER}...")
        if "BBS username:" in initial:
            chan.write(f"{BBS_USER}\r\n")
            res = await read_output(2.0)
            
            if "Create password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(2.0)
                if "Confirm password:" in res2:
                    chan.write(f"{BBS_PASS}\r\n")
                    await read_output(3.0)
            elif "BBS password:" in res or "password:" in res.lower():
                chan.write(f"{BBS_PASS}\r\n")
                await read_output(3.0)
            
            print(f"[✓] Logged in\n")

        await asyncio.sleep(0.5)
        
        async def send_cmd(cmd):
            chan.write(f"{cmd}\r\n")
            return await read_output(2.0)

        # Navigate to game
        print("[*] Loading Hitchhiker's Guide...\n")
        await send_cmd("3")  # Utilities
        await asyncio.sleep(0.2)
        await send_cmd("4")  # Games
        await asyncio.sleep(0.2)
        await send_cmd("5")  # HHGTTG
        await asyncio.sleep(1.2)

        print(f"{'='*70}")
        print("📖 GAME STARTED - THURSDAY MORNING")
        print(f"{'='*70}\n")

        # Execute all moves and show output
        for i, move in enumerate(MOVES, 1):
            result = await send_cmd(move)
            
            # Extract and display game text
            print(f"[{i:2d}] > {move}")
            
            # Show relevant game output
            lines = result.strip().split('\n')
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith('>') and not stripped.startswith('Username'):
                    if 'password' not in stripped.lower() and 'create' not in stripped.lower():
                        print(f"     {stripped}")
            
            print()
            
            # Check for victory
            if any(x in result.lower() for x in ["won", "congratulations", "victory", "score"]):
                print(f"\n{'='*70}")
                print("✅ GAME COMPLETE!")
                print(f"{'='*70}\n")
                break
            
            # Check for death
            if "Type RESTART" in result or "RESTART/RESTORE/QUIT" in result:
                print(f"❌ GAME OVER\n")
                break
            
            await asyncio.sleep(0.35)

        print(f"[✓] Playthrough complete - {i} moves executed\n")

if __name__ == "__main__":
    asyncio.run(run_game())
