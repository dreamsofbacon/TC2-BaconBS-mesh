import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "arthurdent"
BBS_PASS = "DontPanic42!"

class SSHReaderSession(asyncssh.SSHClientSession):
    def __init__(self):
        self.queue = asyncio.Queue()

    def data_received(self, data, datatype):
        self.queue.put_nowait(data)


async def read_output(session, timeout=2.0):
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
            if any(p in output for p in ["> ", "? "]):
                await asyncio.sleep(0.1)
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
    print("🎬 LIVE PLAY: ARTHUR DENT'S ESCAPE FROM EARTH")
    print("="*70 + "\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)
        
        # BBS login
        init = await read_output(session, 3.0)
        if "BBS username:" in init:
            chan.write(f"{BBS_USER}\r\n")
            await read_output(session, 2.0)
            chan.write(f"{BBS_PASS}\r\n")
            await read_output(session, 3.0)

        await asyncio.sleep(0.5)
        
        # Navigate to game
        chan.write("3\r\n")
        await asyncio.sleep(0.2)
        await read_output(session, 1.0)
        chan.write("4\r\n")
        await asyncio.sleep(0.2)
        await read_output(session, 1.0)
        chan.write("5\r\n")
        await asyncio.sleep(1.0)
        result = await read_output(session, 1.5)

        print("📖 GAME STARTING\n")

        # Check if saved game prompt
        if "Saved game restored" in result or "restore" in result.lower():
            print("[!] Game found a saved state")
            print("[*] Sending 'x' to start fresh...\n")
            chan.write("x\r\n")
            await asyncio.sleep(0.5)
            result = await read_output(session, 2.0)

        print("="*70)
        print("ARTHUR DENT WAKES UP ON THURSDAY MORNING")
        print("="*70 + "\n")

        # Game moves
        moves = [
            ("look", "Survey the bedroom"),
            ("take gown", "Take dressing gown"),
            ("wear gown", "Wear the gown"),
            ("south", "Exit to landing"),
            ("look", "Look at landing"),
            ("take towel", "Take the towel - MOST IMPORTANT ITEM"),
            ("south", "Go further south"),
            ("look", "Look"),
            ("take leaflet", "Take leaflet"),
            ("read leaflet", "Read it"),
            ("west", "Go west"),
            ("look", "Look"),
            ("north", "Go north"),
            ("look", "Look"),
            ("open door", "Open door"),
            ("west", "Go west"),
            ("look", "Look"),
            ("north", "Go north"),
            ("look", "Look"),
            ("east", "Go east"),
            ("look", "Look"),
            ("north", "Go north"),
            ("look", "Look around"),
        ]

        move_num = 0
        for cmd, desc in moves:
            move_num += 1
            print(f"[{move_num:2d}] {desc}")
            print(f"     > {cmd}")
            
            chan.write(f"{cmd}\r\n")
            result = await read_output(session, 1.5)
            
            # Show output
            for line in result.split('\n'):
                s = line.strip()
                if s and not s.startswith('>'):
                    print(f"     {s}")
            
            # Check for victory
            if "won" in result.lower() or "escaped" in result.lower():
                print("\n✅ VICTORY!\n")
                break
            
            # Check for death
            if "bulldozer" in result.lower() or "RESTART" in result:
                print("\n❌ DEAD - BULLDOZER HIT\n")
                break
            
            print()

        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
