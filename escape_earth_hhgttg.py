import asyncio
import asyncssh
import time
import re

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "earthescaper"
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
    print("🚀 ESCAPE FROM PLANET EARTH - HITCHHIKER'S GUIDE")
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
        
        # BBS login
        initial = await read_output(session, 3.0)
        if "BBS username:" in initial:
            print("[+] Logging in as arthurdent...")
            chan.write(f"{BBS_USER}\r\n")
            res = await read_output(session, 2.0)
            
            if "Create password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(session, 2.0)
                if "Confirm password:" in res2:
                    chan.write(f"{BBS_PASS}\r\n")
                    await read_output(session, 3.0)
            elif "BBS password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                await read_output(session, 3.0)

        await asyncio.sleep(0.5)
        
        async def send_cmd(cmd):
            chan.write(f"{cmd}\r\n")
            return await read_output(session, 2.0)

        # Navigate to HHGTTG
        print("[+] Navigating to HHGTTG...")
        await send_cmd("3")  # Utilities
        await asyncio.sleep(0.2)
        await send_cmd("4")  # Games
        await asyncio.sleep(0.2)
        result = await send_cmd("5")  # Hitchhiker's Guide
        await asyncio.sleep(1.0)

        # Phase 1: ESCAPE THE BEDROOM
        print("\n" + "="*70)
        print("PHASE 1: ESCAPE THE BEDROOM")
        print("="*70)
        
        escape_moves = [
            "look",
            "take gown",
            "wear gown",
            "south",
        ]
        
        for move in escape_moves:
            result = await send_cmd(move)
            print(f"> {move}")
            if "left bedroom" in result.lower() or "front door" in result.lower():
                print("✓ Left the bedroom!\n")
                break
            await asyncio.sleep(0.3)

        # Phase 2: Get the TOWEL and explore
        print("="*70)
        print("PHASE 2: GET TOWEL & EXPLORE WORLD")
        print("="*70 + "\n")
        
        explore_moves = [
            "look",
            "north",
            "look",
            "take towel",
            "inventory",
            "look",
            "east",
            "look",
            "take leaflet",
            "look at leaflet",
            "west",
            "west",
            "look",
            "take drink",
            "inventory",
            "look",
            "south",
            "look",
            "south",
            "look",
            "east",
            "look",
            "take item",
            "north",
            "look",
            "east",
            "look",
            "take machine",
            "look",
            "push button",
            "look",
            "west",
            "look",
            "take wrench",
            "look",
            "north",
            "look",
            "take card",
            "look",
            "east",
            "look",
            "up",
            "look",
            "take thing",
            "look",
            "down",
            "look",
            "south",
            "look",
            "south",
            "look",
            "south",
            "look",
            "west",
            "look",
            "west",
            "look",
            "south",
            "look",
            "open hatch",
            "look",
            "enter hatch",
            "look",
            "north",
            "look",
            "examine controls",
            "push button",
            "look",
            "activate",
            "go up",
            "ascend",
            "up",
            "look",
        ]
        
        move_count = 0
        escaped = False
        for move in explore_moves:
            result = await send_cmd(move)
            move_count += 1
            
            # Compact output
            lines = result.strip().split('\n')
            score_line = [l for l in lines if 'Score:' in l]
            if score_line:
                print(f"[{move_count:3d}] > {move:25} {score_line[0]}")
            else:
                print(f"[{move_count:3d}] > {move:25}")
            
            # Check for escape from Earth
            earth_escape_phrases = [
                "leaves earth",
                "left earth",
                "escape earth",
                "leave planet",
                "outer space",
                "deep space",
                "heart of gold",
                "vogon",
                "ship",
                "spacecraft",
                "spaceship",
                "magrathea",
                "above earth",
                "orbital",
                "weightless",
                "zero gravity",
                "vacuum",
                "black space",
            ]
            
            result_lower = result.lower()
            for phrase in earth_escape_phrases:
                if phrase in result_lower:
                    print(f"\n{'='*70}")
                    print(f"🚀 ESCAPED EARTH! ({phrase.upper()})")
                    print(f"{'='*70}\n")
                    print(result)
                    print(f"\n{'='*70}")
                    print("MISSION ACCOMPLISHED - ARTHUR DENT IS OFF THE PLANET")
                    print(f"{'='*70}\n")
                    escaped = True
                    break
            
            if escaped:
                break
            
            # Check for game victory
            if any(x in result_lower for x in ["won!", "congratulations", "victory", "you win"]):
                print(f"\n{'='*70}")
                print("🏆 GAME WON!")
                print(f"{'='*70}\n")
                print(result)
                escaped = True
                break
            
            # Check for game over
            if ("type restart" in result_lower or "type quit" in result_lower) and "restore" in result_lower:
                print(f"\n[!] Game ended at move {move_count}")
                print("Last output:", result[-150:])
                break
            
            await asyncio.sleep(0.25)

        if not escaped:
            print(f"\n⚠️  Could not escape Earth in {move_count} moves")
            print("The puzzle requires specific item combinations or sequence we haven't found yet")
        
        print(f"\n[=] Total moves: {move_count}")
        
        # Exit
        await asyncio.sleep(0.5)
        await send_cmd("x")
        await asyncio.sleep(0.3)
        await send_cmd("0")
        print("\n[✓] Disconnected from BBS\n")

if __name__ == "__main__":
    asyncio.run(main())
