#!/usr/bin/env python3
"""Build the Trivia King question database from the Open Trivia Database.

Run this to create or top up data/trivia.db:

    python scripts/fetch_trivia_questions.py --target 1000

OpenTDB content is CC BY-SA 4.0: it may be redistributed with attribution,
and derivatives share alike. The attribution is written into the database's
own meta table rather than left to a README nobody ships, so a copy of the
file always carries its provenance.

Notes on the API, each of which is a way this quietly goes wrong:

  * A session token is requested first. Without one the API samples with
    replacement and a few hundred "new" questions are mostly repeats.
    Token exhaustion (response_code 4) means every question has been served
    and is the correct place to stop, not an error.
  * 50 per request is the documented maximum; asking for more returns none.
  * The API rate-limits to roughly one request every five seconds and
    answers code 5 when exceeded, so the delay is not optional politeness.
  * Text comes HTML-escaped (&quot;, &#039;, &amp;). Unescaped once here,
    because a BBS sends plain text to a radio, not markup to a browser.
"""
import argparse
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://opentdb.com/api.php"
TOKEN_URL = "https://opentdb.com/api_token.php"
BATCH_SIZE = 50          # API maximum per request
REQUEST_SPACING = 5.0    # seconds; the API rate-limits below this

SOURCE_NAME = "Open Trivia Database (opentdb.com)"
SOURCE_LICENSE = "CC BY-SA 4.0"
SOURCE_URL = "https://opentdb.com"

# The BBS shows a points value per question. OpenTDB grades by difficulty,
# so that grading is what the score is built from.
POINTS_BY_DIFFICULTY = {"easy": 100, "medium": 200, "hard": 300}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id       INTEGER NOT NULL REFERENCES categories(id),
    difficulty        TEXT NOT NULL,
    points            INTEGER NOT NULL,
    type              TEXT NOT NULL,
    question          TEXT NOT NULL,
    correct_answer    TEXT NOT NULL,
    incorrect_answers TEXT NOT NULL,
    fingerprint       TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_questions_category ON questions (category_id);
"""


def _get(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def request_token() -> str:
    return _get(TOKEN_URL, {"command": "request"})["token"]


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for key, value in (
        ("source", SOURCE_NAME),
        ("source_url", SOURCE_URL),
        ("license", SOURCE_LICENSE),
        ("license_url", "https://creativecommons.org/licenses/by-sa/4.0/"),
        ("attribution",
         f"Questions from {SOURCE_NAME}, used under {SOURCE_LICENSE}."),
    ):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
    conn.commit()
    return conn


def _category_id(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    return conn.execute(
        "SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]


def store(conn: sqlite3.Connection, results: list) -> int:
    """Insert a batch, skipping questions already held. Returns new rows."""
    added = 0
    for item in results:
        question = html.unescape(str(item.get("question") or "")).strip()
        correct = html.unescape(str(item.get("correct_answer") or "")).strip()
        wrong = [html.unescape(str(a)).strip()
                 for a in (item.get("incorrect_answers") or [])]
        if not question or not correct or not wrong:
            continue
        difficulty = str(item.get("difficulty") or "medium").lower()
        category = html.unescape(str(item.get("category") or "General")).strip()
        cursor = conn.execute(
            """INSERT OR IGNORE INTO questions
               (category_id, difficulty, points, type, question,
                correct_answer, incorrect_answers, fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_category_id(conn, category), difficulty,
             POINTS_BY_DIFFICULTY.get(difficulty, 200),
             str(item.get("type") or "multiple"), question, correct,
             json.dumps(wrong, ensure_ascii=False),
             # The question text alone: the same question served twice with
             # its wrong answers in a different order is still a repeat.
             question.casefold()))
        added += cursor.rowcount
    conn.commit()
    return added


def harvest(conn: sqlite3.Connection, target: int, verbose: bool = True) -> int:
    token = request_token()
    added_total = 0
    consecutive_empty = 0

    while added_total < target:
        try:
            payload = _get(API_URL, {"amount": BATCH_SIZE, "token": token})
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            print(f"  request failed ({exc}); retrying", file=sys.stderr)
            time.sleep(REQUEST_SPACING * 2)
            continue

        code = int(payload.get("response_code", -1))
        if code == 0:
            added = store(conn, payload.get("results") or [])
            added_total += added
            consecutive_empty = consecutive_empty + 1 if added == 0 else 0
            if verbose:
                print(f"  {added_total}/{target} stored "
                      f"(+{added} new this batch)")
            # Every question in the batch was already held. The token should
            # prevent this; if it happens repeatedly the corpus is exhausted
            # in practice and looping further only burns requests.
            if consecutive_empty >= 3:
                print("  no new questions in three batches; stopping")
                break
        elif code == 4:
            # Token exhausted: OpenTDB has served everything it has.
            print("  every available question has been fetched")
            break
        elif code == 5:
            print("  rate limited; waiting")
            time.sleep(REQUEST_SPACING * 2)
            continue
        elif code == 3:
            print("  session token expired; requesting a new one")
            token = request_token()
            continue
        else:
            print(f"  API returned response_code {code}; stopping",
                  file=sys.stderr)
            break

        time.sleep(REQUEST_SPACING)

    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('fetched_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
    conn.commit()
    return added_total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=int, default=500,
                        help="how many NEW questions to add (default 500)")
    parser.add_argument("--db", default=os.path.join("data", "trivia.db"),
                        help="database to create or top up")
    args = parser.parse_args()

    conn = connect(args.db)
    before = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    print(f"{args.db}: {before} questions held, fetching up to {args.target} more")

    try:
        harvest(conn, args.target)
    except KeyboardInterrupt:
        print("\ninterrupted; keeping what was already stored")

    after = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    categories = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    print(f"done: {after} questions across {categories} categories "
          f"(+{after - before})")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
