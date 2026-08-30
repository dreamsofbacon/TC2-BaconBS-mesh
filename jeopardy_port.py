"""Single-player Jeopardy door backed by the captured clue archive."""
import os, random, sqlite3, re

GAME_ID = "jeopardy"
DEFAULT_DB = os.path.join("data", "jeopardy.db")
_sessions = {}
_last_scores = {}

def _db_path():
    return os.getenv("BBS_JEOPARDY_DB", DEFAULT_DB)

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

UNAVAILABLE = ("Jeopardy is unavailable: no clue archive on this node.\n"
               "Ask the operator to install it, or set BBS_JEOPARDY_DB.")


def _draw_clue():
    """One random clue, or None when there is no usable archive.

    The archive is data rather than code and is not shipped with the BBS, so
    a node can perfectly well be running this without one. Opening a missing
    file read-only raises OperationalError, which unhandled would surface as
    a crash to whoever picked Jeopardy from the games menu rather than as a
    message telling them why it did not start.
    """
    path = os.path.abspath(_db_path())
    if not os.path.isfile(path):
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return con.execute("""SELECT c.id, ca.name, c.value, c.clue_text, c.correct_response
                              FROM clues c JOIN categories ca ON ca.id=c.category_id
                              WHERE c.clue_text IS NOT NULL AND c.correct_response IS NOT NULL
                              ORDER BY random() LIMIT 1""").fetchone()
    except sqlite3.Error:
        # A present but unreadable or wrong-schema archive is the same
        # outcome for the player as no archive at all.
        return None
    finally:
        if con is not None:
            con.close()


def start(user_id):
    previous = _sessions.get(user_id)
    row = _draw_clue()
    if not row:
        return UNAVAILABLE
    _sessions[user_id] = {"id": row[0], "category": row[1], "value": row[2] or 0,
                           "clue": row[3], "answer": row[4],
                           "score": previous.get("score", 0) if previous else 0,
                           "moves": previous.get("moves", 0) if previous else 0}
    s = _sessions[user_id]
    return f"🎤 JEOPARDY!\nCategory: {s['category']}\nValue: ${s['value']}\n\n{s['clue']}\n\nReply with your answer, N for a new clue, or X to exit."

def active(user_id): return user_id in _sessions

def command(user_id, text):
    s = _sessions.get(user_id)
    if not s: return "No Jeopardy game is active."
    t = text.strip()
    if t.lower() in ("x", "q", "quit", "exit"):
        score, moves = s["score"], s["moves"]
        _last_scores[user_id] = (score, moves)
        _sessions.pop(user_id, None)
        return f"Jeopardy ended. Final score: ${score} ({moves} clues). Your score was saved."
    if t.lower() in ("n", "next"):
        return start(user_id)
    s["moves"] += 1
    correct = _norm(t) == _norm(s["answer"])
    if correct: s["score"] += s["value"]
    result = "Correct!" if correct else f"Sorry — the response was: {s['answer']}"
    return f"{result}\nScore: ${s['score']}\n\nReply N for another clue, or X to exit."

def finish_score(user_id):
    s = _sessions.get(user_id)
    return (s or {}).get("score", _last_scores.get(user_id, (0, 0))[0]), (s or {}).get("moves", _last_scores.get(user_id, (0, 0))[1])
