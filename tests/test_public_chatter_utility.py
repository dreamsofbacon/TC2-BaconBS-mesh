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

    def test_prompts_for_user_defined_window(self):
        command_handlers.handle_public_chatter_command(1234, self.interface)
        self.assertIn("1-168", self.sent[-1])
        self.assertEqual(utils.get_user_state(1234)["command"], "PUBLIC_CHATTER")

    def test_hours_show_newest_then_next_older(self):
        self.add_message("pch:new", 1, "newest message")
        self.add_message("pch:old", 2, "older message")
        command_handlers.handle_public_chatter_command(1234, self.interface)
        state = utils.get_user_state(1234)
        command_handlers.handle_public_chatter_steps(1234, "24", self.interface, state)
        self.assertIn("newest message", self.sent[-1])
        self.assertIn("[N]ext older", self.sent[-1])

        state = utils.get_user_state(1234)
        command_handlers.handle_public_chatter_steps(1234, "n", self.interface, state)
        self.assertIn("older message", self.sent[-1])
        self.assertNotIn("newest message", self.sent[-1])

        state = utils.get_user_state(1234)
        command_handlers.handle_public_chatter_steps(1234, "n", self.interface, state)
        self.assertNotIn("next older", self.sent[-1].lower())

    def test_rejects_window_over_one_week(self):
        command_handlers.handle_public_chatter_command(1234, self.interface)
        command_handlers.handle_public_chatter_steps(
            1234, "169", self.interface, utils.get_user_state(1234)
        )
        self.assertIn("1 to 168", self.sent[-1])


if __name__ == "__main__":
    unittest.main()