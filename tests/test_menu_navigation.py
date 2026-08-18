"""Tests for main-menu discoverability of Settings and the API Gateway.

Account linking existed but was effectively unreachable: it sat under
Profile > Linked Devices with nothing on the main menu hinting at it. The
API Gateway had a handler wired up but never rendered, because an existing
config.ini lists utilities_menu_items explicitly and predates that entry.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import command_handlers as ch


class _FakeInterface:
    nodes = {"!abc": {"num": 1234, "user": {"id": "!abc"}}}


class MainMenuContentsTests(unittest.TestCase):
    MAIN = "\U0001F4BEBacon BBS\U0001F4BE"

    def test_new_entries_appear_even_for_a_config_written_before_them(self):
        """The exact reason these were invisible: config.ini pins the item
        list, so a new entry would never show up on an upgraded node."""
        rendered = ch.build_menu(["Q", "B", "U", "P", "N", "X"], self.MAIN)
        self.assertIn("API Gateway", rendered)
        self.assertIn("Settings", rendered)

    def test_existing_entries_keep_their_numbers(self):
        """Renumbering would break muscle memory and every doc reference."""
        rendered = ch.build_menu(["Q", "B", "U", "P", "N", "X"], self.MAIN)
        for expected in ("[1] Quick Commands", "[2] BBS", "[3] Utilities",
                         "[4] Profile", "[5] Ask Nomad", "[0] Exit"):
            self.assertIn(expected, rendered)

    def test_new_entries_are_inserted_before_exit(self):
        lines = [l for l in ch.build_menu(["Q", "X"], self.MAIN).splitlines() if l.strip()]
        self.assertTrue(lines[-1].startswith("[0]"), f"Exit should stay last: {lines}")

    def test_no_duplicates_when_config_already_lists_them(self):
        rendered = ch.build_menu(["Q", "A", "S", "X"], self.MAIN)
        self.assertEqual(rendered.count("API Gateway"), 1)
        self.assertEqual(rendered.count("Settings"), 1)

    def test_api_gateway_no_longer_rendered_under_utilities(self):
        """It moved to the main menu; showing it in both would be confusing."""
        rendered = ch.build_menu(
            ["S", "F", "W", "G", "A", "X"], "\U0001F6E0\uFE0FUtilities Menu\U0001F6E0\uFE0F")
        self.assertNotIn("API Gateway", rendered)
        self.assertIn("[1] Stats", rendered)


class SettingsNavigationTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _FakeInterface()

    def tearDown(self):
        ch.send_message = self._real_send
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_settings_menu_offers_linked_devices(self):
        ch.handle_settings_command(1234, self.iface)
        self.assertIn("Linked Devices", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "SETTINGS")

    def test_choice_one_opens_account_linking(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "1", self.iface, "!abc")
        self.assertIn("Request link code", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "ACCOUNT")

    def test_zero_returns_to_the_main_menu(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "0", self.iface, "!abc")
        self.assertIn("Bacon BBS", self.sent[-1])

    def test_unrecognised_input_reshows_the_menu(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "9", self.iface, "!abc")
        self.assertIn("Settings", self.sent[-1])


class MenuHandlerWiringTests(unittest.TestCase):
    def test_main_menu_dispatches_the_new_letters(self):
        import message_processing as mp
        self.assertIn("s", mp.main_menu_handlers)
        self.assertIn("a", mp.main_menu_handlers)
        self.assertIs(mp.main_menu_handlers["s"], ch.handle_settings_command)
        self.assertIs(mp.main_menu_handlers["a"], ch.handle_apigw_command)

    def test_utilities_still_accepts_a_for_muscle_memory(self):
        """No longer listed there, but an existing habit shouldn't break."""
        import message_processing as mp
        self.assertIn("a", mp.utilities_menu_handlers)


if __name__ == "__main__":
    unittest.main()
