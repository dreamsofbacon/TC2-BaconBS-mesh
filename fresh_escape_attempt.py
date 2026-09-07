import asyncio
import asyncssh
import time
import random
import string

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

# Create a fresh account each time
def gen_user():
    suffix = ''.join(random.choices(string.ascii_lowercase, k=6))
    return f"fresh_{suffix}"

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
            if any(p in output for p in ["> ", "? ", ": "]):
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
    print("🎮 FRESH ESCAPE ATTEMPT - ARTHUR DENT")
    print("="*70 + "\n")
    
    new_user = gen_user()
    new_pass = "DontPanic42!"
    
    print(f"Creating fresh account: {new_user}\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)
        
        # BBS login with NEW account
        init = await read_output(session, 3.0)
        if "BBS username:" in init:
            print(f"[+] Registering new account...")
            chan.write(f"new:{new_user}\r\n")
            result = await read_output(session, 2.0)
            print(f"    {result.strip()[:80]}")
            
            # Create password
            chan.write(f"{new_pass}\r\n")
            result = await read_output(session, 1.5)
            print(f"    {result.strip()[:80]}")
            
            # Confirm password
            chan.write(f"{new_pass}\r\n")
            await read_output(session, 3.0)
            print(f"[✓] Account created\n")

        await asyncio.sleep(0.5)
        
        # Navigate to game
        print(f"[+] Starting Hitchhiker's Guide...\n")
        chan.write("3\r\n")
        await asyncio.sleep(0.2)
        await read_output(session, 1.0)
        chan.write("4\r\n")
        await asyncio.sleep(0.2)
        await read_output(session, 1.0)
        chan.write("5\r\n")
        await asyncio.sleep(1.0)
        result = await read_output(session, 1.5)

        print("="*70)
        print("THURSDAY MORNING - ARTHUR WAKES UP")
        print("="*70 + "\n")

        # Execute moves
        moves = [
            ("look", "Look around"),
            ("take gown", "Take gown"),
            ("wear gown", "Wear gown"),
            ("south", "Go south - ESCAPE BEDROOM"),
            ("look", "Look"),
            ("take towel", "Take towel"),
            ("south", "Go south"),
            ("look", "Look"),
            ("take leaflet", "Take leaflet"),
            ("read leaflet", "Read leaflet"),
            ("west", "Go west"),
            ("look", "Look"),
            ("north", "Go north"),
            ("look", "Look"),
            ("open door", "Open door"),
            ("west", "Go west"),
            ("look", "Look around"),
            ("north", "Go north"),
            ("look", "Look"),
            ("east", "Go east"),
            ("look", "Look"),
            ("north", "Go north"),
            ("look", "Look around"),
            ("up", "Go up"),
            ("look", "Look"),
        ]

        move_num = 0
        for cmd, desc in moves:
            move_num += 1
            print(f"[{move_num:2d}] {desc} > {cmd}")
            
            chan.write(f"{cmd}\r\n")
            result = await read_output(session, 1.5)
            
            # Show relevant output
            for line in result.split('\n'):
                s = line.strip()
                if s and not s.startswith('>') and len(s) > 3:
                    print(f"     {s[:70]}")
            
            if "won" in result.lower() or "escaped" in result.lower():
                print("\n✅ ESCAPED!\n")
                break
            
            if "bulldozer" in result.lower() or "RESTART" in result:
                print("\n❌ DEAD\n")
                break
            
            print()

        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
