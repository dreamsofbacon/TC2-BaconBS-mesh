"""Which peers a board's posts are allowed to reach.

`local_only` already meant "this node only", but only the web admin could
set it and it was all-or-nothing. A board now carries an audience --
everywhere, this node only, or named peers -- and each post takes a copy of
it when it is written.

The rule the whole feature rests on: **whatever a peer is not sent must
also be absent from what that peer compares against.** Sync finds drift by
comparing counts and hashes; withhold a record from the data but leave it
in the hash and that peer mismatches forever, repairing nothing. Every test
in NoRepairLoopTests is there to catch that.

The leak these tests were written against, in the code as shipped in
v0.1.698: a local-only bulletin was still written to op_log, so peers were
told its id existed by an EVENT frame, and get_bulletin_by_unique_id --
which answers the HASHMISS that follows -- never checked local_only. A post
marked "never leaves this node" would be handed over on request.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import message_processing as mp

PEER_A = "!aaaa1111"
PEER_B = "!bbbb2222"
LOCAL = "!self0000"


class _Iface:
    protocol_name = "meshtastic"
    max_text_bytes = 220

    def __init__(self):
        self.nodes = {}
        self.bbs_nodes = [PEER_A, PEER_B]


class _Sync(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()
        self.sent = []
        patcher = mock.patch.object(
            db_operations, 'get_local_node_id', return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def post(self, board="General", subject="hi", content="body", nodes=None):
        """Write a bulletin the way the BBS does, recording outbound sends."""
        sends = []
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes',
                               side_effect=lambda *a, **k: sends.append((a, k))):
            unique_id = db_operations.add_bulletin(
                board, "caller", subject, content,
                self.iface.bbs_nodes if nodes is None else nodes, self.iface)
        return unique_id, sends


class ALocalOnlyPostNeverLeavesTests(_Sync):
    """The leak. Every one of these failed before this change."""

    def setUp(self):
        super().setUp()
        db_operations.set_board_audience("Private", "local", [])

    def test_it_is_not_served_when_a_peer_asks_for_it_by_id(self):
        unique_id, _ = self.post(board="Private", content="the secret")
        self.assertIsNone(
            db_operations.get_bulletin_by_unique_id(unique_id, peer_id=PEER_A))

    def test_the_hashmiss_handler_sends_nothing(self):
        unique_id, _ = self.post(board="Private", content="the secret")
        with mock.patch.object(mp, 'send_bulletin_to_bbs_nodes') as send:
            mp._send_requested_record('bulletins', unique_id, PEER_A, self.iface)
        send.assert_not_called()

    def test_it_is_still_readable_here(self):
        """Withheld from peers, not from the node that wrote it."""
        unique_id, _ = self.post(board="Private", content="the secret")
        row = db_operations.get_bulletin_by_unique_id(unique_id)
        self.assertIsNotNone(row)
        self.assertEqual("the secret", row[4])

    def test_no_op_log_event_announces_it(self):
        unique_id, _ = self.post(board="Private", content="the secret")
        conn = db_operations.get_db_connection()
        rows = conn.execute(
            "SELECT target_uid FROM op_log WHERE target_uid = ?",
            (unique_id,)).fetchall()
        self.assertEqual([], rows, "the post was announced to peers in op_log")

    def test_it_is_not_broadcast_when_written(self):
        _, sends = self.post(board="Private")
        self.assertEqual([], sends)


class ChosenPeersTests(_Sync):
    def setUp(self):
        super().setUp()
        db_operations.set_board_audience("Ops", "peers", [PEER_A])

    def test_only_the_chosen_peer_is_sent_the_new_post(self):
        _, sends = self.post(board="Ops")
        self.assertEqual(1, len(sends), "expected exactly one outbound send")
        self.assertEqual([PEER_A], list(sends[0][0][5]))

    def test_the_chosen_peer_may_request_it(self):
        unique_id, _ = self.post(board="Ops")
        self.assertIsNotNone(
            db_operations.get_bulletin_by_unique_id(unique_id, peer_id=PEER_A))

    def test_another_peer_may_not(self):
        unique_id, _ = self.post(board="Ops")
        self.assertIsNone(
            db_operations.get_bulletin_by_unique_id(unique_id, peer_id=PEER_B))

    def test_an_ordinary_board_still_goes_everywhere(self):
        unique_id, sends = self.post(board="General")
        self.assertEqual([PEER_A, PEER_B], list(sends[0][0][5]))
        for peer in (PEER_A, PEER_B):
            with self.subTest(peer=peer):
                self.assertIsNotNone(
                    db_operations.get_bulletin_by_unique_id(unique_id, peer_id=peer))

    def test_the_audience_is_a_snapshot_taken_when_the_post_is_written(self):
        """Widening a board later must not hand over what was written while
        it was narrow, and narrowing it must not un-send what peers hold."""
        unique_id, _ = self.post(board="Ops")
        db_operations.set_board_audience("Ops", "all", [])
        self.assertIsNone(
            db_operations.get_bulletin_by_unique_id(unique_id, peer_id=PEER_B))
        later, _ = self.post(board="Ops", subject="after")
        self.assertIsNotNone(
            db_operations.get_bulletin_by_unique_id(later, peer_id=PEER_B))


class NoRepairLoopTests(_Sync):
    """What a peer is told exists must match what it can be given.

    A count or hash that includes a record the peer will never receive makes
    that scope mismatch on every cycle, and the repair finds nothing to fix
    -- the failure the hash comments in db_operations warn about.
    """

    def setUp(self):
        super().setUp()
        db_operations.set_board_audience("Ops", "peers", [PEER_A])
        db_operations.set_board_audience("Private", "local", [])
        self.post(board="General")
        self.post(board="Ops")
        self.post(board="Private")

    def test_counts_match_what_each_peer_can_receive(self):
        for peer, expected in ((PEER_A, 2), (PEER_B, 1)):
            with self.subTest(peer=peer):
                counts = db_operations.get_local_record_counts(peer_id=peer)
                self.assertEqual(expected, counts['bulletins'])

    def test_the_manifest_matches_the_count(self):
        for peer in (PEER_A, PEER_B):
            with self.subTest(peer=peer):
                counts = db_operations.get_local_record_counts(peer_id=peer)
                manifest = db_operations.get_record_hash_manifest(
                    'bulletins', peer_id=peer)
                self.assertEqual(counts['bulletins'], len(manifest))

    def test_every_manifest_entry_can_actually_be_served(self):
        for peer in (PEER_A, PEER_B):
            manifest = db_operations.get_record_hash_manifest(
                'bulletins', peer_id=peer)
            for unique_id in manifest:
                with self.subTest(peer=peer, unique_id=unique_id):
                    self.assertIsNotNone(db_operations.get_bulletin_by_unique_id(
                        unique_id, peer_id=peer))

    def test_two_peers_with_different_audiences_get_different_hashes(self):
        a = db_operations.get_local_record_counts(peer_id=PEER_A)
        b = db_operations.get_local_record_counts(peer_id=PEER_B)
        self.assertNotEqual(a['bulletins_hash'], b['bulletins_hash'])

    def test_the_unfiltered_view_is_unchanged_for_the_node_itself(self):
        counts = db_operations.get_local_record_counts()
        self.assertEqual(2, counts['bulletins'],
                         "local_only is still excluded from the local view")


class BoardAudienceStoreTests(_Sync):
    def test_an_unset_board_is_everywhere(self):
        self.assertEqual(('all', []), db_operations.get_board_audience("General"))

    def test_it_round_trips(self):
        db_operations.set_board_audience("Ops", "peers", [PEER_A, PEER_B])
        self.assertEqual(('peers', [PEER_A, PEER_B]),
                         db_operations.get_board_audience("Ops"))

    def test_choosing_no_peers_is_the_same_as_this_node_only(self):
        """Otherwise "chosen nodes" with an empty list would silently mean
        "everywhere", which is the opposite of what was asked for."""
        db_operations.set_board_audience("Ops", "peers", [])
        self.assertEqual(('local', []), db_operations.get_board_audience("Ops"))

    def test_a_board_name_is_matched_the_way_boards_are_matched(self):
        db_operations.set_board_audience("Ops", "local", [])
        self.assertEqual(('local', []), db_operations.get_board_audience("ops"))

    def test_setting_it_back_to_all_clears_it(self):
        db_operations.set_board_audience("Ops", "local", [])
        db_operations.set_board_audience("Ops", "all", [])
        self.assertEqual(('all', []), db_operations.get_board_audience("Ops"))


if __name__ == "__main__":
    unittest.main()
