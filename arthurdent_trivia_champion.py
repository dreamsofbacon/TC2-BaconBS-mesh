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

# Smart trivia answer strategy - we'll learn as we play
answer_patterns = {
    # Boolean questions - True/False
    'true': 'B',  # Start with False (B)
    'false': 'A',
    # Music/Entertainment questions - usually later options
    'music': ['B', 'C', 'D', 'A'],
    # Science questions - usually early options
    'science': ['A', 'B', 'C', 'D'],
    # Geography - often early options
    'geography': ['A', 'C', 'B', 'D'],
}

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


def guess_answer(question_text, options):
    """Smart guessing based on question content and options"""
    q_lower = question_text.lower()
    
    # Boolean questions - analyze options
    if '[A] True' in options or '[A] False' in options:
        if '[A] True' in options:
            # For true/false, default to B (False) as many questions are false
            return 'B'
        else:
            return 'A'
    
    # Look for keywords to make educated guesses
    keywords = {
        'who': 'A',      # People questions often have person in first answer
        'what': 'B',     # What questions often in middle
        'where': 'A',    # Geography usually early
        'when': 'D',     # Dates often at end
        'how': 'C',      # How questions middle-ish
        'currency': 'A', # Currency is often first specific answer
        'country': 'A',  # Countries usually first
        'capital': 'C',  # Capitals often later
        'first': 'A',    # First things usually early
        'last': 'D',     # Last things usually late
        'only': 'A',     # Only X questions favor early answers
        'longest': 'D',  # Longest/biggest often last
        'oldest': 'A',   # Oldest/first favor early
    }
    
    for keyword, answer in keywords.items():
        if keyword in q_lower:
            return answer
    
    # Default strategy: rotate through answers
    return 'A'


async def main():
    print("\n" + "="*70)
    print("🎯 TRIVIA KING - ARTHUR DENT CHAMPION MODE")
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
        await asyncio.sleep(1.0)

        # Play trivia questions
        print("[+] Starting Trivia King tournament...\n")
        final_score = 0
        questions_correct = 0
        questions_total = 0
        
        for round_num in range(1, 41):  # Play up to 40 questions
            result = await send_cmd("n")  # Get new question
            await asyncio.sleep(0.3)
            
            # Extract question and options
            lines = result.split('\n')
            question_text = ""
            options_text = ""
            in_options = False
            
            for line in lines:
                if '[A]' in line:
                    in_options = True
                if in_options:
                    options_text += line + "\n"
                elif '[A]' not in line and '[B]' not in line:
                    question_text += line + " "
            
            # Make intelligent guess
            guess = guess_answer(question_text, options_text)
            
            print(f"[Q{round_num:2d}] {question_text[:50].strip()}...", end=" ")
            
            # Send answer
            answer_result = await send_cmd(guess)
            
            # Check if correct
            if "Correct!" in answer_result:
                questions_correct += 1
                questions_total += 1
                # Extract points
                points_match = re.search(r'\+(\d+)', answer_result)
                if points_match:
                    print(f"✓ +{points_match.group(1)} pts")
                else:
                    print("✓")
            else:
                questions_total += 1
                # Show what the correct answer was
                if "answer was:" in answer_result.lower():
                    ans_match = re.search(r'answer was: ([A-D]|\w+)', answer_result)
                    if ans_match:
                        print(f"✗ (was {ans_match.group(1)})")
                    else:
                        print("✗")
                else:
                    print("✗")
            
            # Extract current score
            score_match = re.search(r'Score: (\d+)', answer_result)
            if score_match:
                final_score = int(score_match.group(1))
            
            # Check for end of game
            if "final score" in answer_result.lower() or "trivia king ended" in answer_result.lower():
                print("\n" + "="*70)
                print("🏁 TRIVIA SESSION ENDED")
                print("="*70)
                break
            
            await asyncio.sleep(0.5)
        
        # Show final results
        print("\n" + "="*70)
        print("📊 FINAL RESULTS FOR ARTHUR DENT")
        print("="*70)
        print(f"Final Score:      {final_score} points")
        print(f"Correct Answers:  {questions_correct}/{questions_total}")
        if questions_total > 0:
            accuracy = (questions_correct / questions_total) * 100
            print(f"Accuracy:         {accuracy:.1f}%")
        print("="*70 + "\n")
        
        # Exit gracefully
        await asyncio.sleep(0.5)
        await send_cmd("x")
        await asyncio.sleep(0.3)
        await send_cmd("0")
        print("[✓] Arthur Dent has left the BBS\n")

if __name__ == "__main__":
    asyncio.run(main())
