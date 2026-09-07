import sqlite3
import json
import pickle
import os

# Read local trivia database
con = sqlite3.connect('trivia.db')
cur = con.cursor()

# Query all questions with their correct answers
print("[+] Reading trivia database...")
rows = cur.execute('''
    SELECT question, correct_answer, incorrect_answers, difficulty
    FROM questions
    ORDER BY id
''').fetchall()

print(f"[+] Found {len(rows)} questions")

# Build lookup dictionaries
question_to_answer = {}  # question text -> correct answer
answer_lookup = {}  # (question text, option letter) -> is_correct

for q, correct, incorrect, difficulty in rows:
    # Clean question text
    q_clean = q.strip()
    
    # Parse incorrect answers
    try:
        incorrect_list = json.loads(incorrect) if isinstance(incorrect, str) else incorrect
    except:
        incorrect_list = []
    
    # All options: correct + incorrect
    all_options = [correct] + incorrect_list
    
    # Shuffle not guaranteed to be in same order each time, but we'll store them
    question_to_answer[q_clean] = {
        'correct': correct,
        'all_options': all_options,
        'difficulty': difficulty
    }

# Show sample
print("\n[+] Sample questions and answers:")
for i, (q, data) in enumerate(list(question_to_answer.items())[:5], 1):
    print(f"\n{i}. Q: {q[:60]}...")
    print(f"   Correct: {data['correct']}")
    print(f"   Options: {data['all_options']}")

# Save as pickle
output_file = 'trivia_answers.pkl'
with open(output_file, 'wb') as f:
    pickle.dump(question_to_answer, f)
print(f"\n[✓] Saved {len(question_to_answer)} questions to {output_file}")

con.close()
