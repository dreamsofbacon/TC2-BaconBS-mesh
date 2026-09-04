"""Trivia King: a single-player multiple-choice door.

Grew out of the quiz door contributed by materva
(https://github.com/materva/TC2-BaconBS-mesh), rebuilt around the Open
Trivia Database. Questions are CC BY-SA 4.0; the attribution travels in the
database's own meta table, so a copy of the file carries its provenance.

Multiple choice rather than free text because that is what the source data
is: the wrong answers ship with every question, plenty of them are only
answerable as a choice ("Which of these came first?"), and a single letter
is a much kinder thing to ask of someone typing on a radio than an exact
spelling of "Sto-vo-kor".

Sessions are per-process and deliberately not persisted. A restart is rare,
a trivia round is short, and a half-finished question is not worth a table.
"""
import json
import os
import random
import sqlite3

GAME_ID = "trivia"
GAME_NAME = "Trivia King"
DEFAULT_DB = os.path.join("data", "trivia.db")

# Answers are offered as letters, so the count of options is capped at what
# stays readable in one radio message.
CHOICE_LETTERS = "ABCD"

UNAVAILABLE = ("Trivia King is unavailable: no question database on this node.\n"
               "Ask the operator to run scripts/fetch_trivia_questions.py, "
               "or set BBS_TRIVIA_DB.")

_sessions: dict = {}
_last_scores: dict = {}


def _db_path() -> str:
    return os.getenv("BBS_TRIVIA_DB", DEFAULT_DB)


def _draw_question():
    """One random question, or None when there is no usable database.

    The question set is data rather than code and is not required to be
    present, so a node without one is an ordinary state. Opening a missing
    file read-only raises OperationalError, which unhandled would reach
    whoever picked the game from the menu as a crash instead of an
    explanation.
    """
    path = os.path.abspath(_db_path())
    if not os.path.isfile(path):
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return con.execute(
            """SELECT q.id, ca.name, q.difficulty, q.points, q.question,
                      q.correct_answer, q.incorrect_answers
               FROM questions q JOIN categories ca ON ca.id = q.category_id
               ORDER BY random() LIMIT 1""").fetchone()
    except sqlite3.Error:
        # Present but unreadable, or an older schema: the same outcome for
        # the player as no database at all.
        return None
    finally:
        if con is not None:
            con.close()


def _format(session: dict) -> str:
    lines = [
        f"\U0001F3AF TRIVIA KING",
        f"{session['category']} - {session['difficulty']} - {session['points']}pt",
        "",
        session["question"],
        "",
    ]
    for letter, option in zip(CHOICE_LETTERS, session["options"]):
        lines.append(f"[{letter}] {option}")
    lines.append("")
    lines.append("Reply with a letter, N for a new question, or X to exit.")
    return "\n".join(lines)


def start(user_id):
    previous = _sessions.get(user_id)
    row = _draw_question()
    if not row:
        return UNAVAILABLE

    _, category, difficulty, points, question, correct, incorrect_json = row
    try:
        incorrect = [str(a) for a in json.loads(incorrect_json)]
    except (TypeError, ValueError):
        incorrect = []

    # True/False questions carry one wrong answer, multiple choice three.
    options = [str(correct), *incorrect][:len(CHOICE_LETTERS)]
    random.shuffle(options)

    _sessions[user_id] = {
        "category": category,
        "difficulty": difficulty,
        "points": int(points or 0),
        "question": question,
        "answer": str(correct),
        "options": options,
        "score": previous.get("score", 0) if previous else 0,
        "moves": previous.get("moves", 0) if previous else 0,
        # A question pays out once. Without this the same one could be scored
        # over and over: any unrecognised key reprinted it, and answering the
        # reprint scored again. One player took a single question from 600 to
        # 1000 that way and left 2200 on the public Hall of Fame.
        "answered": False,
    }
    return _format(_sessions[user_id])


def active(user_id) -> bool:
    return user_id in _sessions


def command(user_id, text):
    session = _sessions.get(user_id)
    if not session:
        return "No Trivia King game is active."

    entry = str(text or "").strip()
    lowered = entry.lower()

    if lowered in ("x", "q", "quit", "exit"):
        score, moves = session["score"], session["moves"]
        _last_scores[user_id] = (score, moves)
        _sessions.pop(user_id, None)
        return (f"Trivia King ended. Final score: {score} "
                f"({moves} question{'' if moves == 1 else 's'}). "
                "Your score was saved.")

    if lowered in ("n", "next"):
        return start(user_id)

    if session.get("answered"):
        # Scored already. This has to come before the letter is parsed, or a
        # second correct answer pays out again -- and before the reprint
        # below, which is what kept offering the chance.
        return ("You have already answered that one.\n"
                "Reply N for another question, or X to exit.")

    options = session["options"]
    index = CHOICE_LETTERS.find(lowered.upper()) if len(lowered) == 1 else -1
    if index < 0 or index >= len(options):
        # Re-showing the question costs one message and saves the player
        # scrolling back for the options they are being asked to choose from.
        return ("Answer with one of the listed letters.\n\n"
                + _format(session))

    session["answered"] = True
    session["moves"] += 1
    chosen = options[index]
    correct = chosen == session["answer"]
    if correct:
        session["score"] += session["points"]
        verdict = f"Correct! +{session['points']}"
    else:
        verdict = f"No - the answer was: {session['answer']}"
    return (f"{verdict}\nScore: {session['score']}\n\n"
            "Reply N for another question, or X to exit.")


def finish_score(user_id):
    session = _sessions.get(user_id)
    fallback = _last_scores.get(user_id, (0, 0))
    if session is None:
        return fallback
    return session.get("score", fallback[0]), session.get("moves", fallback[1])


def attribution() -> str:
    """Where the questions came from, for anyone who asks."""
    path = os.path.abspath(_db_path())
    if not os.path.isfile(path):
        return ""
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'attribution'").fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        if con is not None:
            con.close()
