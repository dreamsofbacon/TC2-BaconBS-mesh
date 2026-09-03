import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers
import db_operations
import utils


class PublicChatterUtilityTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        utils.user_states.clear()
        self.sent = []
        self.real_send = command_handlers.send_message
        command_handlers.send_message = lambda text, *_args: self.sent.append(text) or True
        self.interface = types.SimpleNamespace(nodes={}, bbs_nodes=[])

    def tearDown(self):
        command_handlers.send_message = self.real_send
        utils.user_states.clear()
        db_operations.thread_local.connection.close()
        del db_operations.thread_local.connection

    def add_message(self, unique_id, minutes_ago, content):
        timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        db_operations.add_public_chatter(
            unique_id, "meshtastic", 0, "LongFast", "!sender", "CALL",
            content, timestamp.isoformat().replace("+00:00", "Z"),
            timestamp.isoformat().replace("+00:00", "Z"), "!capture",
            (timestamp + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            sync_received=True,
        )

    # The window used to be typed as a number of hours and results came one
    # message at a time. Both changed: the window is picked from presets, and
    # a reply carries as many entries as the airtime budget allows. The wider
    # behaviour lives in tests/test_radio_chatter_menu.py; these keep this
    # file's own ground -- that the utility opens, bounds its window, and
    # pages oldest-ward without losing anything.

    def test_offers_preset_windows_rather_than_a_typed_number(self):
        command_handlers.handle_public_chatter_command(1234, self.interface)
        self.assertIn("1h", self.sent[-1])
        self.assertIn("7d", self.sent[-1])
        self.assertNotIn("1-168", self.sent[-1])
        self.assertEqual(utils.get_user_state(1234)["command"], "PUBLIC_CHATTER")

    def test_one_reply_carries_the_whole_window(self):
        """Both messages arrive together; before, the second needed another
        round trip."""
        self.add_message("pch:new", 1, "newest message")
        self.add_message("pch:old", 2, "older message")
        command_handlers.handle_public_chatter_command(1234, self.interface)
        command_handlers.handle_public_chatter_steps(
            1234, "4", self.interface, utils.get_user_state(1234))
        self.assertIn("newest message", self.sent[-1])
        self.assertIn("older message", self.sent[-1])

    def test_newest_comes_first(self):
        self.add_message("pch:new", 1, "newest message")
        self.add_message("pch:old", 2, "older message")
        command_handlers.handle_public_chatter_command(1234, self.interface)
        command_handlers.handle_public_chatter_steps(
            1234, "4", self.interface, utils.get_user_state(1234))
        body = self.sent[-1]
        self.assertLess(body.index("newest message"), body.index("older message"))

    def test_nothing_more_is_offered_once_the_window_is_shown(self):
        self.add_message("pch:new", 1, "newest message")
        command_handlers.handle_public_chatter_command(1234, self.interface)
        command_handlers.handle_public_chatter_steps(
            1234, "4", self.interface, utils.get_user_state(1234))
        self.assertNotIn("[M]ore", self.sent[-1])

    def test_a_week_is_still_the_longest_window(self):
        """Retention is a week, so a longer window could only ever return the
        same rows -- and there is now no way to ask for one."""
        self.assertEqual(max(h for h, _ in command_handlers.CHATTER_WINDOWS), 168)
        command_handlers.handle_public_chatter_command(1234, self.interface)
        command_handlers.handle_public_chatter_steps(
            1234, "9", self.interface, utils.get_user_state(1234))
        self.assertIn("1-6", self.sent[-1])
        self.assertEqual(utils.get_user_state(1234)["step"], 1)


if __name__ == "__main__":
    unittest.main()