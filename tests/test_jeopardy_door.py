"""The Jeopardy door has to survive not having its clue archive.

The archive is data, not code, and is not shipped with the BBS -- so a node
running this without one is the normal case, not an edge case. Opening a
missing file read-only raises sqlite3.OperationalError, which unhandled came
out of the games menu as a crash rather than as a message saying why the game
did not start.
"""
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

import jeopardy_port


class MissingArchiveTests(unittest.TestCase):
    def tearDown(self):
        jeopardy_port._sessions.clear()
        jeopardy_port._last_scores.clear()

    def test_a_missing_archive_explains_itself(self):
        with mock.patch.dict(os.environ, {"BBS_JEOPARDY_DB": "no/such/archive.db"}):
            self.assertEqual(jeopardy_port.start(7), jeopardy_port.UNAVAILABLE)

    def test_a_missing_archive_starts_no_session(self):
        """A session with no clue would take over the user's input and answer
        every message with 'No Jeopardy game is active.'"""
        with mock.patch.dict(os.environ, {"BBS_JEOPARDY_DB": "no/such/archive.db"}):
            jeopardy_port.start(7)
        self.assertFalse(jeopardy_port.active(7))

    def test_an_unreadable_archive_is_the_same_outcome(self):
        """Present but not a database -- a truncated or half-copied file."""
        with mock.patch.dict(os.environ, {"BBS_JEOPARDY_DB": __file__}):
            self.assertEqual(jeopardy_port.start(7), jeopardy_port.UNAVAILABLE)
        self.assertFalse(jeopardy_port.active(7))

    def test_the_message_names_the_override(self):
        """An operator reading it should know where to point the game."""
        self.assertIn("BBS_JEOPARDY_DB", jeopardy_port.UNAVAILABLE)


class PlayableArchiveTests(unittest.TestCase):
    """Guards against the hardening swallowing a working archive."""

    def setUp(self):
        self.path = os.path.join(
            os.path.dirname(__file__), "_tmp_jeopardy.db")
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("CREATE TABLE clues (id INTEGER PRIMARY KEY, category_id INTEGER, "
                    "value INTEGER, clue_text TEXT, correct_response TEXT)")
        con.execute("INSERT INTO categories VALUES (1, 'MESH RADIO')")
        con.execute("INSERT INTO clues VALUES (1, 1, 400, 'This protocol...', 'Meshtastic')")
        con.commit()
        con.close()
        self.env = mock.patch.dict(os.environ, {"BBS_JEOPARDY_DB": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        jeopardy_port._sessions.clear()
        jeopardy_port._last_scores.clear()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_a_clue_is_served(self):
        text = jeopardy_port.start(7)
        self.assertIn("MESH RADIO", text)
        self.assertTrue(jeopardy_port.active(7))

    def test_a_correct_answer_scores(self):
        jeopardy_port.start(7)
        reply = jeopardy_port.command(7, "meshtastic")
        self.assertIn("Correct!", reply)
        self.assertEqual(jeopardy_port.finish_score(7)[0], 400)

    def test_answers_ignore_case_and_punctuation(self):
        jeopardy_port.start(7)
        self.assertIn("Correct!", jeopardy_port.command(7, "  MESH-TASTIC!  "))

    def test_a_wrong_answer_reveals_the_response(self):
        jeopardy_port.start(7)
        reply = jeopardy_port.command(7, "nope")
        self.assertIn("Meshtastic", reply)
        self.assertEqual(jeopardy_port.finish_score(7)[0], 0)

    def test_exiting_ends_the_session_and_keeps_the_score(self):
        jeopardy_port.start(7)
        jeopardy_port.command(7, "meshtastic")
        jeopardy_port.command(7, "x")
        self.assertFalse(jeopardy_port.active(7))
        self.assertEqual(jeopardy_port.finish_score(7)[0], 400)


if __name__ == "__main__":
    unittest.main()
