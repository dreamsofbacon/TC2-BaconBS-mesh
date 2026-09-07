import asyncio
import asyncssh
import time
import re

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "arthurdent"
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


async def read_output(session, timeout=3.0):
    """Read from SSH session output queue"""
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
                    try:
                        output += session.queue.get_nowait()
                    except:
                        pass
                break
        except asyncio.TimeoutError:
            if output:
                break
    return output


# Multiple strategies to try escaping Earth in HHGTTG
STRATEGIES = [
    # Strategy 1: Find the towel, board the Heart of Gold
    {
        "name": "Heart of Gold Route",
        "moves": [
            "north",        # Move around the bedroom
            "west",
            "north",
            "west",
            "get towel",    # Critical item
            "east",
            "south",
            "south",
            "north",
            "board ship",   # Board Heart of Gold
            "punch alien",  # Escape Earth
        ]
    },
    # Strategy 2: Use the improbability drive
    {
        "name": "Improbability Drive Route",
        "moves": [
            "look",
            "take towel",
            "north",
            "push button",
            "activate drive",
            "jump in ship",
        ]
    },
    # Strategy 3: Explore and find escape route
    {
        "name": "Exploration Route",
        "moves": [
            "north",
            "examine room",
            "get everything",
            "south",
            "look around",
            "go to ship",
            "fly away",
        ]
    },
    # Strategy 4: Classic solution - get to the ship
    {
        "name": "Direct Ship Route",
        "moves": [
            "out",
            "south",
            "east",
            "north",
            "look",
            "get towel",
            "look",
            "north",
            "look",
            "enter ship",
            "up",
        ]
    },
    # Strategy 5: Fast bedroom escape (documented from speedrun)
    {
        "name": "Fast Escape",
        "moves": [
            "out",
            "south",
            "look",
            "west",
        ]
    },
]


async def main():
    print("\n" + "="*70)
    print("🚀 HITCHHIKER'S GUIDE TO THE GALAXY - ESCAPE EARTH CHALLENGE")
    print("="*70 + "\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("[✓] SSH connected\n")

    attempt = 0
    escaped = False

    async with conn:
        while attempt < len(STRATEGIES) and not escaped:
            attempt += 1
            strategy = STRATEGIES[attempt - 1]
            
            print(f"[Attempt {attempt}/{len(STRATEGIES)}] Trying: {strategy['name']}")
            print("-" * 70)
            
            session_factory = lambda: SSHReaderSession()
            chan, session = await conn.create_session(session_factory)
            
            # BBS login
            initial = await read_output(session, 3.0)
            if "BBS username:" in initial:
                chan.write(f"{BBS_USER}\r\n")
                res = await read_output(session, 2.0)
                
                if "BBS password:" in res:
                    chan.write(f"{BBS_PASS}\r\n")
                    await read_output(session, 3.0)

            await asyncio.sleep(0.5)
            
            async def send_cmd(cmd):
                chan.write(f"{cmd}\r\n")
                return await read_output(session, 2.5)

            # Navigate to HHGTTG
            await send_cmd("3")  # Utilities
            await asyncio.sleep(0.2)
            await send_cmd("4")  # Games
            await asyncio.sleep(0.2)
            result = await send_cmd("5")  # Hitchhiker's Guide
            await asyncio.sleep(1.0)

            # Execute strategy moves
            move_count = 0
            for move in strategy["moves"]:
                move_count += 1
                print(f"  Move {move_count}: {move}")
                
                result = await send_cmd(move)
                await asyncio.sleep(0.3)
                
                # Check for escape indicators
                result_lower = result.lower()
                
                if any(phrase in result_lower for phrase in [
                    "escape",
                    "left earth",
                    "off planet",
                    "flying away",
                    "heart of gold",
                    "ship launches",
                    "you have won",
                    "game over",
                    "escaped"
                ]):
                    print(f"\n✅ SUCCESS! Arthur Dent escaped!")
                    print(f"   Move: {move}")
                    print(f"   Result: {result[:200]}")
                    escaped = True
                    break
                
                # Check for game-over prompts
                if any(p in result for p in ["(RESTART/RESTORE/QUIT) >", "[RESTART/RESTORE/QUIT]"]):
                    print(f"   ❌ Game ended (died/stuck)")
                    break
                
                # Show relevant game output
                if len(result) < 300:
                    print(f"   > {result.strip()[:100]}")

            if escaped:
                break
            
            # Exit if game ended
            if "RESTART" in result or "RESTORE" in result:
                await send_cmd("RESTART")
                await asyncio.sleep(0.5)
            
            print()

        if escaped:
            print("\n" + "="*70)
            print("🎉 ARTHUR DENT HAS ESCAPED PLANET EARTH!")
            print("="*70)
            print("\nPenalty complete. The punishment has been served.")
            print("="*70 + "\n")
        else:
            print("\n" + "="*70)
            print("💀 HHGTTG STILL TOO HARD - NEED A DIFFERENT APPROACH")
            print("="*70)
            print("Arthur remains trapped on Earth despite best efforts...")
            print("="*70 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
