"""Editing a post that has already been sent to other nodes.

The rule that shapes all of this: a post is NOT edited in place. Sync
reconciles records by unique_id, and add_bulletin merges a record that
arrives under an id it already holds -- so re-sending an edit leaves the
tail of the old text behind, and an edit down to a prefix of the original is
discarded as a duplicate. A peer would then push its copy back and the edit
would quietly disappear on the next repair pass, which is what the web
admin's old in-place UPDATE did.

So an edit retracts the post (a tombstone every node honours) and publishes
the new text under a new id, carrying the original date, author and
audience. Editing must never widen where a post may travel.
"""
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations

LOCAL = "!self0000"
OTHER = "!peer9999"
AUTHOR = "!author111"
PEER_A = "!aaaa1111"


class _Edit(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        patcher = mock.patch.object(db_operations, 'get_local_node_id',
                                    return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def post(self, board="General", subject="hi", content="first text",
             author=AUTHOR):
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'):
            return db_operations.add_bulletin(
                board, "caller", subject, content, [PEER_A], None,
                author_node_id=author)

    def row(self, unique_id):
        return db_operations.get_db_connection().execute(
            "SELECT board, sender_short_name, date, subject, content,"
            " author_node_id, local_only, sync_peers FROM bulletins"
            " WHERE unique_id = ?", (unique_id,)).fetchone()


class TheEditReplacesRatherThanMergesTests(_Edit):
    def test_a_shorter_edit_does_not_keep_the_old_tail(self):
        """The merge trap: re-sending 'short' over 'a much longer text' used
        to store 'shortch longer text'."""
        unique_id = self.post(content="a much longer original text")
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes'):
            new_id, error = db_operations.edit_bulletin(
                unique_id, "General", "hi", "short", [PEER_A], None,
                editor_node_id=AUTHOR)
        self.assertEqual('', error)
        self.assertEqual("short", self.row(new_id)[4])

    def test_the_old_record_is_retracted_for_every_node(self):
        unique_id = self.post()
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes') as retract:
            db_operations.edit_bulletin(unique_id, "General", "hi", "new text",
                                        [PEER_A], None, editor_node_id=AUTHOR)
        retract.assert_called_once()
        self.assertEqual(unique_id, retract.call_args[0][0])
        self.assertIsNone(self.row(unique_id))

    def test_the_date_author_and_name_are_carried_across(self):
        unique_id = self.post()
        before = self.row(unique_id)
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes'):
            new_id, _ = db_operations.edit_bulletin(
                unique_id, "General", "hi", "new text", [PEER_A], None,
                editor_node_id=AUTHOR)
        after = self.row(new_id)
        self.assertEqual(before[2], after[2], "the date moved")
        self.assertEqual(before[1], after[1], "the author's name changed")
        self.assertEqual(AUTHOR, after[5])

    def test_an_unchanged_save_is_left_alone(self):
        unique_id = self.post()
        with mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes') as retract:
            same, error = db_operations.edit_bulletin(
                unique_id, "General", "hi", "first text", [PEER_A], None,
                editor_node_id=AUTHOR)
        retract.assert_not_called()
        self.assertEqual((unique_id, ''), (same, error))


class EditingNeverWidensTheAudienceTests(_Edit):
    def test_a_restricted_post_stays_restricted(self):
        db_operations.set_board_audience("Ops", "peers", [PEER_A])
        unique_id = self.post(board="Ops")
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes'):
            new_id, _ = db_operations.edit_bulletin(
                unique_id, "Ops", "hi", "new text", [PEER_A], None,
                editor_node_id=AUTHOR)
        self.assertEqual(PEER_A, self.row(new_id)[7])

    def test_a_local_only_post_stays_local_even_if_its_board_widened(self):
        db_operations.set_board_audience("Ops", "local", [])
        unique_id = self.post(board="Ops")
        db_operations.set_board_audience("Ops", "all", [])
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes'):
            new_id, _ = db_operations.edit_bulletin(
                unique_id, "Ops", "hi", "new text", [PEER_A], None,
                editor_node_id=AUTHOR)
        self.assertEqual(1, self.row(new_id)[6])


class WhoMayEditTests(_Edit):
    def test_the_author_may(self):
        unique_id = self.post()
        self.assertEqual((True, ''),
                         db_operations.bulletin_edit_permission(unique_id, AUTHOR))

    def test_someone_else_may_not(self):
        unique_id = self.post()
        allowed, reason = db_operations.bulletin_edit_permission(unique_id, OTHER)
        self.assertFalse(allowed)
        self.assertIn("only edit posts you wrote", reason)

    def test_the_operator_may(self):
        unique_id = self.post()
        self.assertEqual((True, ''), db_operations.bulletin_edit_permission(
            unique_id, '', is_operator=True))

    def test_another_device_on_the_same_account_may(self):
        """The post belongs to the person, not the radio they used."""
        unique_id = self.post()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account(AUTHOR, account_id, 'meshtastic')
        db_operations.link_node_to_account("!second22", account_id, 'meshtastic')
        self.assertEqual((True, ''), db_operations.bulletin_edit_permission(
            unique_id, "!second22"))

    def test_a_post_from_another_node_cannot_be_edited_here(self):
        """Not policy -- arithmetic. The other node still holds the original
        and would push it back, so the two would argue forever."""
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'):
            unique_id = db_operations.add_bulletin(
                "General", "someone", "theirs", "their text", [], None,
                unique_id="from-elsewhere", source_node_id=OTHER,
                source_timestamp="2026-09-01T00:00:00Z", author_node_id=AUTHOR)
        allowed, reason = db_operations.bulletin_edit_permission(unique_id, AUTHOR)
        self.assertFalse(allowed)
        self.assertIn("another node", reason)

    def test_a_missing_post_says_so(self):
        allowed, reason = db_operations.bulletin_edit_permission("no-such-id", AUTHOR)
        self.assertFalse(allowed)
        self.assertIn("no longer exists", reason)

    def test_edit_refuses_when_permission_refuses(self):
        unique_id = self.post()
        with mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes') as retract:
            new_id, error = db_operations.edit_bulletin(
                unique_id, "General", "hi", "new text", [], None,
                editor_node_id=OTHER)
        retract.assert_not_called()
        self.assertIsNone(new_id)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
