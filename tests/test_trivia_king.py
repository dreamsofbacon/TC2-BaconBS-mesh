"""Trivia King: the multiple-choice quiz door.

Two things this guards. The question database is data, not code, and a node
without one is an ordinary state -- opening a missing file read-only raises
sqlite3.OperationalError, which unhandled reaches whoever picked the game
from the menu as a crash rather than an explanation.

And the answer has to be matched by the option the player picked, not by the
letter's position in some other ordering. Options are shuffled per question
precisely so B is not always right, which makes an off-by-one here silently
mark correct answers wrong.
"""
import json
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import trivia_port

USER = 4242


def _build(path, rows):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE questions (
            id INTEGER PRIMARY KEY, category_id INTEGER, difficulty TEXT,
            points INTEGER, type TEXT, question TEXT, correct_answer TEXT,
            incorrect_answers TEXT, fingerprint TEXT UNIQUE);
    """)
    con.execute("INSERT INTO meta VALUES ('attribution', 'Questions from OpenTDB')")
    con.execute("INSERT INTO categories VALUES (1, 'Geography')")
    for index, (question, correct, wrong, points, kind) in enumerate(rows, start=1):
        con.execute(
            "INSERT INTO questions VALUES (?,1,'medium',?,?,?,?,?,?)",
            (index, points, kind, question, correct,
             json.dumps(wrong), question))
    con.commit()
    con.close()


class MissingDatabaseTests(unittest.TestCase):
    def tearDown(self):
        trivia_port._sessions.clear()
        trivia_port._last_scores.clear()

    def test_a_missing_database_explains_itself(self):
        with mock.patch.dict(os.environ, {"BBS_TRIVIA_DB": "no/such/file.db"}):
            self.assertEqual(trivia_port.start(USER), trivia_port.UNAVAILABLE)

    def test_a_missing_database_starts_no_session(self):
        """A session with no question would swallow the player's input and
        answer everything with 'No Trivia King game is active.'"""
        with mock.patch.dict(os.environ, {"BBS_TRIVIA_DB": "no/such/file.db"}):
            trivia_port.start(USER)
        self.assertFalse(trivia_port.active(USER))

    def test_an_unreadable_database_is_the_same_outcome(self):
        with mock.patch.dict(os.environ, {"BBS_TRIVIA_DB": __file__}):
            self.assertEqual(trivia_port.start(USER), trivia_port.UNAVAILABLE)
        self.assertFalse(trivia_port.active(USER))

    def test_the_message_names_how_to_fix_it(self):
        self.assertIn("fetch_trivia_questions.py", trivia_port.UNAVAILABLE)
        self.assertIn("BBS_TRIVIA_DB", trivia_port.UNAVAILABLE)


class _WithDatabase(unittest.TestCase):
    ROWS = [("Capital of Ecuador?", "Quito",
             ["Bogota", "Santiago", "Lima"], 200, "multiple")]

    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__), "_tmp_trivia.db")
        if os.path.exists(self.path):
            os.unlink(self.path)
        _build(self.path, self.ROWS)
        self.env = mock.patch.dict(os.environ, {"BBS_TRIVIA_DB": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        trivia_port._sessions.clear()
        trivia_port._last_scores.clear()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _letter_of(self, answer):
        options = trivia_port._sessions[USER]["options"]
        return trivia_port.CHOICE_LETTERS[options.index(answer)]


class GameplayTests(_WithDatabase):
    def test_a_question_is_presented_with_lettered_options(self):
        text = trivia_port.start(USER)
        self.assertIn("Capital of Ecuador?", text)
        for letter in "ABCD":
            self.assertIn(f"[{letter}] ", text)
        self.assertTrue(trivia_port.active(USER))

    def test_every_answer_appears_exactly_once(self):
        trivia_port.start(USER)
        options = trivia_port._sessions[USER]["options"]
        self.assertEqual(sorted(options),
                         sorted(["Quito", "Bogota", "Santiago", "Lima"]))

    def test_the_right_letter_scores(self):
        """The letter is resolved through the shuffled options, so this has
        to hold wherever the correct answer landed."""
        trivia_port.start(USER)
        reply = trivia_port.command(USER, self._letter_of("Quito"))
        self.assertIn("Correct!", reply)
        self.assertEqual(trivia_port.finish_score(USER)[0], 200)

    def test_a_wrong_letter_scores_nothing_and_reveals_the_answer(self):
        trivia_port.start(USER)
        reply = trivia_port.command(USER, self._letter_of("Bogota"))
        self.assertIn("Quito", reply)
        self.assertEqual(trivia_port.finish_score(USER)[0], 0)

    def test_the_letter_is_case_insensitive(self):
        trivia_port.start(USER)
        letter = self._letter_of("Quito")
        self.assertIn("Correct!", trivia_port.command(USER, letter.lower()))

    def test_an_unlisted_letter_reshows_the_question(self):
        """Rather than consuming the attempt, which would score a miss for a
        typo."""
        trivia_port.start(USER)
        reply = trivia_port.command(USER, "Z")
        self.assertIn("Capital of Ecuador?", reply)
        self.assertEqual(trivia_port._sessions[USER]["moves"], 0)

    def test_exiting_keeps_the_score(self):
        trivia_port.start(USER)
        trivia_port.command(USER, self._letter_of("Quito"))
        trivia_port.command(USER, "x")
        self.assertFalse(trivia_port.active(USER))
        self.assertEqual(trivia_port.finish_score(USER), (200, 1))

    def test_the_score_carries_across_questions(self):
        trivia_port.start(USER)
        trivia_port.command(USER, self._letter_of("Quito"))
        trivia_port.command(USER, "n")
        trivia_port.command(USER, self._letter_of("Quito"))
        self.assertEqual(trivia_port.finish_score(USER)[0], 400)

    def test_the_attribution_travels_with_the_database(self):
        """CC BY-SA requires it, and a README does not ship with the file."""
        self.assertIn("OpenTDB", trivia_port.attribution())


class TrueFalseTests(_WithDatabase):
    ROWS = [("The Earth is flat.", "False", ["True"], 100, "boolean")]

    def test_only_two_options_are_offered(self):
        text = trivia_port.start(USER)
        self.assertIn("[A] ", text)
        self.assertIn("[B] ", text)
        self.assertNotIn("[C] ", text)

    def test_a_true_false_question_can_be_answered(self):
        trivia_port.start(USER)
        reply = trivia_port.command(USER, self._letter_of("False"))
        self.assertIn("Correct!", reply)
        self.assertEqual(trivia_port.finish_score(USER)[0], 100)


class WiringTests(unittest.TestCase):
    """The door has to be reachable, and its input has to belong to it."""

    def test_it_is_registered_in_the_games_menu(self):
        import zork_port
        self.assertIn(trivia_port.GAME_ID, zork_port.GAMES)
        self.assertEqual(zork_port.GAMES[trivia_port.GAME_ID]["name"],
                         "Trivia King")

    def test_a_trivia_session_owns_its_input(self):
        """N means 'next question' here and 'Ask Nomad' on the main menu."""
        import pathlib
        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "message_processing.py").read_text(encoding="utf-8")
        self.assertIn("('ZORK', 'TRIVIA')", source)

    def test_the_old_name_is_gone(self):
        import pathlib
        here = pathlib.Path(__file__).resolve()
        repo = here.parent.parent
        offenders = []
        for path in list(repo.glob("*.py")) + list((repo / "tests").glob("*.py")):
            # This file names the old game to search for it, so it would
            # always match itself.
            if path.resolve() == here:
                continue
            if "jeopardy" in path.read_text(encoding="utf-8").lower():
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()


class ScoreFarmingTests(_WithDatabase):
    """A question pays out once.

    Found by an agent playing the live BBS: after answering, an unrecognised
    key reprinted the same question in full, and answering the reprint scored
    it again. Repeating that took one question from 600 to 1000 and put 2200
    on the public Hall of Fame.
    """

    def test_the_same_question_cannot_be_scored_twice(self):
        trivia_port.start(USER)
        letter = self._letter_of("Quito")
        self.assertIn("Correct!", trivia_port.command(USER, letter))
        self.assertEqual(trivia_port.finish_score(USER)[0], 200)

        trivia_port.command(USER, letter)
        self.assertEqual(trivia_port.finish_score(USER)[0], 200)

    def test_the_exploit_loop_itself_pays_nothing(self):
        """The exact sequence: answer, send a junk key to reprint it, answer
        the reprint. Ten times."""
        trivia_port.start(USER)
        letter = self._letter_of("Quito")
        trivia_port.command(USER, letter)
        for _ in range(10):
            trivia_port.command(USER, "?")
            trivia_port.command(USER, letter)
        self.assertEqual(trivia_port.finish_score(USER)[0], 200)

    def test_a_wrong_answer_cannot_be_retried_for_the_points(self):
        trivia_port.start(USER)
        trivia_port.command(USER, self._letter_of("Bogota"))
        trivia_port.command(USER, self._letter_of("Quito"))
        self.assertEqual(trivia_port.finish_score(USER)[0], 0)

    def test_the_question_count_is_not_inflated_either(self):
        trivia_port.start(USER)
        letter = self._letter_of("Quito")
        for _ in range(5):
            trivia_port.command(USER, letter)
        self.assertEqual(trivia_port.finish_score(USER)[1], 1)

    def test_answering_again_says_so_rather_than_going_quiet(self):
        trivia_port.start(USER)
        letter = self._letter_of("Quito")
        trivia_port.command(USER, letter)
        reply = trivia_port.command(USER, letter)
        self.assertIn("already answered", reply.lower())
        self.assertIn("N for another question", reply)

    def test_a_junk_key_after_answering_does_not_reprint_the_question(self):
        """Reprinting is what kept offering the second payout."""
        trivia_port.start(USER)
        trivia_port.command(USER, self._letter_of("Quito"))
        self.assertNotIn("Capital of Ecuador?", trivia_port.command(USER, "?"))

    def test_the_next_question_is_answerable_again(self):
        trivia_port.start(USER)
        trivia_port.command(USER, self._letter_of("Quito"))
        trivia_port.command(USER, "N")
        self.assertFalse(trivia_port._sessions[USER]["answered"])
        reply = trivia_port.command(USER, self._letter_of("Quito"))
        self.assertIn("Correct!", reply)
        self.assertEqual(trivia_port.finish_score(USER)[0], 400)

    def test_exiting_still_works_after_answering(self):
        trivia_port.start(USER)
        trivia_port.command(USER, self._letter_of("Quito"))
        self.assertIn("Trivia King ended", trivia_port.command(USER, "X"))
