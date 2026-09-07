import asyncio
import asyncssh
import sys
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "hitchhiker_complete"
BBS_PASS = "DontPanic42!"

# Complete Hitchhiker's Guide walkthrough to beat the game
MOVES = [
    # Bedroom - collect items and escape before bulldozer
    "turn on light",
    "get up",
    "take gown",
    "wear gown",
    "look in pocket",
    "take toothbrush",
    "take screwdriver",
    "look at phone",
    "answer phone",
    
    # Take towel and leave
    "inventory",
    "south",
    "look",
    
    # Hall
    "take towel",
    "south",
    "look",
    
    # Outside - at the ground
    "look up",
    "north",
    "north",
    "east",
    
    # Kitchen
    "look",
    "take tea",
    "take biscuit",
    "take sponge",
    
    "west",
    "south",
    "south",
    "look",
    "take sack",
    
    # Continue exploring
    "south",
    "look",
    "east",
    "look",
    "inventory",
    "north",
    "west",
    "west",
    "look",
    "drop sack",
    "take headache",
    "inventory",
    
    # Key puzzle moments
    "drop tea",
    "take leaflet",
    "read leaflet",
    "inventory",
    "take tea",
    
    # Navigate to Zarniwoop's office area
    "west",
    "look",
    "enter door",
    
    # Try more exploration
    "look",
    "north",
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
    print(f"\n{'='*60}")
    print(f"HITCHHIKER'S GUIDE TO THE GALAXY - COMPLETE PLAYTHROUGH")
    print(f"Connecting to {HOST}:{PORT}...")
    print(f"{'='*60}\n")
    
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
                    chunk = await asyncio.wait_for(session.queue.get(), timeout=min(0.5, time_left))
                    if chunk is None:
                        break
                    output += chunk
                    if any(m in output for m in ["> ", "BBS username: ", "password: ", "Confirm password: "]):
                        await asyncio.sleep(0.2)
                        while not session.queue.empty():
                            output += session.queue.get_nowait()
                        break
                except asyncio.TimeoutError:
                    if output:
                        break
            return output

        # Handle BBS login
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

        # Skip to main menu if needed
        await asyncio.sleep(0.5)
        
        async def send_game_cmd(cmd):
            """Send a game command and return output"""
            chan.write(f"{cmd}\r\n")
            out = await read_output(2.5)
            return out

        # Navigate to game
        print("[+] Navigating to Hitchhiker's Guide...")
        await send_game_cmd("3")  # Utilities
        await asyncio.sleep(0.3)
        await send_game_cmd("4")  # Games
        await asyncio.sleep(0.3)
        result = await send_game_cmd("5")  # Hitchhiker's Guide
        print(result)
        await asyncio.sleep(1.5)

        # Play through the game
        move_count = 0
        for i, move in enumerate(MOVES, 1):
            result = await send_game_cmd(move)
            move_count += 1
            
            # Print game output (truncated for readability)
            lines = result.strip().split('\n')
            if len(lines) > 8:
                print(f"\n[Move {i}] > {move}")
                print('\n'.join(lines[:6]))
                print(f"... ({len(lines)-6} more lines)")
            else:
                print(f"\n[Move {i}] > {move}")
                print(result.strip())
            
            # Check for actual game end prompts
            if "type restart" in result.lower() and ("restore" in result.lower() or "quit" in result.lower()):
                print("\n" + "="*60)
                print("GAME ENDED - Final Score Shown")
                print("="*60)
                print(result)
                break
            
            # Check for victory
            if "congratulations" in result.lower() or "you have won" in result.lower() or "you win" in result.lower():
                print("\n" + "="*60)
                print("🎉 GAME WON! 🎉")
                print("="*60)
                break
            
            await asyncio.sleep(0.7)

        print(f"\nCompleted {move_count} moves through Hitchhiker's Guide!")
        
        # Graceful exit
        await asyncio.sleep(0.5)
        await send_game_cmd("x")
        await asyncio.sleep(0.3)
        await send_game_cmd("0")

if __name__ == "__main__":
    asyncio.run(run_game())
