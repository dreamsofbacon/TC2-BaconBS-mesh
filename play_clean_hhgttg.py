import asyncio
import asyncssh
import sys
import time

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "arthurdent2"
BBS_PASS = "DontPanic42!"

MOVES = [
    "turn on light",
    "get up",
    "take gown",
    "wear gown",
    "open pocket",
    "look in pocket",
    "take toothbrush",
    "take screwdriver",
    "south",
    "look",
    "inventory",
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
    print(f"Connecting via SSH to {HOST}:{PORT}...")
    
    conn = await asyncssh.connect(
        HOST, port=PORT,
        username=SSH_USER,
        password=SSH_PASS,
        known_hosts=None
    )
    print("SSH transport connected successfully!\n")

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
                        await asyncio.sleep(0.3)
                        while not session.queue.empty():
                            output += session.queue.get_nowait()
                        break
                except asyncio.TimeoutError:
                    if output:
                        break
            return output

        # Initial prompt
        initial = await read_output(3.0)
        print(initial, end="")

        if "BBS username:" in initial:
            print(f">>> Registering BBS user: {BBS_USER}")
            chan.write(f"{BBS_USER}\r\n")
            res = await read_output(2.0)
            print(res, end="")
            
            if "Create password:" in res:
                print(f">>> Entering password: {BBS_PASS}")
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(2.0)
                print(res2, end="")
                if "Confirm password:" in res2:
                    print(f">>> Confirming password: {BBS_PASS}")
                    chan.write(f"{BBS_PASS}\r\n")
                    res3 = await read_output(3.0)
                    print(res3, end="")
            elif "BBS password:" in res:
                print(f">>> Logging in with password: {BBS_PASS}")
                chan.write(f"{BBS_PASS}\r\n")
                res2 = await read_output(3.0)
                print(res2, end="")

        async def send_cmd(cmd):
            print(f"\n==========================================")
            print(f">>> COMMAND: {cmd}")
            print(f"==========================================")
            chan.write(f"{cmd}\r\n")
            out = await read_output(3.0)
            print(out.strip())
            await asyncio.sleep(1.0)
            return out

        # Navigate to Hitchhiker's Guide
        await send_cmd("3")  # Utilities Menu
        await send_cmd("4")  # Games Menu
        await send_cmd("5")  # Hitchhiker's Guide

        # Play moves
        for move in MOVES:
            await send_cmd(move)

        # Exit cleanly
        print("\n--- Exiting Game & BBS ---")
        await send_cmd("x")
        await send_cmd("0")

if __name__ == "__main__":
    asyncio.run(run_game())
