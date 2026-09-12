"""Help tips: on for everyone, off for anyone who says so.

The people who need a hint under a menu are the ones who do not yet know
there is a setting to turn hints off, so the default has to be on. Everything
here is about that asymmetry: a stranger with no profile row still gets tips,
and a regular who switched them off gets a screen byte-identical to the one
that existed before tips did.

That last point is not tidiness. The main menu is already 176 bytes against a
160-byte MeshCore packet, so it arrives as two transmissions; a tip that
lingered after being switched off would cost real airtime on the smallest
transport, every time the user returned to the top.
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import command_handlers as ch
import db_operations


class _Interface:
    bbs_nodes = []
    nodes = {"!abc": {"num": 1234, "user": {"id": "!abc", "shortName": "bac"}}}


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self._real = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _Interface()
        db_operations.auto_upsert_user_profile(1234, "bac", "bacon")
        self.addCleanup(self._restore)

    def _restore(self):
        ch.send_message = self._real
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    @property
    def last(self):
        return self.sent[-1]


class DefaultTests(_Case):
    def test_a_new_user_gets_tips(self):
        self.assertTrue(db_operations.get_help_tips_enabled(1234))

    def test_someone_with_no_profile_row_at_all_still_gets_tips(self):
        """A stranger reaches a menu before anything creates their profile,
        and a stranger is exactly who the tips are for."""
        self.assertTrue(db_operations.get_help_tips_enabled(999999))

    def test_a_database_without_the_column_shows_tips_rather_than_failing(self):
        """Tips are advice. The safe failure is to show one, not to take a
        menu screen down with an exception."""
        conn = db_operations.get_db_connection()
        conn.execute("DROP TABLE user_profiles")
        conn.execute("CREATE TABLE user_profiles (user_id TEXT PRIMARY KEY)")
        conn.commit()
        self.assertTrue(db_operations.get_help_tips_enabled(1234))
        self.assertEqual(ch.help_tip(1234, 'main'), ch.HELP_TIPS['main'])


class TogglingTests(_Case):
    def test_turning_them_off_sticks(self):
        db_operations.set_help_tips_enabled(1234, False)
        self.assertFalse(db_operations.get_help_tips_enabled(1234))

    def test_turning_them_back_on_sticks(self):
        db_operations.set_help_tips_enabled(1234, False)
        db_operations.set_help_tips_enabled(1234, True)
        self.assertTrue(db_operations.get_help_tips_enabled(1234))

    def test_one_users_choice_does_not_touch_another(self):
        db_operations.auto_upsert_user_profile(5678, "two", "two")
        db_operations.set_help_tips_enabled(1234, False)
        self.assertTrue(db_operations.get_help_tips_enabled(5678))

    def test_a_user_with_no_profile_row_can_still_opt_out(self):
        db_operations.set_help_tips_enabled(4242, False)
        self.assertFalse(db_operations.get_help_tips_enabled(4242))

    def test_the_choice_does_not_bump_last_seen(self):
        """Profile sync compares last_seen. Turning tips off is not a reason
        to beat a peer holding a newer bio."""
        before = db_operations.get_user_profile(1234)[4]
        db_operations.set_help_tips_enabled(1234, False)
        self.assertEqual(db_operations.get_user_profile(1234)[4], before)


class MenuTests(_Case):
    def test_the_main_menu_carries_its_tip(self):
        ch.handle_help_command(1234, self.iface)
        self.assertIn(ch.HELP_TIPS['main'], self.last)

    def test_the_bbs_menu_carries_its_own(self):
        ch.handle_help_command(1234, self.iface, 'bbs')
        self.assertIn(ch.HELP_TIPS['bbs'], self.last)

    def test_the_bbs_tip_explains_the_thing_users_actually_ask(self):
        """Bulletins against channels is the question this whole feature
        exists to answer without anyone having to ask it."""
        tip = ch.HELP_TIPS['bbs']
        self.assertIn("Bulletin", tip)
        self.assertIn("Channel", tip)

    def test_switching_them_off_restores_the_original_screen(self):
        ch.handle_help_command(1234, self.iface)
        with_tip = self.last
        db_operations.set_help_tips_enabled(1234, False)
        self.sent.clear()
        ch.handle_help_command(1234, self.iface)
        self.assertNotIn(ch.HELP_TIPS['main'], self.last)
        self.assertLess(len(self.last.encode()), len(with_tip.encode()))

    def test_a_screen_with_no_tip_defined_gets_nothing_appended(self):
        self.assertEqual(ch.help_tip(1234, 'no-such-screen'), '')
        self.assertEqual(ch.with_help_tip("body", 1234, 'no-such-screen'), "body")

    def test_every_tip_fits_a_single_meshcore_packet_on_its_own(self):
        """160 bytes. A tip is one line of advice; if one ever needs more
        than a packet it has stopped being a tip."""
        for key, tip in ch.HELP_TIPS.items():
            with self.subTest(screen=key):
                self.assertLessEqual(len(tip.encode('utf-8')), 160)

    def test_tips_do_not_push_the_main_menu_past_two_chunks(self):
        """The menu already spans two MeshCore packets. A tip must not buy a
        third, or every return to the top costs another 2s of pacing."""
        ch.handle_help_command(1234, self.iface)
        self.assertLessEqual(len(self.last.encode('utf-8')), 320)


class SettingsScreenTests(_Case):
    def test_the_screen_reports_the_current_state(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertIn("Help tips: On", self.last)
        db_operations.set_help_tips_enabled(1234, False)
        self.sent.clear()
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertIn("Help tips: Off", self.last)

    def test_choice_five_toggles_them(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        ch.handle_settings_steps(1234, "5", self.iface, "!abc")
        self.assertFalse(db_operations.get_help_tips_enabled(1234))
        ch.handle_settings_steps(1234, "5", self.iface, "!abc")
        self.assertTrue(db_operations.get_help_tips_enabled(1234))

    def test_turning_them_off_says_how_to_get_them_back(self):
        """Otherwise the way back is the tip you just switched off."""
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.sent.clear()
        ch.handle_settings_steps(1234, "5", self.iface, "!abc")
        self.assertIn("back on", self.sent[0].lower())

    def test_the_tip_on_this_screen_points_at_the_switch(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertIn("[5]", ch.HELP_TIPS['settings'])
        self.assertIn(ch.HELP_TIPS['settings'], self.last)


if __name__ == "__main__":
    unittest.main()
