"""The parts every menu-driven door game shares.

Packets are split and never cut; the fleet's day and its daily seed are the
same on every node; and a turn is saved whole or not at all.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import db_operations
import door_kit


class PacketTests(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(["a\nb"], door_kit.messages("a\nb"))

    def test_long_text_is_split_between_lines_not_cut(self):
        lines = [f"line {i} " + "x" * 30 for i in range(20)]
        parts = door_kit.messages("\n".join(lines))
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-8")), door_kit.MAX_SCREEN_BYTES)
        self.assertEqual(lines, "\n".join(parts).split("\n"))

    def test_one_long_line_is_split_between_words(self):
        line = " ".join(["word"] * 120)
        parts = door_kit.messages(line)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-8")), door_kit.MAX_SCREEN_BYTES)
        self.assertEqual(line.split(), " ".join(parts).split())

    def test_the_budget_is_bytes_not_characters(self):
        """An emoji is four bytes; a line of fifty-one is over a packet."""
        line = "💾" * 51
        self.assertTrue(all(len(p.encode("utf-8")) <= door_kit.MAX_SCREEN_BYTES
                            for p in door_kit.messages(line)))
        self.assertEqual(line, "".join(door_kit.messages(line)))

    def test_the_separator_makes_separate_messages(self):
        reply = door_kit.MESSAGE_SEPARATOR.join(["note", "screen\nline"])
        self.assertEqual(["note", "screen\nline"], door_kit.messages(reply))


class FleetDayTests(unittest.TestCase):
    def test_the_day_is_utc_whatever_the_node_clock_says(self):
        late = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
        self.assertEqual("2026-10-01", door_kit.fleet_day(late))

    def test_every_node_derives_the_same_seed(self):
        """Not Python's hash(), which is salted per process: two nodes
        would each get their own puzzle."""
        self.assertEqual(door_kit.daily_seed("word", "2026-10-01"),
                         door_kit.daily_seed("word", "2026-10-01"))
        self.assertEqual(13972464266786207605,
                         door_kit.daily_seed("word", "2026-10-01"))

    def test_the_seed_changes_with_the_day_and_the_game(self):
        base = door_kit.daily_seed("word", "2026-10-01")
        self.assertNotEqual(base, door_kit.daily_seed("word", "2026-10-02"))
        self.assertNotEqual(base, door_kit.daily_seed("number", "2026-10-01"))


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = mock.patch.dict(os.environ, {
            "BBS_DB_PATH": os.path.join(self.folder.name, "kit.db")})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(
            os.path.join(self.folder.name, "kit.db"))
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _bump(self, state):
        state["n"] += 1
        return state, state["n"]

    def test_a_new_player_starts_fresh_and_a_returning_one_resumes(self):
        new = lambda: {"n": 0}
        self.assertEqual(1, door_kit.run_turn("g", 7, new, self._bump))
        self.assertEqual(2, door_kit.run_turn("g", 7, new, self._bump))
        self.assertEqual(1, door_kit.run_turn("g", 8, new, self._bump))

    def test_games_do_not_share_a_save(self):
        new = lambda: {"n": 0}
        door_kit.run_turn("one", 7, new, self._bump)
        self.assertEqual(1, door_kit.run_turn("two", 7, new, self._bump))

    def test_a_turn_that_fails_saves_nothing(self):
        new = lambda: {"n": 0}
        door_kit.run_turn("g", 7, new, self._bump)

        def explode(state):
            state["n"] = 99
            raise RuntimeError("mid-turn")

        with self.assertRaises(RuntimeError):
            door_kit.run_turn("g", 7, new, explode)
        self.assertEqual(2, door_kit.run_turn("g", 7, new, self._bump))

    def test_an_unreadable_save_is_kept_not_replaced(self):
        new = lambda: {"n": 0}
        door_kit.run_turn("g", 7, new, self._bump)
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE zork_saves SET save_data = ?", (b"{broken",))
        conn.commit()
        with self.assertRaises(door_kit.SaveUnavailable):
            door_kit.run_turn("g", 7, new, self._bump)
        self.assertEqual(b"{broken", bytes(conn.execute(
            "SELECT save_data FROM zork_saves").fetchone()[0]))

    def test_a_save_the_game_refuses_is_kept(self):
        new = lambda: {"n": 0}
        door_kit.run_turn("g", 7, new, self._bump)

        def refuse(state):
            raise ValueError("future version")

        with self.assertRaises(door_kit.SaveUnavailable):
            door_kit.run_turn("g", 7, new, self._bump, load=refuse)
        self.assertEqual(2, door_kit.run_turn("g", 7, new, self._bump))


class CompressionTests(unittest.TestCase):
    """Saves cross the air whenever they change, so they are stored in
    whichever form is smaller -- and every older form still reads."""

    BIG = {"market": {f"good{i}": {"price": 100 + i, "stock": 20 + i} for i in range(16)},
           "inventory": {f"good{i}": 0 for i in range(16)}, "cash": 2400}

    def test_a_repetitive_save_is_stored_much_smaller(self):
        import json
        plain = json.dumps(self.BIG, separators=(",", ":")).encode()
        packed = door_kit.encode_save(self.BIG)
        self.assertLess(len(packed), len(plain) * 0.6)
        self.assertEqual(self.BIG, door_kit.decode_save(packed))

    def test_a_tiny_save_is_left_plain(self):
        packed = door_kit.encode_save({"n": 1})
        self.assertEqual(b'{"n":1}', packed)

    def test_never_larger_than_plain(self):
        import json
        for state in ({}, {"a": "x"}, {"guesses": ["crane"]}, self.BIG):
            plain = json.dumps(state, separators=(",", ":")).encode()
            self.assertLessEqual(len(door_kit.encode_save(state)), len(plain))

    def test_every_older_form_still_reads(self):
        self.assertEqual({"n": 1}, door_kit.decode_save(b'{"n":1}'))          # plain bytes
        self.assertEqual({"n": 1}, door_kit.decode_save('{"n":1}'))           # the old text tables
        self.assertEqual({"n": 1}, door_kit.decode_save(memoryview(b'{"n":1}')))

    def test_a_damaged_save_is_a_value_error_not_a_crash(self):
        packed = door_kit.encode_save(self.BIG)
        for bad in (packed[:20], b"not-zlib", b"{broken", b""):
            with self.assertRaises(ValueError):
                door_kit.decode_save(bad)

    def test_text_outside_ascii_survives(self):
        state = {"name": "🥓 Crispy", "note": "naïve café" * 20}
        self.assertEqual(state, door_kit.decode_save(door_kit.encode_save(state)))


if __name__ == "__main__":
    unittest.main()
