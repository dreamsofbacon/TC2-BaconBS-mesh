"""Node View on screen: the picker, and the promise that nothing is hidden.

The lens is only trustworthy if a narrowed screen says so. A filter someone
set and forgot, on a BBS they check once a week, otherwise reads as an empty
board or an empty mailbox -- indistinguishable from a broken node.

So every one of these asserts on the TEXT THE USER IS SENT, not on the query
underneath it. Two of this project's vacuous tests came from checking the
mechanism and never checking that anything said so.
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations
import message_processing
import utils


SENDER = 4242
SENDER_NODE = "!04058ac8"
BRIDGE = "mqtt:baconbbsvt:Burlington-NNE"
PEER = "mqtt:baconbbsvt:Chattanooga"


class _Iface:
    nodes = {SENDER_NODE: {"num": SENDER, "user": {"shortName": "me"}}}
    bbs_nodes = []
    node_id_from_num = {SENDER: SENDER_NODE}
    max_text_bytes = 160


class _Screen(unittest.TestCase):
    """A radio session with a real database and captured sends."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config_path = Path(self.temp_dir.name) / "config.ini"
        self.config_path.write_text("[boards]\nbulletin_boards = General\n",
                                    encoding="utf-8")
        self.env_patch = mock.patch.dict(
            os.environ, {"BBS_CONFIG_PATH": str(self.config_path)}, clear=False)
        self.env_patch.start()

        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self._saved = (db_operations._local_node_id,
                       db_operations.get_local_link_identities(),
                       db_operations.get_local_capture_identities())
        db_operations.set_local_node_id(SENDER_NODE)
        db_operations.set_local_link_identities([SENDER_NODE, BRIDGE])
        db_operations.set_local_capture_identities([])

        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _Iface()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        ch.send_message = self._real_send
        utils.user_states.pop(SENDER, None)
        utils.clear_view_scope(SENDER)
        db_operations.set_local_node_id(self._saved[0])
        db_operations.set_local_link_identities(self._saved[1])
        db_operations.set_local_capture_identities(self._saved[2])
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        self.env_patch.stop()
        self.temp_dir.cleanup()

    # -- fixtures ---------------------------------------------------------
    def bulletin(self, subject, source_node_id):
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO bulletins (board, sender_short_name, date, subject,"
            " content, unique_id, source_node_id)"
            " VALUES ('General','who','2026-09-05 10:00',?,'body',?,?)",
            (subject, f"u-{subject}", source_node_id))
        conn.commit()

    def mail(self, subject, source_node_id):
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO mail (sender, sender_short_name, recipient, date,"
            " subject, content, unique_id, source_node_id)"
            " VALUES ('s','who',?,'2026-09-05 10:00',?,'body',?,?)",
            (SENDER_NODE, subject, f"m-{subject}", source_node_id))
        conn.commit()

    # -- driving ----------------------------------------------------------
    def open_picker(self):
        self.sent.clear()
        ch.handle_node_view_command(SENDER, self.iface)
        return self.sent[-1]

    def pick(self, text):
        self.sent.clear()
        ch.handle_node_view_steps(
            SENDER, text, self.iface, utils.get_user_state(SENDER) or {})
        return self.sent[-1] if self.sent else ""

    def all_sent(self):
        return "\n".join(self.sent)

    def read_board(self):
        self.sent.clear()
        ch.handle_bb_steps(SENDER, 'r', 2, {'command': 'BULLETIN_ACTION',
                                            'step': 2, 'board': 'General',
                                            'boards': ['General']},
                           self.iface, [])
        return self.all_sent()

    def read_mail(self):
        self.sent.clear()
        ch.handle_mail_steps(SENDER, 'r', 1, {'command': 'MAIL', 'step': 1},
                             self.iface, [])
        return self.all_sent()


class PickerTests(_Screen):
    def test_it_explains_itself(self):
        """The whole complaint: someone who wandered in is told nothing.
        One sentence has to carry that posts are shared and this is about
        reading, not about what the node keeps."""
        screen = self.open_picker()
        self.assertIn("Posts sync everywhere", screen)
        self.assertIn("Pick whose you read", screen)

    def test_all_and_this_node_are_always_offered(self):
        """A lens that cannot return you to everything, or to your own
        posts, is a trap rather than a filter."""
        screen = self.open_picker()
        self.assertIn("All nodes", screen)
        self.assertIn("This node", screen)

    def test_all_nodes_is_marked_by_default(self):
        self.assertIn("[1]*All nodes", self.open_picker())

    def test_all_nodes_is_always_number_one(self):
        """Every hidden-count notice says !V=all, so the way back has to be
        in the same place on every page."""
        for _ in range(12):
            self.bulletin(f"b{_}", f"mqtt:baconbbsvt:peer{_}")
        screen = self.open_picker()
        self.assertIn("[1]", screen.split("\n")[2])
        self.assertIn("All nodes", screen)

    def test_only_nodes_with_content_are_offered(self):
        self.bulletin("theirs", PEER)
        screen = self.open_picker()
        self.assertIn("Chattanooga", screen)
        self.assertNotIn("Nowhere", screen)

    def test_an_mqtt_peer_is_named_without_any_config(self):
        self.bulletin("theirs", PEER)
        self.assertIn("Chattanooga", self.open_picker())
        self.assertNotIn(PEER, self.open_picker())

    def test_picking_a_node_moves_the_star(self):
        self.bulletin("theirs", PEER)
        self.open_picker()
        screen = self.pick("3")
        self.assertIn("*Chattanooga", screen)
        self.assertNotIn("[1]*All nodes", screen)

    def test_picking_sets_the_session_scope(self):
        self.bulletin("theirs", PEER)
        self.open_picker()
        self.pick("3")
        self.assertEqual(utils.get_view_scope(SENDER), (PEER,))

    def test_picking_all_nodes_widens_again(self):
        self.bulletin("theirs", PEER)
        self.open_picker()
        self.pick("3")
        self.pick("1")
        self.assertIsNone(utils.get_view_scope(SENDER))

    def test_this_node_carries_every_local_identity(self):
        """A node answers to a radio id AND its mqtt bridge id, and on the
        live fleet nearly all content is stamped with the bridge one."""
        self.open_picker()
        self.pick("2")
        self.assertEqual(set(utils.get_view_scope(SENDER)), {SENDER_NODE, BRIDGE})

    def test_a_grouped_nickname_carries_every_id(self):
        """The only thing joining a peer's BBS id to the public key its
        radios stamp on chatter. Keeping one would filter half a node."""
        self.config_path.write_text(
            f"[node_names]\nChattanooga = {PEER}, capturekey123\n", encoding="utf-8")
        self.bulletin("theirs", PEER)
        self.open_picker()
        self.pick("3")
        self.assertEqual(set(utils.get_view_scope(SENDER)),
                         {PEER, "capturekey123"})

    def test_a_bad_choice_says_so_and_stays_put(self):
        screen = self.pick("99") if self.open_picker() else ""
        self.assertIn("Invalid choice.", screen)
        self.assertIn("All nodes", screen)

    def test_zero_leaves_for_the_main_menu(self):
        self.open_picker()
        self.pick("0")
        self.assertEqual(
            (utils.get_user_state(SENDER) or {}).get('command'), 'MAIN_MENU')

    def test_a_long_fleet_pages_rather_than_flooding_the_radio(self):
        """MeshCore gives 160 bytes and two seconds a chunk. A fixed count
        would turn a twelve-node fleet into a multi-chunk transmission."""
        for i in range(12):
            self.bulletin(f"b{i}", f"mqtt:baconbbsvt:LongNodeName{i}")
        screen = self.open_picker()
        self.assertIn("[N]ext", screen)
        self.assertLessEqual(len(screen.encode("utf-8")), 160)

    def test_next_shows_the_rest(self):
        for i in range(12):
            self.bulletin(f"b{i}", f"mqtt:baconbbsvt:LongNodeName{i}")
        first = self.open_picker()
        second = self.pick("n")
        self.assertNotEqual(first, second)
        self.assertIn("[P]rev", second)


class BulletinIndicatorTests(_Screen):
    def setUp(self):
        super().setUp()
        self.bulletin("mine", SENDER_NODE)
        self.bulletin("theirs", PEER)

    def test_the_default_view_says_nothing_about_scope(self):
        """All nodes is what every existing user has. Their screens must be
        byte-identical to a build without this feature."""
        screen = self.read_board()
        self.assertNotIn("Node view", screen)
        self.assertIn("mine", screen)
        self.assertIn("theirs", screen)

    def test_a_narrowed_board_names_the_node_and_the_count(self):
        utils.set_view_scope(SENDER, [PEER])
        screen = self.read_board()
        self.assertIn("theirs", screen)
        self.assertNotIn("mine", screen)
        self.assertIn("Node view: Chattanooga.", screen)
        self.assertIn("1 more from other nodes.", screen)

    def test_an_empty_board_still_accounts_for_what_it_hid(self):
        """Otherwise the BBS flatly says the board is empty when it is not."""
        utils.set_view_scope(SENDER, ["mqtt:baconbbsvt:Nobody"])
        screen = self.read_board()
        self.assertIn("No bulletins in General.", screen)
        self.assertIn("2 more from other nodes.", screen)

    def test_the_board_menu_counts_only_what_is_shown(self):
        utils.set_view_scope(SENDER, [PEER])
        self.sent.clear()
        ch.send_board_action_menu(SENDER, self.iface, 'General', ['General'])
        self.assertIn("General has 1 messages.", self.all_sent())
        self.assertIn("Node view: Chattanooga.", self.all_sent())


class MailIndicatorTests(_Screen):
    """Mail is where hiding is genuinely risky, so it gets the most care."""

    def setUp(self):
        super().setUp()
        self.mail("here", SENDER_NODE)
        self.mail("fromthem", PEER)

    def test_the_default_mailbox_says_nothing_about_scope(self):
        screen = self.read_mail()
        self.assertNotIn("Node view", screen)
        self.assertIn("You have 2 mail messages.", screen)

    def test_a_narrowed_mailbox_says_how_much_it_is_holding_back(self):
        utils.set_view_scope(SENDER, [PEER])
        screen = self.read_mail()
        self.assertIn("You have 1 mail messages.", screen)
        self.assertIn("1 more mail from other nodes.", screen)

    def test_an_empty_mailbox_never_just_says_empty(self):
        """The mandatory mitigation. Someone may be waiting on a message,
        and 'There are no messages' would be a flat untruth."""
        utils.set_view_scope(SENDER, ["mqtt:baconbbsvt:Nobody"])
        screen = self.read_mail()
        self.assertIn("There are no messages in your mailbox.", screen)
        self.assertIn("2 more mail from other nodes.", screen)

    def test_the_way_out_is_always_named(self):
        """A bare V at a mail prompt is read as a message number, so only
        !V escapes. The wording is the mitigation, not decoration."""
        utils.set_view_scope(SENDER, [PEER])
        self.assertIn("!V=all", self.read_mail())

    def test_the_way_out_actually_works_from_the_mailbox(self):
        """The notice is a promise, and it was false here. Mail is
        dispatched ahead of the global-prefix branch, so !V typed on a mail
        screen was swallowed as mail input and nothing happened -- on the
        one screen where being able to widen matters most."""
        utils.set_view_scope(SENDER, [PEER])
        utils.update_user_state(SENDER, {'command': 'MAIL', 'step': 1})
        self.sent.clear()
        message_processing.process_message(
            sender_id=SENDER, message="!v", interface=self.iface,
            sender_node_id=SENDER_NODE)
        self.assertIn("Node View", self.all_sent())
        self.assertIn("Posts sync everywhere", self.all_sent())

    def test_a_message_body_starting_with_a_bang_is_still_a_body(self):
        """The reason mail is dispatched early in the first place. Escaping
        must not reach into the steps where the BBS asked for content."""
        for step in (3, 5, 7):
            with self.subTest(step=step):
                utils.update_user_state(
                    SENDER, {'command': 'MAIL', 'step': step,
                             'recipient_id': PEER, 'recipient_name': 'x',
                             'subject': 's', 'content': ''})
                self.sent.clear()
                message_processing.process_message(
                    sender_id=SENDER, message="!vote for me", interface=self.iface,
                    sender_node_id=SENDER_NODE)
                self.assertNotIn("Posts sync everywhere", self.all_sent())

    def test_the_main_menu_badge_still_counts_the_whole_mailbox(self):
        """Deliberately unscoped. It is the top-level guarantee that mail is
        never hidden: badge 2 against a list of 1 is what makes the notice
        on that list checkable."""
        utils.set_view_scope(SENDER, [PEER])
        self.sent.clear()
        ch.handle_help_command(SENDER, self.iface)
        self.assertIn(":2)", self.all_sent())

    def test_a_selected_message_stays_readable_while_narrowed(self):
        """Narrowing between the list and the read must not answer 'Mail not
        found' about a message the user was just looking at."""
        row_id = db_operations.get_db_connection().execute(
            "SELECT id FROM mail WHERE subject='here'").fetchone()[0]
        utils.set_view_scope(SENDER, [PEER])
        self.sent.clear()
        ch.handle_mail_steps(SENDER, str(row_id), 2,
                             {'command': 'MAIL', 'step': 2}, self.iface, [])
        self.assertIn("here", self.all_sent())
        self.assertNotIn("not found", self.all_sent().lower())


class ChannelCommentIndicatorTests(_Screen):
    def setUp(self):
        super().setUp()
        conn = db_operations.get_db_connection()
        conn.execute("INSERT INTO channels (name, url) VALUES ('Chan','psk')")
        self.channel_id = conn.execute(
            "SELECT id FROM channels WHERE name='Chan'").fetchone()[0]
        for uid, sender, source in (("c1", "local-voice", SENDER_NODE),
                                    ("c2", "far-voice", PEER)):
            conn.execute(
                "INSERT INTO channel_comments (channel_id, sender_short_name,"
                " date, content, unique_id, source_node_id)"
                " VALUES (?,?,'2026-09-05 10:00','text',?,?)",
                (self.channel_id, sender, uid, source))
        conn.commit()

    def view_comments(self):
        self.sent.clear()
        ch.handle_channel_directory_steps(
            SENDER, 'v', 6,
            {'command': 'CHANNEL_DIRECTORY', 'step': 6,
             'channel_id': self.channel_id},
            self.iface)
        return self.all_sent()

    def test_the_default_view_shows_every_comment_and_says_nothing(self):
        screen = self.view_comments()
        self.assertIn("local-voice", screen)
        self.assertIn("far-voice", screen)
        self.assertNotIn("Node view", screen)

    def test_a_narrowed_view_hides_and_accounts_for_the_rest(self):
        utils.set_view_scope(SENDER, [PEER])
        screen = self.view_comments()
        self.assertIn("far-voice", screen)
        self.assertNotIn("local-voice", screen)
        self.assertIn("Node view: Chattanooga.", screen)
        self.assertIn("1 more from other nodes.", screen)

    def test_the_post_list_preview_is_left_unscoped(self):
        """Deliberate. That line is a liveliness preview, not a read, and
        scoping it would label a busy post 'No comments yet' -- false, and
        about a lens that hides nothing from the network anyway."""
        utils.set_view_scope(SENDER, ["mqtt:baconbbsvt:Nobody"])
        rows = db_operations.get_channel_comments(self.channel_id)
        self.assertEqual(len(rows), 2)


class WiringTests(_Screen):
    def test_the_main_menu_offers_it(self):
        """MENU_REQUIRED, so a config.ini written before this feature still
        shows it -- the same reason Web Fetch and Linked Devices are there."""
        rendered = ch.build_menu(["Q", "B", "U", "X"], "BBS")
        self.assertIn("Node View", rendered)

    def test_the_bang_prefix_reaches_it_from_anywhere(self):
        self.assertIs(message_processing.main_menu_handlers["v"],
                      ch.handle_node_view_command)

    def test_quick_commands_list_it(self):
        self.sent.clear()
        ch.handle_quick_help_command(SENDER, self.iface)
        self.assertIn("!V", self.all_sent())

    def test_leaving_the_bbs_forgets_the_lens(self):
        """Per-session. The next person on this radio starts on all nodes."""
        utils.set_view_scope(SENDER, [PEER])
        ch.handle_exit_command(SENDER, self.iface)
        self.assertIsNone(utils.get_view_scope(SENDER))

    def test_moving_around_the_menus_does_not(self):
        """The reason the lens is not stored in user_states, which is
        replaced wholesale on every menu move."""
        utils.set_view_scope(SENDER, [PEER])
        ch.handle_help_command(SENDER, self.iface)
        ch.handle_help_command(SENDER, self.iface, 'bbs')
        self.assertEqual(utils.get_view_scope(SENDER), (PEER,))


class WebAdminFilterTests(unittest.TestCase):
    """The admin counterpart, and the SQL trap it sits on."""

    def setUp(self):
        import configparser
        from web_admin import create_app
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp_dir.name)
        self.config_path = root / "config.ini"
        self.db_path = root / "bulletins.db"
        config = configparser.ConfigParser()
        config["admin"] = {"username": "admin", "password": "oldpass"}
        config["boards"] = {"bulletin_boards": "General"}
        with open(self.config_path, "w", encoding="utf-8") as handle:
            config.write(handle)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_CONFIG_PATH": str(self.config_path),
             "BBS_DB_PATH": str(self.db_path),
             "BBS_WEBGUI_SECRET": "test-secret"},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self._saved = db_operations.get_local_link_identities()
        db_operations.set_local_link_identities([SENDER_NODE])

        conn = db_operations.get_db_connection()
        for subject, source in (("apples", SENDER_NODE), ("apples", PEER),
                                ("pears", PEER)):
            conn.execute(
                "INSERT INTO bulletins (board, sender_short_name, date, subject,"
                " content, unique_id, source_node_id)"
                " VALUES ('General','who','2026-09-05 10:00',?,'body',?,?)",
                (subject, f"u-{subject}-{source}", source))
        conn.commit()

        self.create_app = create_app
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        db_operations.set_local_link_identities(self._saved)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin
        db_operations.remove_connection_log_handler()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _client(self):
        client = self.create_app().test_client()
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        client.post("/login", data={"username": "admin", "password": "oldpass",
                                    "csrf_token": token})
        return client

    def test_the_unfiltered_page_shows_everything(self):
        page = self._client().get("/bulletins").get_data(as_text=True)
        self.assertIn("pears", page)
        self.assertIn("apples", page)

    def test_filtering_by_node_narrows_the_list(self):
        page = self._client().get(f"/bulletins?node={PEER}").get_data(as_text=True)
        self.assertIn("pears", page)

    def test_search_and_node_filter_intersect(self):
        """The trap: the search half is a chain of ORs, so ANDing the node
        clause on unbracketed binds it to the last arm only -- and searching
        while filtering quietly returns most of the table."""
        page = self._client().get(
            f"/bulletins?q=pears&node={SENDER_NODE}").get_data(as_text=True)
        # "pears" itself is echoed in the search box, so the assertion is on
        # the result set: the only pears bulletin came from the peer, so
        # intersecting with this node must leave nothing.
        self.assertIn("No bulletins found", page)

    def test_the_node_column_shows_a_resolved_name(self):
        """Two rows come from that peer, so the readable name has to appear
        more often than the raw id -- which legitimately appears exactly
        once, as the filter dropdown's option value."""
        page = self._client().get("/bulletins").get_data(as_text=True)
        # Once as the dropdown label plus once per row from that peer. A
        # column still showing the raw id would leave only the label.
        self.assertGreaterEqual(page.count("Chattanooga"), 3)

    def test_the_filter_offers_this_node(self):
        page = self._client().get("/bulletins").get_data(as_text=True)
        self.assertIn("This node", page)


if __name__ == "__main__":
    unittest.main()
