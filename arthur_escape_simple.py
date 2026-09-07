import asyncio
import asyncssh
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "arthurdent"
BBS_PASS = "DontPanic42!"

# Simplified escape sequence
MOVES = [
    ("out", "Get out of bed"),
    ("south", "Exit bedroom to landing"),
    ("look", "Look around the landing"),
    ("west", "Go west to Dent's study"),
    ("look", "Check out the study"),
    ("north", "Move north"),
    ("look", "Look"),
    ("enter ship", "Enter the alien ship"),
]

class SSHReaderSession(asyncssh.SSHClientSession):
    def __init__(self):
        self.queue = asyncio.Queue()

    def data_received(self, data, datatype):
        self.queue.put_nowait(data)


async def read_output(session, timeout=2.0):
    """Read output until prompt or timeout"""
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
            if any(p in output for p in ["> ", ">", "? "]):
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
    print("🎮 WATCHING ARTHUR DENT ESCAPE EARTH")
    print("="*70 + "\n")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("✓ Connected to BBS\n")

    async with conn:
        session_factory = lambda: SSHReaderSession()
        chan, session = await conn.create_session(session_factory)
        
        # Wait for and handle BBS login
        init = await read_output(session, 3.0)
        if "BBS username:" in init:
            chan.write(f"{BBS_USER}\r\n")
            await read_output(session, 2.0)
            chan.write(f"{BBS_PASS}\r\n")
            await read_output(session, 2.0)

        await asyncio.sleep(0.5)
        
        # Navigate to game
        print("→ Starting Hitchhiker's Guide...\n")
        chan.write("3\r\n")  # Utilities
        await asyncio.sleep(0.2)
        await read_output(session, 1.0)
        
        chan.write("4\r\n")  # Games
        await asyncio.sleep(0.2)
        await read_output(session, 1.0)
        
        chan.write("5\r\n")  # HHGTTG
        await asyncio.sleep(1.0)
        result = await read_output(session, 1.0)
        print(result)
        print()

        # Execute escape moves
        move_num = 0
        for cmd, description in MOVES:
            move_num += 1
            print(f"[{move_num}] {description}")
            print(f"    > {cmd}")
            
            chan.write(f"{cmd}\r\n")
            result = await read_output(session, 2.0)
            
            # Show game output
            for line in result.split('\n'):
                if line.strip() and not line.strip().startswith('>'):
                    print(f"    {line}")
            
            # Check for success
            if "escaped" in result.lower() or "won" in result.lower():
                print("\n✅ SUCCESS!\n")
                break
            
            # Check for death
            if any(x in result for x in ["RESTART", "RESTORE", "QUIT"]):
                print("\n❌ Game Over\n")
                break
            
            print()

        print("="*70)

if __name__ == "__main__":
    asyncio.run(main())
