"""The Public Chatter channel filter is remembered between visits.

Picking two channels out of nine is several replies over LoRa, and the
filter used to live only in the session state: leave the screen, come back,
and every choice was gone. On a radio that is not a small annoyance, it is
the reason to stop using the filter at all.

It is stored on the ACCOUNT, like relay consent and PG-13 mode, so it
follows the person to their other devices on this node. It is deliberately
not advertised to peers: the channels are what this node's own radios
overheard, so the same choice on another node names traffic it never
captured.
"""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import command_handlers as ch
import db_operations
import utils

SENDER = 4242
NODE = "!abcd1234"
CAPTURE = "5a582498f3d5f2b91a9ea3bbb21c6f1f2355bc3eca060cfac6a98a5105f69930"


class _Iface:
    bbs_nodes = []

    def __init__(self):
        self.nodes = {NODE: {'num': SENDER}}


class _Chatter(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.now = datetime.now(timezone.utc)
        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _Iface()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        ch.send_message = self._real_send
        utils.user_states.pop(SENDER, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def add(self, n, network="meshcore", index=2, name="Channel 2"):
        ts = (self.now - timedelta(minutes=n)).isoformat().replace("+00:00", "Z")
        db_operations.add_public_chatter(
            unique_id=f"u{network}{index}{n}", network=network, channel_index=index,
            channel_name=name, sender_node_id=None, sender_name="brown dog",
            content="hello", message_timestamp=ts, captured_at=ts,
            capture_node_id=CAPTURE,
            expires_at=(self.now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            hops=1)

    def say(self, text):
        self.sent.clear()
        ch.handle_public_chatter_steps(
            SENDER, text, self.iface, utils.get_user_state(SENDER))
        return self.sent[-1] if self.sent else ""

    def open_menu(self):
        self.sent.clear()
        ch.handle_public_chatter_command(SENDER, self.iface)
        return self.sent[-1]

    def state(self):
        return utils.get_user_state(SENDER) or {}

    def pick_first_channel(self):
        """Open the window, the filter screen, and toggle option 1."""
        self.open_menu()
        self.say('1')       # a time window
        self.say('f')       # the filter screen
        self.say('1')       # toggle the first channel


class ItSurvivesTheSessionTests(_Chatter):
    def setUp(self):
        super().setUp()
        self.add(1, network="meshcore", index=2, name="Channel 2")
        self.add(2, network="meshtastic", index=0, name="LongFast")

    def test_a_chosen_channel_comes_back_on_the_next_visit(self):
        self.pick_first_channel()
        chosen = list(self.state().get('channels') or [])
        self.assertTrue(chosen, "nothing was selected to begin with")

        utils.user_states.pop(SENDER, None)
        self.open_menu()
        self.assertEqual(chosen, list(self.state().get('channels') or []))

    def test_it_is_written_to_the_account(self):
        self.pick_first_channel()
        self.assertEqual(list(self.state().get('channels') or []),
                         db_operations.get_chatter_channels_for_node(NODE))

    def test_all_clears_it_for_the_next_visit_too(self):
        """[A]ll means no filter, and that has to stick as firmly as a
        choice does -- otherwise turning it off lasts one screen."""
        self.pick_first_channel()
        self.say('a')
        self.assertEqual([], db_operations.get_chatter_channels_for_node(NODE))

        utils.user_states.pop(SENDER, None)
        self.open_menu()
        self.assertEqual([], list(self.state().get('channels') or []))

    def test_a_fresh_reader_has_no_filter(self):
        self.open_menu()
        self.assertEqual([], list(self.state().get('channels') or []))


class ARememberedFilterOwnsUpTests(_Chatter):
    """A filter kept from last time can empty the screen by itself. Silence
    then reads as a quiet mesh instead of as the reader's own choice -- the
    same trap the Node View lens notice already guards against."""

    def test_the_empty_screen_names_the_filter(self):
        self.add(1, network="meshcore", index=2, name="Channel 2")
        db_operations.set_chatter_channels_for_node(NODE, ["meshtastic/7"])
        self.open_menu()
        screen = self.say('1')
        self.assertIn("filter", screen.lower())
        self.assertIn("[F]", screen)

    def test_a_full_screen_does_not_nag(self):
        self.add(1, network="meshcore", index=2, name="Channel 2")
        self.open_menu()
        screen = self.say('1')
        self.assertNotIn("Channel filter on", screen)


class TheStoreItselfTests(_Chatter):
    def test_an_unknown_device_reads_as_no_filter(self):
        self.assertEqual([], db_operations.get_chatter_channels_for_node("!nope"))

    def test_saving_creates_the_account_for_a_bare_radio(self):
        self.assertTrue(db_operations.set_chatter_channels_for_node(
            NODE, ["meshcore/2"]))
        self.assertIsNotNone(db_operations.get_account_id_for_node(NODE))
        self.assertEqual(["meshcore/2"],
                         db_operations.get_chatter_channels_for_node(NODE))

    def test_clearing_an_unknown_device_makes_no_account(self):
        """"No filter" is the default, so recording it for a passer-by would
        create an account per radio that ever opened the screen."""
        self.assertFalse(db_operations.set_chatter_channels_for_node("!nope", []))
        self.assertIsNone(db_operations.get_account_id_for_node("!nope"))

    def test_it_follows_the_account_to_another_device(self):
        db_operations.set_chatter_channels_for_node(NODE, ["meshcore/2"])
        account_id = db_operations.get_account_id_for_node(NODE)
        db_operations.link_node_to_account("!other999", account_id, "meshtastic")
        self.assertEqual(["meshcore/2"],
                         db_operations.get_chatter_channels_for_node("!other999"))

    def test_duplicates_and_blanks_are_dropped(self):
        db_operations.set_chatter_channels_for_node(
            NODE, ["meshcore/2", "", "meshcore/2", "  ", "meshtastic/0"])
        self.assertEqual(["meshcore/2", "meshtastic/0"],
                         db_operations.get_chatter_channels_for_node(NODE))

    def test_a_storage_failure_never_blocks_the_screen(self):
        """The filter is a convenience. If the write fails, the reader still
        gets their chatter."""
        self.add(1)
        original = ch.set_chatter_channels_for_node
        ch.set_chatter_channels_for_node = lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked"))
        self.addCleanup(setattr, ch, 'set_chatter_channels_for_node', original)
        self.pick_first_channel()
        self.assertTrue(self.state().get('channels'))


if __name__ == "__main__":
    unittest.main()
