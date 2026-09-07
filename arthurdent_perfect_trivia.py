import asyncio
import asyncssh
import time
import re
import pickle
import difflib

HOST = "bbs.local"
PORT = 2222
SSH_USER = "nan"
SSH_PASS = "nannannan1"

BBS_USER = "arthurdent"
BBS_PASS = "DontPanic42!"

# Load trivia answer lookup
print("[+] Loading trivia answer database...")
with open('trivia_answers.pkl', 'rb') as f:
    answer_lookup = pickle.load(f)
print(f"[✓] Loaded {len(answer_lookup)} trivia questions\n")


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


def find_best_question_match(game_question, lookup):
    """Find the best matching question in the database"""
    # Normalize game question
    game_q_norm = game_question.strip().lower()
    
    # Try exact match first
    for db_q in lookup.keys():
        if db_q.lower() == game_q_norm:
            return db_q
    
    # Try substring match
    for db_q in lookup.keys():
        if game_q_norm in db_q.lower() or db_q.lower() in game_q_norm:
            return db_q
    
    # Use difflib to find closest match
    close_matches = difflib.get_close_matches(game_question, lookup.keys(), n=1, cutoff=0.6)
    if close_matches:
        return close_matches[0]
    
    return None


def extract_answer_letter(options_text, correct_answer):
    """Find which letter [A][B][C][D] has the correct answer"""
    lines = options_text.strip().split('\n')
    
    for line in lines:
        # Match patterns like "[A] Answer text"
        match = re.match(r'\[([A-D])\]\s*(.*)', line)
        if match:
            letter = match.group(1)
            option_text = match.group(2).strip()
            
            # Check if this option contains the correct answer
            if correct_answer.lower() in option_text.lower() or option_text.lower() in correct_answer.lower():
                return letter
    
    # Fallback: try to match partial words
    correct_words = correct_answer.split()
    for line in lines:
        match = re.match(r'\[([A-D])\]\s*(.*)', line)
        if match:
            letter = match.group(1)
            option_text = match.group(2).lower()
            # Count how many words from correct answer are in this option
            matches = sum(1 for word in correct_words if word.lower() in option_text)
            if matches > len(correct_words) * 0.5:  # At least 50% word match
                return letter
    
    return 'A'  # Default fallback


async def main():
    print("="*70)
    print("🎯 TRIVIA KING - ARTHUR DENT PERFECT SCORE ATTEMPT")
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
        print("[+] Starting Trivia King tournament with perfect answer matching...\n")
        final_score = 0
        questions_correct = 0
        questions_total = 0
        questions_skipped = 0
        
        for round_num in range(1, 51):  # Play up to 50 questions
            result = await send_cmd("n")  # Get new question
            await asyncio.sleep(0.3)
            
            # Parse question and options
            lines = result.split('\n')
            question_text = ""
            options_text = ""
            in_options = False
            category = ""
            
            for i, line in enumerate(lines):
                # Category line typically has ": " and difficulty
                if ':' in line and any(diff in line for diff in ['easy', 'medium', 'hard']):
                    category = line.strip()
                
                if '[A]' in line:
                    in_options = True
                
                if in_options:
                    options_text += line + "\n"
                elif '[A]' not in line and '[B]' not in line and '[C]' not in line and '[D]' not in line:
                    if line.strip() and ':' not in line or 'Question' in line:
                        question_text += line + " "
            
            question_text = question_text.strip()
            
            # Find matching question in database
            db_question = find_best_question_match(question_text, answer_lookup)
            
            if db_question:
                correct_answer = answer_lookup[db_question]['correct']
                guess = extract_answer_letter(options_text, correct_answer)
                
                print(f"[Q{round_num:2d}] {question_text[:50]}...", end=" ")
                
                # Send answer
                answer_result = await send_cmd(guess)
                
                # Check if correct
                if "Correct!" in answer_result:
                    questions_correct += 1
                    questions_total += 1
                    points_match = re.search(r'\+(\d+)', answer_result)
                    if points_match:
                        print(f"✓ +{points_match.group(1)} [{guess}]")
                    else:
                        print(f"✓ [{guess}]")
                else:
                    questions_total += 1
                    if "answer was:" in answer_result.lower():
                        ans_match = re.search(r'answer was: ([A-D]|\w+)', answer_result)
                        if ans_match:
                            print(f"✗ {ans_match.group(1)} vs guessed {guess}")
                        else:
                            print(f"✗")
                    else:
                        print(f"✗")
            else:
                # Couldn't find question in database - guess A
                questions_skipped += 1
                print(f"[Q{round_num:2d}] {question_text[:50]}... (skipped - no match)", end=" ")
                answer_result = await send_cmd("A")
                if "Correct!" in answer_result:
                    questions_correct += 1
                questions_total += 1
                print()
            
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
        print(f"Final Score:       {final_score} points")
        print(f"Correct Answers:   {questions_correct}/{questions_total}")
        print(f"Questions Skipped: {questions_skipped}")
        if questions_total > 0:
            accuracy = (questions_correct / questions_total) * 100
            print(f"Accuracy:          {accuracy:.1f}%")
        print("="*70 + "\n")
        
        # Exit gracefully
        await asyncio.sleep(0.5)
        await send_cmd("x")
        await asyncio.sleep(0.3)
        await send_cmd("0")
        print("[✓] Arthur Dent has left the BBS\n")

if __name__ == "__main__":
    asyncio.run(main())
