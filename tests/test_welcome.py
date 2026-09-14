"""The welcome screen: what a stranger sees when they first reach this BBS.

Two sections, because a fleet is two things at once. [bbs] name and
[bbs] welcome describe the BBS -- one identity every node shares. [bbs]
node_welcome is this node's own line and never leaves it.

The hard part is the trigger: the whole welcome runs to several packets, so
it is sent unprompted once per person, on first contact, and after that only
on request (!WELCOME, !HELLO). It used to be trimmed to fit one packet
instead, which on the live fleet meant radio users saw only the BBS name.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers
import db_operations
import utils


class _Radio:
    def __init__(self, max_bytes=220):
        self.sent = []
        self.max_bytes = max_bytes

    def capture(self, text, sender_id, interface):
        self.sent.append(text)
        return True

    @property
    def text(self):
        return "\n".join(self.sent)


def _settings(**values):
    """Patch the [bbs] section without touching a real config file."""
    return mock.patch.object(
        utils, "_config_raw",
        lambda section, option: values.get(option) if section == "bbs" else None)


class WelcomeTextTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(utils, "affiliated_node_labels", lambda: [])
        p.start()
        self.addCleanup(p.stop)

    def test_the_name_is_always_present(self):
        with _settings(name="Bacon BBS"):
            self.assertTrue(utils.welcome_text().startswith("Bacon BBS"))

    def test_a_blank_name_falls_back(self):
        with _settings(name=""):
            self.assertTrue(utils.welcome_text().startswith("Bacon BBS"))

    def test_every_section_is_sent_however_long(self):
        """The live failure: an 899-byte greeting never fit one packet, so it
        was dropped -- and the short node line and node list after it went
        with it. A radio user saw the BBS name and nothing else."""
        with mock.patch.object(utils, "affiliated_node_labels",
                               lambda: ["burlington (here)", "chattanooga"]):
            with _settings(name="Bacon BBS", welcome="A" * 899,
                           node_welcome="You have reached Burlington."):
                text = utils.welcome_text()
        self.assertIn("A" * 899, text)
        self.assertIn("You have reached Burlington.", text)
        self.assertIn("Nodes: burlington (here), chattanooga", text)

    def test_sections_come_in_order_and_the_menu_hint_is_last(self):
        with _settings(name="Bacon BBS", welcome="Fleet line.",
                       node_welcome="Node line."):
            lines = utils.welcome_text(first_contact=True).split("\n")
        self.assertEqual(lines, ["Bacon BBS", "Fleet line.", "Node line.",
                                 utils.WELCOME_MENU_HINT])

    def test_on_request_there_is_no_menu_hint(self):
        """Someone who typed !WELCOME already knows there is a menu."""
        with _settings(name="Bacon BBS", welcome="Fleet line.",
                       node_welcome="Node line."):
            lines = utils.welcome_text().split("\n")
        self.assertEqual(lines, ["Bacon BBS", "Fleet line.", "Node line."])

    def test_the_menu_hint_says_how_to_get_the_menu(self):
        self.assertIn("?", utils.WELCOME_MENU_HINT)
        self.assertIn("menu", utils.WELCOME_MENU_HINT.lower())

    def test_carriage_returns_from_the_web_form_are_removed(self):
        """The browser submits CRLF; every \\r is a wasted byte on air."""
        with _settings(name="Bacon BBS", welcome="One.\r\n\r\nTwo.\rThree."):
            text = utils.welcome_text()
        self.assertNotIn("\r", text)
        self.assertIn("One.\n\nTwo.\nThree.", text)

    def test_the_node_list_can_be_turned_off(self):
        with mock.patch.object(utils, "affiliated_node_labels",
                               lambda: ["Burlington", "forgecam"]):
            with _settings(name="Bacon BBS", show_nodes="false"):
                self.assertNotIn("forgecam", utils.welcome_text())
            with _settings(name="Bacon BBS", show_nodes="true"):
                self.assertIn("forgecam", utils.welcome_text())


class WelcomeMessagesTests(unittest.TestCase):
    """What each transport receives, and what the web admin previews."""

    def setUp(self):
        p = mock.patch.object(utils, "affiliated_node_labels", lambda: [])
        p.start()
        self.addCleanup(p.stop)

    def test_a_long_welcome_is_split_to_fit_each_transport(self):
        with _settings(name="Bacon BBS",
                       welcome=" ".join(["Sentence number %d." % i for i in range(60)])):
            for budget in (160, 220):
                with self.subTest(budget=budget):
                    messages = utils.welcome_messages(budget)
                    self.assertGreater(len(messages), 1)
                    for message in messages:
                        self.assertLessEqual(len(message.encode("utf-8")), budget)
                    self.assertIn(utils.WELCOME_MENU_HINT, messages[-1])

    def test_nothing_is_lost_in_the_split(self):
        words = ["word%d" % i for i in range(150)]
        with _settings(name="Bacon BBS", welcome=" ".join(words)):
            joined = " ".join(utils.welcome_messages(160))
        for word in words:
            self.assertIn(word, joined)

    def test_ssh_gets_it_in_one(self):
        with _settings(name="Bacon BBS", welcome="A" * 899):
            self.assertEqual(len(utils.welcome_messages(8192)), 1)

    def test_it_is_split_exactly_as_send_message_splits_it(self):
        with _settings(name="Bacon BBS", welcome="Long greeting. " * 40):
            self.assertEqual(
                utils.welcome_messages(220),
                utils._split_into_chunks(utils.welcome_text(first_contact=True),
                                         max_len=220))


class NodeListTests(unittest.TestCase):
    """One node is several ids. Listing both makes the BBS look bigger."""

    def _labels(self, peers, local_ids=(), nicknames=None):
        with mock.patch.object(db_operations, "get_peer_sync_states",
                               lambda: [(p,) for p in peers]), \
             mock.patch.object(utils, "local_identities_for_display",
                               lambda: set(local_ids)), \
             mock.patch.object(utils, "get_node_nicknames",
                               lambda: dict(nicknames or {})):
            return utils.affiliated_node_labels()

    def test_two_ids_for_one_node_are_listed_once(self):
        labels = self._labels(
            ["mqtt:baconbbs:node2", "mqtt:baconbbsvt:BaconBBS-VT2"],
            nicknames={"mqtt:baconbbs:node2": "forgecam",
                       "mqtt:baconbbsvt:BaconBBS-VT2": "forgecam"})
        self.assertEqual(labels, ["forgecam"])

    def test_ungrouped_ids_are_still_both_listed(self):
        """Without a [node_names] line there is nothing to group them BY, and
        inventing a grouping would be a guess. The web admin says so."""
        labels = self._labels(
            ["mqtt:baconbbs:node2", "mqtt:baconbbsvt:BaconBBS-VT2"])
        self.assertEqual(labels, ["node2", "BaconBBS-VT2"])

    def test_this_node_comes_first_and_is_marked(self):
        labels = self._labels(
            ["mqtt:baconbbsvt:Chattanooga"],
            local_ids=["mqtt:baconbbsvt:Burlington-NNE"])
        self.assertEqual(labels[0], "Burlington-NNE (here)")
        self.assertIn("Chattanooga", labels)

    def test_our_own_id_appears_once_as_here_and_not_again_as_a_peer(self):
        """peer_sync_state carries our own identities too -- a node records
        state for every id it hears about, including its own."""
        labels = self._labels(
            ["mqtt:baconbbs:bbs-main", "mqtt:baconbbsvt:Chattanooga"],
            local_ids=["mqtt:baconbbs:bbs-main",
                       "mqtt:baconbbsvt:Burlington-NNE"])
        # The WHOLE list, not just a search for our own name. Skipping the
        # guard does not duplicate "bbs-main" -- node_display_name resolves
        # a local id to "this node", so the list reads
        # "bbs-main (here), this node, Chattanooga" and a substring check
        # sails straight past it.
        self.assertEqual(labels, ["bbs-main (here)", "Chattanooga"])

    def test_a_peer_that_never_arrived_is_not_announced(self):
        """Sourced from peer_sync_state, not from configured peers."""
        self.assertEqual(self._labels([]), [])


class FirstContactTests(unittest.TestCase):
    """Greeting the same person twice is the failure to avoid."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_DB_PATH": str(Path(self.temp_dir.name) / "bulletins.db")},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def test_the_first_message_is_first_contact(self):
        self.assertTrue(
            db_operations.auto_upsert_user_profile(1234, "bac", "bacon"))

    def test_every_later_message_is_not(self):
        db_operations.auto_upsert_user_profile(1234, "bac", "bacon")
        for _ in range(5):
            self.assertFalse(
                db_operations.auto_upsert_user_profile(1234, "bac", "bacon"))

    def test_a_different_user_is_greeted_too(self):
        db_operations.auto_upsert_user_profile(1234, "bac", "bacon")
        self.assertTrue(
            db_operations.auto_upsert_user_profile(5678, "egg", "eggs"))

    def test_a_synced_profile_means_they_already_met_the_bbs(self):
        """Profiles sync fleet-wide. Someone who met forgecam yesterday is a
        stranger to this node's memory but not to the BBS, and being greeted
        again by 'the same' BBS would read as a bug."""
        db_operations.upsert_synced_user_profile(
            "1234", "bac", "bacon", "2026-09-01 10:00:00",
            "2026-09-01 10:00:00", 3, "")
        self.assertFalse(
            db_operations.auto_upsert_user_profile(1234, "bac", "bacon"))

    def test_the_message_count_still_increments(self):
        for _ in range(3):
            db_operations.auto_upsert_user_profile(1234, "bac", "bacon")
        row = db_operations.get_db_connection().execute(
            "SELECT messages_sent FROM user_profiles WHERE user_id = '1234'"
        ).fetchone()
        self.assertEqual(row[0], 3)

    def test_it_does_not_use_returning(self):
        """forgecam is on SQLite 3.34.1, where RETURNING raises -- and this
        path runs on every message the node receives."""
        import inspect
        source = inspect.getsource(db_operations.auto_upsert_user_profile)
        # The docstring explains why it is avoided, so check the code only.
        body = source.split('"""')[-1]
        self.assertNotIn("RETURNING", body.upper())


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.radio = _Radio()
        p = mock.patch.object(command_handlers, "send_message",
                              self.radio.capture)
        p.start()
        self.addCleanup(p.stop)

    def test_first_contact_adds_the_menu_hint(self):
        seen = []
        with mock.patch.object(command_handlers, "welcome_text",
                               lambda first_contact=False: seen.append(first_contact) or "W"):
            command_handlers.handle_welcome_command("!user", object(), first_contact=True)
        self.assertEqual(seen, [True])
        self.assertEqual(self.radio.sent, ["W"])

    def test_on_request_it_does_not(self):
        seen = []
        with mock.patch.object(command_handlers, "welcome_text",
                               lambda first_contact=False: seen.append(first_contact) or "W"):
            command_handlers.handle_welcome_command("!user", object())
        self.assertEqual(seen, [False])

    def test_the_quick_help_screen_advertises_it(self):
        with mock.patch.object(command_handlers, "_role_commands_available",
                               lambda s, i: False):
            command_handlers.handle_quick_help_command("!user", object())
        self.assertIn("!WELCOME", self.radio.text)


class WiringTests(unittest.TestCase):
    def test_bang_welcome_and_hello_are_dispatched(self):
        import inspect
        import message_processing
        source = inspect.getsource(message_processing.process_message)
        self.assertIn('global_lower in ("welcome", "hello")', source)
        self.assertIs(message_processing.handle_welcome_command,
                      command_handlers.handle_welcome_command)

    def test_first_contact_is_wired_to_the_profile_upsert(self):
        """The greeting hangs off _auto_update_profile's return value, which
        is the only place that knows."""
        import inspect
        import message_processing
        source = inspect.getsource(message_processing.process_message)
        self.assertIn(
            "if _auto_update_profile(sender_id, interface):\n"
            "            handle_welcome_command(sender_id, interface, first_contact=True)",
            source)

    def test_a_profile_failure_never_greets(self):
        """_auto_update_profile swallows errors; it must return False when it
        does, or a broken profile write greets a regular on every message."""
        import message_processing
        broken = types.SimpleNamespace(nodes={})
        self.assertFalse(
            message_processing._auto_update_profile(1234, broken))


class SentOnceEndToEndTests(unittest.TestCase):
    """Through process_message, the way a radio message arrives."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        env = mock.patch.dict(
            os.environ,
            {"BBS_DB_PATH": str(Path(self.temp_dir.name) / "bulletins.db")},
            clear=False)
        env.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(env.stop)
        self.addCleanup(self._close)
        import bbs_emulator
        self.session = bbs_emulator.start_session(short_name="once")
        self.addCleanup(lambda: bbs_emulator.end_session(self.session.token))
        labels = mock.patch.object(utils, "affiliated_node_labels", lambda: [])
        labels.start()
        self.addCleanup(labels.stop)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def _texts(self, message):
        chunks, error = self.session.send(message)
        self.assertIsNone(error)
        return [c["text"] if isinstance(c, dict) else str(c) for c in chunks]

    def test_the_whole_welcome_arrives_unprompted_on_the_first_message_only(self):
        greeting = " ".join(["Welcome sentence %d." % i for i in range(30)])
        with _settings(name="Bacon BBS", welcome=greeting,
                       node_welcome="You have reached Burlington."):
            first = "\n".join(self._texts("hi"))
            again = "\n".join(self._texts("hi"))
        self.assertIn("Welcome sentence 29.", first)
        self.assertIn("You have reached Burlington.", first)
        self.assertIn(utils.WELCOME_MENU_HINT, first)
        self.assertNotIn("Welcome sentence", again)

    def test_bang_welcome_and_hello_send_it_again_in_full(self):
        greeting = " ".join(["Welcome sentence %d." % i for i in range(30)])
        with _settings(name="Bacon BBS", welcome=greeting,
                       node_welcome="You have reached Burlington."):
            self._texts("hi")
            for command in ("!welcome", "!HELLO"):
                with self.subTest(command=command):
                    reply = "\n".join(self._texts(command))
                    self.assertIn("Welcome sentence 0.", reply)
                    self.assertIn("Welcome sentence 29.", reply)
                    self.assertIn("You have reached Burlington.", reply)
                    self.assertNotIn(utils.WELCOME_MENU_HINT, reply)


if __name__ == "__main__":
    unittest.main()
