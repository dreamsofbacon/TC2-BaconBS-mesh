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
            if any(m in output for m in ["> ", "BBS username: ", "password: ", "Confirm password: ", "[ABCD]"]):
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


async def main():
    print("\n" + "="*70)
    print("🎯 TRIVIA KING - ARTHUR DENT HIGH SCORE CHALLENGE")
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
            
            if "BBS password:" in res:
                chan.write(f"{BBS_PASS}\r\n")
                await read_output(session, 3.0)

        await asyncio.sleep(0.5)
        
        async def send_cmd(cmd):
            chan.write(f"{cmd}\r\n")
            return await read_output(session, 2.5)

        # Navigate to Trivia King
        print("[+] Navigating to Trivia King...")
        await send_cmd("3")  # Utilities
        await asyncio.sleep(0.2)
        await send_cmd("4")  # Games
        await asyncio.sleep(0.2)
        result = await send_cmd("1")  # Trivia King
        print(result[:300] + "\n")
        await asyncio.sleep(1.0)

        # Play multiple trivia questions
        print("[+] Starting Trivia King session...\n")
        score = 0
        questions_answered = 0
        
        for round_num in range(1, 21):  # Try 20 questions
            # Get a question
            result = await send_cmd("n")  # Get new question
            await asyncio.sleep(0.5)
            
            print(f"\n{'='*70}")
            print(f"Question {round_num}")
            print(f"{'='*70}")
            print(result)
            
            # Parse the question to extract options
            lines = result.split('\n')
            options = {}
            for line in lines:
                if line.startswith('[A]'):
                    options['A'] = line.split('] ')[1] if '] ' in line else line
                elif line.startswith('[B]'):
                    options['B'] = line.split('] ')[1] if '] ' in line else line
                elif line.startswith('[C]'):
                    options['C'] = line.split('] ')[1] if '] ' in line else line
                elif line.startswith('[D]'):
                    options['D'] = line.split('] ')[1] if '] ' in line else line
            
            # Strategy: Try to guess intelligently
            # For now, let's try different answers to find the pattern
            # Start with A (first option often correct in trivia)
            guess = "A"
            
            print(f"\n>>> Answering with: {guess}")
            answer_result = await send_cmd(guess)
            print(answer_result)
            
            # Parse score from result
            if "score:" in answer_result.lower():
                # Try to extract score
                for line in answer_result.split('\n'):
                    if "score" in line.lower():
                        print(f"[Score Update] {line.strip()}")
                        questions_answered += 1
                        break
            elif "already answered" in answer_result.lower():
                print("[!] Question already answered, skipping...")
            
            await asyncio.sleep(0.8)
            
            # Check if trivia is over
            if "final score" in answer_result.lower() or "trivia king ended" in answer_result.lower():
                print("\n" + "="*70)
                print("🏁 TRIVIA SESSION ENDED")
                print("="*70)
                print(answer_result)
                break
        
        # Exit gracefully
        print("\n[+] Exiting...")
        await asyncio.sleep(0.5)
        await send_cmd("0")
        print("[✓] Disconnected\n")

if __name__ == "__main__":
    asyncio.run(main())
