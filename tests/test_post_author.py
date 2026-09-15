"""Bulletins and channel comments record the device that wrote them.

Reported 2026-09-14: a post keeps whatever name the radio had when it was
written, so a person who later linked their radio to an account still shows
up under a device short name. New posts now store the author's device, and
the name shown follows that device's account. Posts made before this keep
their stored name.

The author travels between nodes in a POSTAUTHOR frame of its own, only to
peers advertising 'auth', and is never part of the record hash.
"""
import sqlite3
import sys
import types
import unittest
import uuid
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers
import db_operations
import message_processing
import utils

AUTHOR = "!0a1b2c3d"


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations._clear_peer_caps_cache()
        self.addCleanup(self._close)

    def _close(self):
        db_operations._clear_peer_caps_cache()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def link(self, node_id, alias):
        account_id = db_operations.create_account()
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE accounts SET alias = ?, alias_normalized = ? WHERE account_id = ?",
                     (alias, db_operations.normalize_alias(alias), account_id))
        conn.commit()
        db_operations.link_node_to_account(node_id, account_id, "meshtastic")

    def bulletin(self, author=AUTHOR, unique_id=None):
        return db_operations.add_bulletin("General", "2c3d", "Subj", "Body", [], None,
                                          unique_id=unique_id, author_node_id=author)

    def channel(self):
        return db_operations.add_channel("baconnet", "https://example.invalid/#x")

    def comment(self, author=AUTHOR, unique_id=None):
        channel_id = db_operations.get_channel_id_by_name_url("baconnet", "https://example.invalid/#x")
        if channel_id is None:
            channel_id = self.channel()
        return db_operations.add_channel_comment(channel_id, "2c3d", "Nice", unique_id=unique_id,
                                                 author_node_id=author), channel_id

    def stored_author(self, table, unique_id):
        return db_operations.get_db_connection().execute(
            f"SELECT author_node_id FROM {table} WHERE unique_id = ?", (unique_id,)).fetchone()[0]

    def sync(self, message):
        message_processing.process_message(
            sender_id=1, message=message,
            interface=types.SimpleNamespace(sent_texts=[], bbs_nodes=[]),
            is_sync_message=True, sender_node_id="!peer1")


class DisplayTests(_Case):
    def test_a_bulletin_shows_the_authors_account(self):
        self.bulletin()
        self.link(AUTHOR, "Materva")
        (row,) = db_operations.get_bulletins("General")
        self.assertEqual(row[2], "Materva")
        self.assertEqual(db_operations.get_bulletin_content(row[0])[0], "Materva")

    def test_a_comment_shows_the_authors_account(self):
        _, channel_id = self.comment()
        self.link(AUTHOR, "Materva")
        self.assertEqual(db_operations.get_channel_comments(channel_id)[0][1], "Materva")

    def test_posts_without_an_author_keep_the_stored_name(self):
        self.link(AUTHOR, "Materva")
        self.bulletin(author=None)
        _, channel_id = self.comment(author=None)
        self.assertEqual(db_operations.get_bulletins("General")[0][2], "2c3d")
        self.assertEqual(db_operations.get_channel_comments(channel_id)[0][1], "2c3d")

    def test_an_unlinked_author_keeps_the_stored_name(self):
        self.bulletin()
        self.link("!ffffffff", "Someone")
        self.assertEqual(db_operations.get_bulletins("General")[0][2], "2c3d")

    def test_the_stored_name_is_not_rewritten(self):
        unique_id = self.bulletin()
        self.link(AUTHOR, "Materva")
        db_operations.get_bulletins("General")
        self.assertEqual(db_operations.get_bulletin_by_unique_id(unique_id)[1], "2c3d")

    def test_an_invalid_author_is_not_stored(self):
        for bad in ("", "a|b", "has space", "x" * 129):
            with self.subTest(author=bad):
                unique_id = self.bulletin(author=bad)
                self.assertIsNone(self.stored_author("bulletins", unique_id))


class PostingTests(_Case):
    def setUp(self):
        super().setUp()
        self.iface = types.SimpleNamespace(
            nodes={AUTHOR: {"num": 7, "user": {"shortName": "2c3d"}}}, bbs_nodes=[])
        patcher = mock.patch.object(command_handlers, "send_message")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(command_handlers.update_user_state, 7, None)

    def test_a_radio_bulletin_records_its_device(self):
        command_handlers.handle_post_bulletin_command(7, "!PB,,General,,Hi,,Body", self.iface, [])
        unique_id = db_operations.get_bulletins("General")[0][4]
        self.assertEqual(self.stored_author("bulletins", unique_id), AUTHOR)

    def test_the_bulletin_editor_records_its_device(self):
        state = {"command": "BULLETIN_POST_CONTENT", "step": 5, "board": "General",
                 "subject": "Hi", "content": "Body\n"}
        editor = command_handlers.handle_bb_steps
        # Posting returns to the board menu through handle_bb_steps; only the
        # post itself is under test.
        with mock.patch.object(command_handlers, "handle_bb_steps"):
            editor(7, "END", 5, state, self.iface, [])
        unique_id = db_operations.get_bulletins("General")[0][4]
        self.assertEqual(self.stored_author("bulletins", unique_id), AUTHOR)

    def test_a_comment_records_its_device(self):
        channel_id = self.channel()
        state = {"command": "CHANNEL_DIRECTORY", "step": 7, "channel_id": channel_id,
                 "channel_name": "baconnet", "comment_content": "Nice"}
        command_handlers.handle_channel_directory_steps(7, "END", 7, state, self.iface)
        rows = db_operations.get_db_connection().execute(
            "SELECT author_node_id FROM channel_comments").fetchall()
        self.assertEqual(rows, [(AUTHOR,)])


class SyncFrameTests(_Case):
    def test_postauthor_sets_the_author(self):
        unique_id = self.bulletin(author=None)
        self.sync(f"POSTAUTHOR|B|{unique_id}|{AUTHOR}")
        self.assertEqual(self.stored_author("bulletins", unique_id), AUTHOR)

    def test_postauthor_for_a_comment_with_a_compact_uid(self):
        unique_id, _ = self.comment(author=None)
        compact = utils.encode_uid(unique_id, use_cuid=True)
        self.sync(f"POSTAUTHOR|C|{compact}|{AUTHOR}")
        self.assertEqual(self.stored_author("channel_comments", unique_id), AUTHOR)

    def test_the_first_author_stays(self):
        """A later frame cannot move a post onto someone else's account."""
        unique_id = self.bulletin()
        self.sync(f"POSTAUTHOR|B|{unique_id}|!deadbeef")
        self.assertEqual(self.stored_author("bulletins", unique_id), AUTHOR)
        db_operations.add_bulletin("General", "2c3d", "Subj", "Body", [], None,
                                   unique_id=unique_id, author_node_id="!deadbeef")
        self.assertEqual(self.stored_author("bulletins", unique_id), AUTHOR)

    def test_a_replayed_record_fills_a_missing_author(self):
        unique_id = self.bulletin(author=None)
        self.bulletin(unique_id=unique_id)
        self.assertEqual(self.stored_author("bulletins", unique_id), AUTHOR)

    def test_malformed_frames_change_nothing(self):
        unique_id = self.bulletin(author=None)
        for frame in (f"POSTAUTHOR|X|{unique_id}|{AUTHOR}", f"POSTAUTHOR|B||{AUTHOR}",
                      f"POSTAUTHOR|B|{unique_id}", f"POSTAUTHOR|B|{unique_id}|a b"):
            with self.subTest(frame=frame):
                self.sync(frame)
                self.assertIsNone(self.stored_author("bulletins", unique_id))

    def test_the_author_is_not_hashed(self):
        unique_id = self.bulletin(author=None)
        _, _ = self.comment(author=None)
        before = (db_operations.get_record_hash_manifest("bulletins"),
                  db_operations.get_record_hash_manifest("channel_comments"))
        db_operations.set_post_author("B", unique_id, AUTHOR)
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE channel_comments SET author_node_id = ?", (AUTHOR,))
        conn.commit()
        after = (db_operations.get_record_hash_manifest("bulletins"),
                 db_operations.get_record_hash_manifest("channel_comments"))
        self.assertEqual(before, after)


class SendTests(_Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        patcher = mock.patch.object(utils, "_send_one_sync",
                                    side_effect=lambda msg, dest, *a, **k: self.sent.append((dest, msg)))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("get_sync_pause_seconds",):
            p = mock.patch.object(utils, name, return_value=0)
            p.start()
            self.addCleanup(p.stop)
        self.iface = types.SimpleNamespace()
        caps = {"!new": frozenset({"auth"}), "!old": frozenset()}
        p = mock.patch.object(db_operations, "get_peer_caps",
                              side_effect=lambda peer: (2, caps.get(peer, frozenset())))
        p.start()
        self.addCleanup(p.stop)

    def author_frames(self):
        return [(dest, msg) for dest, msg in self.sent if msg.startswith("POSTAUTHOR|")]

    def test_only_capable_peers_get_the_author(self):
        unique_id = str(uuid.uuid4())
        utils.send_post_author_to_bbs_nodes("B", unique_id, AUTHOR, ["!new", "!old"], self.iface)
        self.assertEqual(self.author_frames(), [("!new", f"POSTAUTHOR|B|{unique_id}|{AUTHOR}")])

    def test_no_author_no_frame(self):
        utils.send_post_author_to_bbs_nodes("B", str(uuid.uuid4()), None, ["!new"], self.iface)
        self.assertEqual(self.author_frames(), [])

    def test_a_bulletin_sends_its_author_after_the_record(self):
        with mock.patch.object(utils, "get_max_text_bytes", return_value=200):
            utils.send_bulletin_to_bbs_nodes("General", "2c3d", "Subj", "Body", "u-1",
                                             ["!new"], self.iface, author_node_id=AUTHOR)
        self.assertTrue(self.sent[0][1].startswith("BULLETIN|"))
        self.assertEqual(self.sent[-1], ("!new", f"POSTAUTHOR|B|u-1|{AUTHOR}"))

    def test_a_comment_sends_its_author(self):
        with mock.patch.object(utils, "get_max_text_bytes", return_value=200):
            utils.send_channel_comment_to_bbs_nodes(
                db_operations.make_channel_manifest_key("baconnet", "https://example.invalid/#x"),
                "2c3d", "2026-09-14 10:00", "Nice", "u-2", ["!new"], self.iface,
                author_node_id=AUTHOR)
        self.assertEqual(self.sent[-1], ("!new", f"POSTAUTHOR|C|u-2|{AUTHOR}"))

    def test_a_requested_resend_carries_the_author(self):
        unique_id = self.bulletin()
        with mock.patch.object(utils, "get_max_text_bytes", return_value=200), \
             mock.patch.object(message_processing, "send_bulletin_to_bbs_nodes",
                               wraps=utils.send_bulletin_to_bbs_nodes) as send:
            message_processing._send_requested_record("bulletins", unique_id, "!new", self.iface)
        self.assertEqual(send.call_args.kwargs["author_node_id"], AUTHOR)

    def test_the_capability_is_advertised(self):
        self.assertIn("auth", utils.WIRE_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
