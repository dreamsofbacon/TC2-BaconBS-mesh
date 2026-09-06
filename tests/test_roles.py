"""Roles: who someone is, and the one role that actually stops them.

Most of these are labels today. Banned is not -- it is refused before
anything dispatches, so it has to hold from the string node id, survive a
peer trying to lift it, and not accidentally catch anyone else.

The rule underneath the whole thing: an unrecognised role resolves to the
LEAST privilege. A typo in config, or a role a newer peer invented, must
never land somewhere powerful just because nothing here recognises it.
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

NODE = "!04058ac8"
OTHER = "!0408b778"
SECOND_DEVICE = "meshcore-abc123"


class _RoleCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config_path = Path(self.temp_dir.name) / "config.ini"
        self.config_path.write_text("[boards]\nbulletin_boards = General\n",
                                    encoding="utf-8")
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_CONFIG_PATH": str(self.config_path)}, clear=False)
        self.env_patch.start()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _config(self, body):
        self.config_path.write_text(body, encoding="utf-8")

    def _account_for(self, *node_ids):
        account_id = db_operations.create_account()
        for node_id in node_ids:
            db_operations.link_node_to_account(node_id, account_id, "meshtastic")
        return account_id


class RankTests(unittest.TestCase):
    def test_the_ladder_is_ordered(self):
        ladder = ['banned', 'unregistered', 'user', 'bot', 'vip',
                  'mod', 'admin', 'developer']
        ranks = [db_operations.role_rank(r) for r in ladder]
        self.assertEqual(ranks, sorted(ranks))

    def test_bot_sits_with_user_rather_than_above_it(self):
        """A label, not a promotion."""
        self.assertTrue(db_operations.role_at_least('bot', 'user'))
        self.assertFalse(db_operations.role_at_least('bot', 'vip'))

    def test_an_unknown_role_is_the_least_privileged(self):
        """The safety property. A role this build does not know -- a typo, or
        one a newer peer invented -- must not satisfy any threshold."""
        self.assertEqual(db_operations.normalize_role('wizard'), 'unregistered')
        self.assertFalse(db_operations.role_at_least('wizard', 'user'))
        self.assertFalse(db_operations.role_at_least('', 'user'))
        self.assertFalse(db_operations.role_at_least(None, 'user'))

    def test_banned_never_satisfies_a_threshold(self):
        for minimum in ('unregistered', 'user', 'mod', 'admin'):
            with self.subTest(minimum=minimum):
                self.assertFalse(db_operations.role_at_least('banned', minimum))

    def test_unregistered_is_not_assignable(self):
        """It means "has no account", which assigning would contradict."""
        self.assertNotIn('unregistered', db_operations.ASSIGNABLE_ROLES)
        self.assertIn('banned', db_operations.ASSIGNABLE_ROLES)


class ResolutionTests(_RoleCase):
    def test_a_node_with_nothing_is_unregistered(self):
        self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_a_node_role_applies_without_an_account(self):
        db_operations.set_node_role(NODE, 'vip')
        self.assertEqual(db_operations.get_node_role(NODE), 'vip')

    def test_an_account_role_covers_every_linked_device(self):
        """One answer per person. A mod with three radios is a mod on all
        three, and demoting them does not leave a grant on a device they
        forgot they linked."""
        self._account_for(NODE, SECOND_DEVICE)
        db_operations.set_node_role(NODE, 'mod')
        self.assertEqual(db_operations.get_node_role(SECOND_DEVICE), 'mod')

    def test_the_account_wins_over_a_stale_node_row(self):
        db_operations.set_node_role(NODE, 'vip')
        self._account_for(NODE)
        db_operations.set_node_role(NODE, 'user')
        self.assertEqual(db_operations.get_node_role(NODE), 'user')

    def test_one_persons_role_does_not_leak_to_another(self):
        db_operations.set_node_role(NODE, 'admin')
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')

    def test_a_blank_node_id_is_unregistered(self):
        for value in ('', '   ', None):
            with self.subTest(value=value):
                self.assertEqual(db_operations.get_node_role(value), 'unregistered')


class RemoteAssertionTests(_RoleCase):
    """Roles sync fleet-wide, so a peer can assert one for any node id."""

    def test_a_peer_can_ban(self):
        self.assertTrue(
            db_operations.apply_synced_node_role(NODE, 'banned', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'banned')

    def test_a_peer_cannot_grant_above_the_ceiling(self):
        """The whole reason the ceiling exists: the frame is unsigned like
        every other sync frame, so anything on the broker can send one, and
        nobody outside this node should be able to hand themselves admin."""
        for role in ('admin', 'developer'):
            with self.subTest(role=role):
                self.assertFalse(db_operations.apply_synced_node_role(
                    NODE, role, '2026-09-06T10:00:00'))
                self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_the_ceiling_still_allows_what_it_is_for(self):
        self.assertTrue(db_operations.apply_synced_node_role(
            NODE, 'mod', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'mod')

    def test_the_ceiling_is_configurable(self):
        self._config("[roles]\nremote_role_ceiling = user\n")
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'mod', '2026-09-06T10:00:00'))
        self.assertTrue(db_operations.apply_synced_node_role(
            NODE, 'user', '2026-09-06T10:00:00'))

    def test_a_local_operator_is_not_subject_to_the_ceiling(self):
        """The ceiling constrains peers, not the console behind a password."""
        self.assertTrue(db_operations.set_node_role(NODE, 'developer'))
        self.assertEqual(db_operations.get_node_role(NODE), 'developer')

    def test_a_stale_assertion_cannot_undo_a_newer_decision(self):
        db_operations.set_node_role(NODE, 'banned', '2026-09-06T12:00:00')
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'user', '2026-09-06T09:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'banned')

    def test_a_newer_assertion_does_apply(self):
        db_operations.set_node_role(NODE, 'banned', '2026-09-06T09:00:00')
        self.assertTrue(db_operations.apply_synced_node_role(
            NODE, 'user', '2026-09-06T12:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'user')

    def test_role_sync_can_be_turned_off_entirely(self):
        self._config("[roles]\nsync_roles = false\n")
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'banned', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_an_unknown_role_from_a_peer_is_refused(self):
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'superuser', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_an_assertion_with_no_timestamp_is_refused(self):
        """Without one there is no way to order it against a local decision."""
        self.assertFalse(db_operations.apply_synced_node_role(NODE, 'banned', ''))


class _Iface:
    bbs_nodes = []
    nodes = {NODE: {"num": 4242, "user": {"id": NODE}}}
    node_id_from_num = {4242: NODE}


class BanEnforcementTests(_RoleCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        # Both modules, because the refusal is sent from message_processing
        # and everything it lets through replies from command_handlers --
        # patching one would make "was anything sent?" mean two things.
        self._real = (message_processing.send_message, ch.send_message)
        capture = lambda text, sid, iface: self.sent.append(text)
        message_processing.send_message = capture
        ch.send_message = capture
        message_processing._banned_notified.clear()
        self.iface = _Iface()
        self.addCleanup(self._restore)

    def _restore(self):
        message_processing.send_message, ch.send_message = self._real
        message_processing._banned_notified.clear()
        utils.user_states.pop(4242, None)

    def _say(self, text="hello"):
        self.sent.clear()
        message_processing.process_message(
            sender_id=4242, message=text, interface=self.iface,
            sender_node_id=NODE)
        return self.sent

    def test_an_ordinary_user_is_not_stopped(self):
        sent = self._say()
        self.assertTrue(sent)
        self.assertNotIn(message_processing.BANNED_NOTICE, sent)

    def test_a_banned_sender_is_told_once(self):
        db_operations.set_node_role(NODE, 'banned')
        self.assertEqual(self._say(), [message_processing.BANNED_NOTICE])

    def test_and_then_ignored(self):
        """Answering every message would let someone banned for flooding keep
        making the node transmit on demand -- the behaviour they were banned
        for."""
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        for _ in range(3):
            self.assertEqual(self._say(), [])

    def test_nothing_they_send_reaches_the_bbs(self):
        """Not just no menu: no side effects either."""
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        self._say("!pb,,General,,subject,,body")
        rows = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM bulletins").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_lifting_the_ban_lets_them_back_in(self):
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        db_operations.set_node_role(NODE, 'user')
        sent = self._say()
        self.assertTrue(sent)
        self.assertNotIn(message_processing.BANNED_NOTICE, sent)

    def test_a_re_ban_explains_itself_again(self):
        """Otherwise someone banned, unbanned, then banned again gets
        silence and no idea why."""
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        db_operations.set_node_role(NODE, 'user')
        self._say()
        db_operations.set_node_role(NODE, 'banned')
        self.assertEqual(self._say(), [message_processing.BANNED_NOTICE])

    def test_sync_traffic_is_not_subject_to_the_ban(self):
        """A ban is about a person using the BBS. Dropping a peer's sync
        frames because some node id is banned would corrupt replication."""
        db_operations.set_node_role(NODE, 'banned')
        self.sent.clear()
        message_processing.process_message(
            sender_id=4242, message="SYNCSTATE|0|0|0|0|0|0|0",
            interface=self.iface, is_sync_message=True, sender_node_id=NODE)
        self.assertNotIn(message_processing.BANNED_NOTICE, self.sent)


if __name__ == "__main__":
    unittest.main()
