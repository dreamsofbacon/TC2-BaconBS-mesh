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


async def read_output(session, timeout=2.5):
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
            # Early break on specific prompts
            if "Saved game restored" in output or "restore a saved position" in output.lower():
                await asyncio.sleep(0.15)
                while not session.queue.empty():
                    try:
                        output += session.queue.get_nowait()
                    except:
                        pass
                break
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
    print("🎮 ATTEMPT: SEND 'X' TO START FRESH")
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

        print("📖 Game prompt received")
        print(f"Content: {result[:200]}\n")

        # If we see restore prompt, send X to NOT restore
        if "Saved game restored" in result or "restore" in result.lower():
            print("[*] Saved game detected")
            print("[*] Sending 'X' to start FRESH...\n")
            chan.write("X\r\n")
            await asyncio.sleep(0.5)
            result = await read_output(session, 2.0)
            print(f"Response: {result[:300]}\n")

        # Now execute escape moves
        print("="*70)
        print("FRESH GAME - ATTEMPTING ESCAPE")
        print("="*70 + "\n")

        moves = [
            ("look", "Survey bedroom"),
            ("take gown", "Take gown"),
            ("wear gown", "Wear gown"),
            ("south", "🎯 LEAVE BEDROOM"),
            ("look", "Look"),
            ("take towel", "Take towel"),
            ("south", "Go south"),
            ("look", "Look"),
            ("take leaflet", "Take leaflet"),
            ("read leaflet", "Read it"),
            ("west", "Go west"),
            ("look", "Look"),
            ("north", "Go north"),
            ("look", "Look"),
            ("east", "Go east"),
            ("look", "Look"),
            ("north", "Go north"),
            ("up", "Go up"),
            ("look", "Look"),
            ("enter ship", "Enter the ship!"),
        ]

        move_num = 0
        for cmd, desc in moves:
            move_num += 1
            print(f"[{move_num:2d}] {desc}")
            print(f"     > {cmd}")
            
            chan.write(f"{cmd}\r\n")
            result = await read_output(session, 1.5)
            
            # Show output
            lines = result.strip().split('\n')
            for line in lines[:3]:  # Show first 3 lines
                if line.strip() and not line.strip().startswith('>'):
                    print(f"     {line}")
            
            # Check outcomes
            if any(x in result.lower() for x in ["won", "escaped", "victory"]):
                print("\n✅ VICTORY!\n")
                break
            
            if "bulldozer" in result.lower():
                print("\n❌ DEAD - BULLDOZER\n")
                break
            
            if any(x in result for x in ["RESTART", "RESTORE", "QUIT"]):
                print("\n❌ GAME OVER\n")
                break
            
            print()

        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
