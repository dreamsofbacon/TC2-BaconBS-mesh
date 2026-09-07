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

# Solution sequence for escaping Earth in HHGTTG
# Based on classic Infocom walkthrough
ESCAPE_SEQUENCE = [
    ("look", "Observe surroundings"),
    ("get gown", "Pick up dressing gown"),
    ("wear gown", "Wear the gown"),
    ("south", "Leave bedroom"),
    ("look", "Check new area"),
    ("take towel", "Get the towel (most important item!)"),
    ("north", "Back to bedroom"),
    ("look", "Check around"),
    ("get slippers", "Get slippers"),
    ("south", "Exit again"),
    ("take leaflet", "Pick up leaflet"),
    ("read leaflet", "Read the leaflet"),
    ("south", "Go south"),
    ("look", "Survey the area"),
    ("west", "Move west"),
    ("look", "Look around"),
    ("take bottle", "Get bottle"),
    ("open bottle", "Open it"),
    ("drink", "Drink it"),
    ("east", "Go back east"),
    ("south", "Go south"),
    ("look", "Look around"),
    ("west", "Go west"),
    ("look", "Check this area"),
    ("take biscuit", "Take biscuit"),
    ("east", "Return east"),
    ("north", "Go north"),
    ("look", "Survey"),
    ("enter ship", "Board the Heart of Gold"),
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


async def read_output(session, timeout=2.5):
    """Read from SSH session output queue"""
    deadline = time.time() + timeout
    output = ""
    while time.time() < deadline:
        try:
            time_left = max(0.1, deadline - time.time())
            chunk = await asyncio.wait_for(session.queue.get(), timeout=min(0.3, time_left))
            if chunk is None:
                break
            output += chunk
            # Break on common prompts
            if any(p in output for p in ["> ", "prompt >", ":> ", "? >"]):
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


async def main():
    print("\n" + "="*70)
    print("🚀 HITCHHIKER'S GUIDE - ARTHUR DENT ESCAPE EARTH MISSION")
    print("="*70 + "\n")
    print("[WATCH MODE] Observing live gameplay...\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("[✓] SSH connected to bbs.local:2222\n")

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)
        
        # BBS login
        print("[*] Awaiting BBS login prompt...")
        initial = await read_output(session, 4.0)
        
        if "BBS username:" in initial:
            print(f"[+] BBS prompt received")
            chan.write(f"{BBS_USER}\r\n")
            res = await read_output(session, 2.0)
            
            if "BBS password:" in res or "password:" in res.lower():
                print(f"[+] Sending password...")
                chan.write(f"{BBS_PASS}\r\n")
                await read_output(session, 3.0)
                print(f"[+] Login complete\n")

        await asyncio.sleep(0.8)
        
        async def send_cmd(cmd):
            chan.write(f"{cmd}\r\n")
            return await read_output(session, 2.0)

        # Navigate to HHGTTG
        print("[*] Navigating to Hitchhiker's Guide game...")
        await send_cmd("3")  # Utilities
        await asyncio.sleep(0.3)
        await send_cmd("4")  # Games
        await asyncio.sleep(0.3)
        result = await send_cmd("5")  # Hitchhiker's Guide
        await asyncio.sleep(1.2)
        print("[✓] Game loaded\n")

        print("="*70)
        print("📖 GAME STARTED - ARTHUR DENT ON EARTH")
        print("="*70)
        print()

        # Execute escape sequence
        move_count = 0
        escaped = False
        game_active = True

        for move, description in ESCAPE_SEQUENCE:
            if not game_active:
                break
                
            move_count += 1
            
            # Show move being attempted
            print(f"[Move {move_count:2d}] {description}")
            print(f"        Command: > {move}")
            
            result = await send_cmd(move)
            await asyncio.sleep(0.4)
            
            # Display game output
            lines = result.split('\n')
            for line in lines:
                if line.strip() and not line.strip().startswith('>'):
                    print(f"        {line}")
            
            # Check for escape success
            result_lower = result.lower()
            if any(phrase in result_lower for phrase in [
                "you have won",
                "game over",
                "heart of gold",
                "you've escaped",
                "planet earth destroyed",
            ]):
                print()
                print("✅ ESCAPE SUCCESSFUL!")
                escaped = True
                break
            
            # Check for game end/death
            if any(p in result for p in ["(RESTART", "(Type RESTART", "RESTART/RESTORE/QUIT"]):
                print()
                print("❌ ARTHUR DENT DIED")
                game_active = False
                break
            
            print()

        print()
        print("="*70)
        if escaped:
            print("🎉 MISSION ACCOMPLISHED!")
            print(f"Arthur Dent escaped Earth in {move_count} moves!")
        else:
            print("💀 MISSION FAILED")
            print(f"Game ended after {move_count} moves")
        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
