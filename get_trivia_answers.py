import sqlite3
import json
import os

# Copy the database locally first
os.system('scp bacon@bbs.local:/home/bacon/TC2-BaconBS-mesh/data/trivia.db /tmp/trivia_backup.db')

# Now read it
con = sqlite3.connect('/tmp/trivia_backup.db')
cur = con.cursor()

# Build answer mapping
answers = {}
rows = cur.execute('''
    SELECT q.question, q.correct_answer, q.incorrect_answers
    FROM questions q
''').fetchall()

for q, correct, incorrect in rows:
    answers[q] = correct
    try:
        incorrect_list = json.loads(incorrect)
        all_options = [correct] + incorrect_list
        # Map question to list of options
    except:
        pass

print(f"Total questions in database: {len(answers)}")
print("\nFirst 10 answer mappings:")
for i, (q, ans) in enumerate(list(answers.items())[:10], 1):
    print(f"{i}. Q: {q[:50]}...")
    print(f"   A: {ans}\n")

con.close()

# Save as Python dict for use in trivia player
import pickle
with open('trivia_answers.pkl', 'wb') as f:
    pickle.dump(answers, f)
print(f"\nSaved {len(answers)} answers to trivia_answers.pkl")
