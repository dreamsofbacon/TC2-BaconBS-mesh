"""Deleting a score or a profile has to survive the next sync.

A trivia score of 2200, farmed through a scoring exploit, was deleted from
this node. The row went. Zero rows remained, and it was reported fixed.
Hours later it was back, pushed from a peer running older code that had
never heard about the delete.

Nothing was wrong with the delete. game_scores and user_profiles simply had
no delete operation at all -- the row was removed with raw SQL, no tombstone
was written, and the ingest path had no reason to refuse the record when the
peer offered it again. Every scope that syncs needs a memory of its deletes,
or deleting from one node is just a pause.

The rule these tests pin down: a tombstone beats an equal-or-older record,
and loses to a strictly newer one -- because a score achieved after the
delete is a new score that happens to share a key, not the one removed.
"""

import base64
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import message_processing


def _b64(text):
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


EXPLOIT_SCORE = 2200
USER = "3758096387"
GAME = "trivia"
EARLIER = "2026-09-03 10:00:00"
DELETED_AT = "2026-09-03 12:00:00"
LATER = "2026-09-03 14:00:00"
# The spelling the resurrected row actually wore when it came back.
DELETED_AT_T_FORM = "2026-09-03T12:00:00"


class _DbCase(unittest.TestCase):
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

    def _scores(self):
        return db_operations.get_db_connection().execute(
            "SELECT user_id, game_id, score, achieved_at FROM game_scores").fetchall()

    def _profiles(self):
        return db_operations.get_db_connection().execute(
            "SELECT user_id, short_name, last_seen FROM user_profiles").fetchall()

    def _seed_score(self, achieved_at=EARLIER, score=EXPLOIT_SCORE):
        db_operations.upsert_synced_game_score(
            USER, GAME, "baconbot", score, 3000, 12, achieved_at)

    def _seed_profile(self, last_seen=EARLIER):
        db_operations.upsert_synced_user_profile(
            USER, "baconbot", "Bacon Bot", EARLIER, last_seen, 5, "a bio")


class GameScoreDeleteTests(_DbCase):
    def test_the_delete_removes_the_row(self):
        self._seed_score()
        self.assertEqual(len(self._scores()), 1)
        self.assertTrue(db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT))
        self.assertEqual(self._scores(), [])

    def test_the_delete_is_remembered(self):
        self._seed_score()
        db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT)
        self.assertTrue(db_operations.has_sync_tombstone('game_scores', f"{USER}:{GAME}"))

    def test_a_peer_cannot_push_the_deleted_score_back(self):
        """The 2200, exactly as it happened: deleted here, still held there,
        offered again on the next sync."""
        self._seed_score()
        db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT)
        self._seed_score()
        self.assertEqual(self._scores(), [], "the deleted score came back")

    def test_the_other_spelling_of_the_same_instant_is_refused_too(self):
        """The row that returned carried a 'T' timestamp. Comparing raw
        strings, 'T' sorts above ' ', so an identical instant would have read
        as newer and walked straight past the tombstone."""
        self._seed_score()
        db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT)
        self._seed_score(achieved_at=DELETED_AT_T_FORM)
        self.assertEqual(self._scores(), [])

    def test_a_score_achieved_after_the_delete_is_kept(self):
        """A tombstone must not become a permanent ban on the key."""
        self._seed_score()
        db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT)
        self._seed_score(achieved_at=LATER, score=150)
        self.assertEqual(len(self._scores()), 1)
        self.assertEqual(self._scores()[0][2], 150)

    def test_accepting_a_newer_score_clears_the_tombstone(self):
        """Left in place, it would suppress that legitimate score on every
        later sync -- the record present here and refused everywhere else."""
        self._seed_score()
        db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT)
        self._seed_score(achieved_at=LATER, score=150)
        self.assertFalse(db_operations.has_sync_tombstone('game_scores', f"{USER}:{GAME}"))

    def test_a_score_newer_than_the_delete_survives_it(self):
        """Ordering, not arrival order: a delete issued before the score was
        achieved must not remove it, however late the frame lands."""
        self._seed_score(achieved_at=LATER)
        self.assertFalse(
            db_operations.apply_synced_game_score_delete(USER, GAME, DELETED_AT))
        self.assertEqual(len(self._scores()), 1)

    def test_a_tombstone_in_the_other_spelling_still_lets_newer_scores_through(self):
        """Tombstones already on disk were not all written by this code, and
        record_sync_tombstone_at stores what it is handed. A 'T'-form
        tombstone compared as a raw string sorts above every space-form
        timestamp on the same date, so it would refuse a score earned after
        the delete -- silently, and for that whole day."""
        db_operations.record_sync_tombstone_at(
            'game_scores', f"{USER}:{GAME}", DELETED_AT_T_FORM)
        self._seed_score(achieved_at="2026-09-03 12:00:01", score=150)
        self.assertEqual(len(self._scores()), 1,
                         "a score earned after the delete was refused")

    def test_the_row_can_be_brought_back(self):
        self._seed_score()
        db_operations.delete_game_score(USER, GAME, deleted_at=DELETED_AT)
        self.assertTrue(
            db_operations.restore_sync_tombstone(f"game_scores:{USER}:{GAME}"))
        self.assertEqual(len(self._scores()), 1)
        self.assertEqual(self._scores()[0][2], EXPLOIT_SCORE)


class UserProfileDeleteTests(_DbCase):
    def test_the_delete_removes_the_row(self):
        self._seed_profile()
        self.assertTrue(db_operations.delete_user_profile(USER, deleted_at=DELETED_AT))
        self.assertEqual(self._profiles(), [])

    def test_a_peer_cannot_push_the_deleted_profile_back(self):
        self._seed_profile()
        db_operations.delete_user_profile(USER, deleted_at=DELETED_AT)
        self._seed_profile()
        self.assertEqual(self._profiles(), [], "the deleted profile came back")

    def test_a_profile_seen_after_the_delete_is_kept(self):
        """Deleting an orphan profile must not lock out a user who returns."""
        self._seed_profile()
        db_operations.delete_user_profile(USER, deleted_at=DELETED_AT)
        self._seed_profile(last_seen=LATER)
        self.assertEqual(len(self._profiles()), 1)
        self.assertFalse(db_operations.has_sync_tombstone('profiles', USER))

    def test_a_tombstone_in_the_other_spelling_still_lets_a_return_through(self):
        db_operations.record_sync_tombstone_at('profiles', USER, DELETED_AT_T_FORM)
        self._seed_profile(last_seen="2026-09-03 12:00:01")
        self.assertEqual(len(self._profiles()), 1,
                         "a user seen after the delete was refused")

    def test_the_row_can_be_brought_back(self):
        self._seed_profile()
        db_operations.delete_user_profile(USER, deleted_at=DELETED_AT)
        self.assertTrue(db_operations.restore_sync_tombstone(f"profiles:{USER}"))
        self.assertEqual(len(self._profiles()), 1)
        self.assertEqual(self._profiles()[0][1], "baconbot")


class WireFrameTests(_DbCase):
    """A delete has to reach the peer, or we suppress and it re-offers,
    forever. That standoff is what _push_delete_to_peer exists to end."""

    def _dispatch(self, frame):
        message_processing.process_message(
            sender_id=1, message=frame, interface=mock.MagicMock(),
            is_sync_message=True, sender_node_id="!peer1")

    def test_an_inbound_score_delete_is_applied(self):
        self._seed_score()
        self._dispatch(f"DELETE_SCORE|{_b64(USER)}|{_b64(GAME)}|{DELETED_AT}")
        self.assertEqual(self._scores(), [])
        self.assertTrue(db_operations.has_sync_tombstone('game_scores', f"{USER}:{GAME}"))

    def test_an_inbound_profile_delete_is_applied(self):
        self._seed_profile()
        self._dispatch(f"DELETE_PROFILE|{_b64(USER)}|{DELETED_AT}")
        self.assertEqual(self._profiles(), [])
        self.assertTrue(db_operations.has_sync_tombstone('profiles', USER))

    def test_a_malformed_frame_changes_nothing(self):
        self._seed_score()
        self._dispatch("DELETE_SCORE|only-one-field")
        self._dispatch("DELETE_PROFILE|")
        self.assertEqual(len(self._scores()), 1)

    def test_a_local_delete_is_announced_to_peers(self):
        self._seed_score()
        interface = mock.MagicMock()
        with mock.patch.object(db_operations, 'send_delete_game_score_to_bbs_nodes') as sender:
            db_operations.delete_game_score(USER, GAME, ['!peer'], interface,
                                            deleted_at=DELETED_AT)
        sender.assert_called_once()
        self.assertEqual(sender.call_args[0][0], USER)
        self.assertEqual(sender.call_args[0][1], GAME)

    def test_reconciliation_pushes_the_delete_rather_than_going_quiet(self):
        """Suppressing our own re-pull is only half of it. Without telling the
        peer, we refuse what it offers and it offers again on the next cycle."""
        db_operations.record_sync_tombstone_at('game_scores', f"{USER}:{GAME}", DELETED_AT)
        interface = mock.MagicMock()
        with mock.patch.object(message_processing, 'send_delete_game_score_to_bbs_nodes') as sender:
            message_processing._push_delete_to_peer(
                'game_scores', f"{USER}:{GAME}", '!peer', interface)
        sender.assert_called_once()

        db_operations.record_sync_tombstone_at('profiles', USER, DELETED_AT)
        with mock.patch.object(message_processing, 'send_delete_user_profile_to_bbs_nodes') as sender:
            message_processing._push_delete_to_peer('profiles', USER, '!peer', interface)
        sender.assert_called_once()


class ScopeCoverageTests(unittest.TestCase):
    def test_both_scopes_are_tombstone_aware(self):
        """Hash repair consults this list before pulling a record it lacks.
        A scope left out of it re-pulls whatever it deleted."""
        self.assertIn('game_scores', message_processing.TOMBSTONE_AWARE_SCOPES)
        self.assertIn('profiles', message_processing.TOMBSTONE_AWARE_SCOPES)

    def test_every_tombstone_aware_scope_can_propagate_its_deletes(self):
        """Being in the list without a delete frame is the standoff, not a
        fix. Anything added here later has to be pushable too."""
        pushable = set()
        interface = mock.MagicMock()
        for scope in message_processing.TOMBSTONE_AWARE_SCOPES:
            key = 'comment:x' if scope == 'channel_comments' else 'x:y'
            tomb_scope = 'channels' if scope == 'channel_comments' else scope
            with mock.patch.object(message_processing, 'get_sync_tombstone_deleted_at',
                                   return_value=DELETED_AT), \
                 mock.patch.multiple(
                     message_processing,
                     send_delete_bulletin_to_bbs_nodes=mock.DEFAULT,
                     send_delete_mail_to_bbs_nodes=mock.DEFAULT,
                     send_delete_channel_to_bbs_nodes=mock.DEFAULT,
                     send_delete_channel_comment_to_bbs_nodes=mock.DEFAULT,
                     send_delete_zork_save_to_bbs_nodes=mock.DEFAULT,
                     send_delete_game_score_to_bbs_nodes=mock.DEFAULT,
                     send_delete_user_profile_to_bbs_nodes=mock.DEFAULT) as senders:
                message_processing._push_delete_to_peer(tomb_scope, key, '!peer', interface)
                if any(s.called for s in senders.values()):
                    pushable.add(scope)
        self.assertEqual(pushable, set(message_processing.TOMBSTONE_AWARE_SCOPES))


if __name__ == "__main__":
    unittest.main()
