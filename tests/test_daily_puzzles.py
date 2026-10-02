"""The daily puzzles: one answer for the whole fleet, one go each, a streak.

Two nodes never exchange a byte about today's puzzle. They agree because the
answer is computed from the date, in a way that does not depend on the
Python version or the process -- which is the property most worth a test.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import command_handlers as ch
import db_operations
import door_games
import door_kit
import numberday
import wordday

DAY1, DAY2, DAY3, DAY4 = "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"


class WordMarksTests(unittest.TestCase):
    def test_capital_small_and_dot(self):
        # C and E are in place, A is in the word but elsewhere, R and N are not.
        self.assertEqual("C.a.E", wordday.marks("cable", "crane"))

    def test_all_right_is_all_capitals(self):
        self.assertEqual("CRANE", wordday.marks("crane", "crane"))

    def test_nothing_right_is_all_dots(self):
        self.assertEqual(".....", wordday.marks("crane", "moldy"))

    def test_a_letter_is_marked_elsewhere_only_as_often_as_it_is_left(self):
        """One E in the answer: a guess with three shows the one in place
        and neither of the others. (The R is a different letter, elsewhere.)"""
        self.assertEqual("..r.E", wordday.marks("crane", "eerie"))
        # Two Es in the answer, one placed: one more may be marked.
        self.assertEqual("e...E", wordday.marks("where", "eaxxe"))

    def test_feedback_shows_the_guess_and_its_marks(self):
        self.assertEqual("EERIE  . . r . E", wordday.feedback("crane", "eerie"))


class WordRulesTests(unittest.TestCase):
    def test_every_answer_is_a_plain_five_letter_dictionary_word(self):
        allowed = wordday.allowed_words()
        self.assertGreater(len(allowed), 5000, "data/words5.txt did not load")
        self.assertEqual(len(wordday.ANSWERS), len(set(wordday.ANSWERS)))
        for word in wordday.ANSWERS:
            with self.subTest(word=word):
                self.assertEqual(5, len(word))
                self.assertTrue(word.isascii() and word.isalpha() and word.islower())
                self.assertIn(word, allowed)

    def test_a_guess_must_be_a_five_letter_word(self):
        self.assertEqual("crane", wordday.normalise(" CRANE "))
        for bad, why in (("cat", "5 letters"), ("cranes", "5 letters"),
                         ("cr4ne", "5 letters"), ("zzzzz", "not in the word list")):
            with self.assertRaises(ValueError) as refused:
                wordday.normalise(bad)
            self.assertIn(why, str(refused.exception))


class NumberRulesTests(unittest.TestCase):
    def test_bulls_and_cows(self):
        self.assertEqual((4, 0), numberday.count("1234", "1234"))
        self.assertEqual((0, 4), numberday.count("1234", "4321"))
        self.assertEqual((1, 2), numberday.count("1234", "1325"))
        self.assertEqual((0, 0), numberday.count("1234", "5678"))
        self.assertEqual("1325 1B 2C", numberday.feedback("1234", "1325"))

    def test_the_code_is_four_different_digits(self):
        for day in range(1, 29):
            code = numberday.answer(f"2026-10-{day:02d}")
            self.assertEqual(4, len(set(code)))
            self.assertTrue(code.isdigit())

    def test_a_guess_must_be_four_different_digits(self):
        self.assertEqual("0123", numberday.normalise("0 1 2 3"))
        for bad in ("123", "12345", "1123", "12a4"):
            with self.assertRaises(ValueError):
                numberday.normalise(bad)


class SameEverywhereTests(unittest.TestCase):
    def test_the_answer_changes_with_the_day(self):
        for game in (wordday, numberday):
            answers = {game.answer(f"2026-10-{day:02d}") for day in range(1, 29)}
            self.assertGreater(len(answers), 20)

    def test_another_process_computes_the_same_answers(self):
        """A fresh interpreter has a different hash salt. If the answer
        leaned on hash() or on set ordering, this is where it would show."""
        code = ("import wordday, numberday;"
                f"print(wordday.answer('{DAY1}'), numberday.answer('{DAY1}'))")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for salt in ("1", "2"):
            out = subprocess.run(
                [sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                env={**os.environ, "PYTHONHASHSEED": salt}, check=True).stdout.split()
            self.assertEqual([wordday.answer(DAY1), numberday.answer(DAY1)], out)

    def test_the_answers_are_pinned(self):
        """Changing how the answer is derived would give a node mid-rollout
        a different puzzle from its neighbours. This fails first."""
        self.assertEqual("guard", wordday.answer(DAY1))
        self.assertEqual("1075", numberday.answer(DAY1))


class DailyPlayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        path = os.path.join(self.folder.name, "daily.db")
        env = mock.patch.dict(os.environ, {"BBS_DB_PATH": path})
        env.start()
        self.addCleanup(env.stop)
        db_operations.thread_local.connection = sqlite3.connect(path)
        db_operations.initialize_database()
        self.addCleanup(self._close)
        self.day = DAY1
        clock = mock.patch.object(door_kit, 'fleet_day', lambda now=None: self.day)
        clock.start()
        self.addCleanup(clock.stop)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def word(self, text, user=5):
        return wordday.handle(user, text, "Solver")[0]

    def wrong_words(self, count):
        return [w for w in wordday.ANSWERS if w != wordday.answer(self.day)][:count]

    def test_solving_starts_a_streak_and_scores_it(self):
        self.assertIn("Word of the Day 0/6", self.word(None))
        reply = self.word(self.wrong_words(1)[0])
        self.assertIn("1/6", reply)
        reply = self.word(wordday.answer(DAY1))
        self.assertIn("Solved in 2! Streak 1.", reply)
        board = db_operations.get_game_scoreboard(wordday.GAME_ID, limit=5)
        self.assertEqual(("Solver", 10 + 7 - 2, 2), (board[0][0], board[0][1], board[0][3]))

    def test_one_go_a_day(self):
        self.word(None)
        self.word(wordday.answer(DAY1))
        reply = self.word(self.wrong_words(1)[0])
        self.assertIn("Solved in 1!", reply)
        self.assertIn("1/6", reply)

    def test_solving_on_consecutive_days_builds_the_streak(self):
        self.word(wordday.answer(DAY1))
        self.day = DAY2
        self.assertIn("0/6", self.word(None))
        self.assertIn("Streak 2.", self.word(wordday.answer(DAY2)))
        self.day = DAY3
        self.assertIn("Streak 3.", self.word(wordday.answer(DAY3)))

    def test_missing_a_day_ends_the_streak(self):
        self.word(wordday.answer(DAY1))
        self.day = DAY3
        self.assertIn("Streak 1.", self.word(wordday.answer(DAY3)))

    def test_six_wrong_guesses_reveals_the_word_and_ends_the_streak(self):
        self.word(wordday.answer(DAY1))
        self.day = DAY2
        for guess in self.wrong_words(6):
            reply = self.word(guess)
        self.assertIn(f"It was {wordday.answer(DAY2).upper()}.", reply)
        self.assertIn("6/6", reply)
        self.day = DAY3
        self.assertIn("Streak 1.", self.word(wordday.answer(DAY3)))

    def test_a_bad_or_repeated_guess_does_not_use_a_turn(self):
        self.word(None)
        self.assertIn("not in the word list", self.word("zzzzz"))
        self.assertIn("5 letters", self.word("cat"))
        guess = self.wrong_words(1)[0]
        self.word(guess)
        reply = self.word(guess)
        self.assertIn("tried that already", reply)
        self.assertIn("1/6", reply)

    def test_two_players_have_the_same_puzzle_and_their_own_go(self):
        self.word(wordday.answer(DAY1), user=5)
        reply = self.word(None, user=6)
        self.assertIn("0/6", reply)

    def test_every_screen_fits_one_packet_at_its_fullest(self):
        for game, wrong in ((wordday, self.wrong_words(6)),
                            (numberday, ["0123", "4567", "8901", "2345", "6789",
                                         "1357", "2468", "9753"])):
            wrong = [g for g in wrong if g != game.answer(self.day)]
            for guess in wrong[:game.MAX_GUESSES - 1]:
                reply = game.handle(9, guess, "Full")[0]
                self.assertTrue(door_kit.fits(reply), reply)
            for note_word in ("?", "!!"):
                for part in door_kit.messages(game.handle(9, note_word, "Full")[0]):
                    self.assertTrue(door_kit.fits(part), part)
            final = game.handle(9, wrong[game.MAX_GUESSES - 1] if len(wrong) >= game.MAX_GUESSES
                                else game.answer(self.day), "Full")[0]
            for part in door_kit.messages(final):
                self.assertTrue(door_kit.fits(part), part)

    def test_the_number_puzzle_plays_the_same_way(self):
        self.assertIn("Number of the Day 0/8", numberday.handle(5, None, "Solver")[0])
        self.assertIn("Every digit must be different",
                      numberday.handle(5, "1123", "Solver")[0])
        reply = numberday.handle(5, numberday.answer(DAY1), "Solver")[0]
        self.assertIn("Solved in 1! Streak 1.", reply)

    def test_leaving_says_see_you_tomorrow(self):
        reply, leave, _ = wordday.handle(5, "0", "Solver")
        self.assertTrue(leave)
        self.assertIn("tomorrow", reply)


class DailyMenuTests(unittest.TestCase):
    def test_both_are_doors_in_the_daily_group(self):
        for game in ch.DAILY_DOORS:
            self.assertTrue(door_games.is_door(game.COMMAND))
            self.assertEqual('daily', ch.GAMES[game.GAME_ID]['group'])
            self.assertEqual(2, len(ch.games_menu_keys(game.GAME_ID)))
            self.assertTrue(door_kit.fits(game.RULES), len(game.RULES.encode()))

    def test_the_games_menu_lists_the_daily_group(self):
        labels = [label for _key, label, _titles in ch.game_groups()]
        self.assertEqual(['Adventures', 'Classics', 'Daily', 'Trivia'], labels)


if __name__ == "__main__":
    unittest.main()
