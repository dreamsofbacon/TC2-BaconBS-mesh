"""The BBS name and greeting, shared across the fleet.

The frame is unsigned like every other sync frame, and this one renames the
whole BBS -- so the guard is an allow-list ([bbs] accept_identity_from,
empty by default) rather than a ceiling. A node you do not run cannot rename
your BBS, and out of the box nobody can.

Stored in the database rather than config.ini on purpose: config.ini also
holds the fleet signing keys, and an unexplained edit to it has already cost
this project a day (HANDOFF, "When the nodes stop trusting the signing
key"). A radio frame must not be able to write that file.
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

import db_operations
import utils

PEER = "mqtt:baconbbsvt:Burlington-NNE"
STRANGER = "mqtt:baconbbsvt:Chattanooga"
EARLY = "2026-09-01T10:00:00.000000+00:00"
LATER = "2026-09-02T10:00:00.000000+00:00"


class _Case(unittest.TestCase):
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

    def _trusting(self, *sources):
        return mock.patch.object(utils, "identity_sync_sources",
                                 lambda: set(sources))


class AcceptanceTests(_Case):
    def test_a_named_peer_is_adopted(self):
        with self._trusting(PEER):
            self.assertTrue(db_operations.apply_synced_fleet_identity(
                "name", "Bacon BBS", LATER, PEER))
        self.assertEqual(db_operations.get_fleet_identity("name")[0], "Bacon BBS")

    def test_nobody_is_trusted_by_default(self):
        """The default has to be closed: the frame is unsigned."""
        with mock.patch.object(utils, "_config_raw", lambda s, o: None):
            self.assertEqual(utils.identity_sync_sources(), set())
        with self._trusting():
            self.assertFalse(db_operations.apply_synced_fleet_identity(
                "name", "Not Your BBS", LATER, PEER))
        self.assertIsNone(db_operations.get_fleet_identity("name")[0])

    def test_a_node_you_did_not_name_cannot_rename_your_bbs(self):
        with self._trusting(PEER):
            self.assertFalse(db_operations.apply_synced_fleet_identity(
                "name", "Chattanooga BBS", LATER, STRANGER))
        self.assertIsNone(db_operations.get_fleet_identity("name")[0])

    def test_an_older_stamp_never_undoes_a_newer_value(self):
        db_operations.set_fleet_identity("name", "Current", LATER)
        with self._trusting(PEER):
            self.assertFalse(db_operations.apply_synced_fleet_identity(
                "name", "Stale", EARLY, PEER))
        self.assertEqual(db_operations.get_fleet_identity("name")[0], "Current")

    def test_a_future_stamp_is_refused_not_clamped(self):
        """Clamping to now makes every replay look freshly minted, so the
        peer wins on every sweep instead of once -- see the same reasoning
        in apply_synced_node_role."""
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db_operations.set_fleet_identity("name", "Current", LATER)
        with self._trusting(PEER):
            self.assertFalse(db_operations.apply_synced_fleet_identity(
                "name", "Pinned", future, PEER))
        self.assertEqual(db_operations.get_fleet_identity("name")[0], "Current")

    def test_ordinary_clock_skew_still_works(self):
        from datetime import datetime, timedelta, timezone
        skewed = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        with self._trusting(PEER):
            self.assertTrue(db_operations.apply_synced_fleet_identity(
                "name", "Skewed", skewed, PEER))

    def test_an_unknown_key_is_refused(self):
        with self._trusting(PEER):
            self.assertFalse(db_operations.apply_synced_fleet_identity(
                "node_welcome", "not yours to set", LATER, PEER))

    def test_the_node_line_is_not_a_fleet_key(self):
        """node_welcome is only true on the node that wrote it."""
        self.assertNotIn("node_welcome", db_operations.FLEET_IDENTITY_KEYS)


class StorageTests(_Case):
    def test_the_store_wins_over_the_config_seed(self):
        with mock.patch.object(utils, "_config_raw",
                               lambda s, o: "Seed" if o == "name" else None):
            self.assertEqual(utils.get_bbs_name(), "Seed")
            db_operations.set_fleet_identity("name", "Edited", LATER)
            self.assertEqual(utils.get_bbs_name(), "Edited")

    def test_an_empty_stored_name_still_falls_back_to_the_default(self):
        db_operations.set_fleet_identity("name", "", LATER)
        with mock.patch.object(utils, "_config_raw", lambda s, o: None):
            self.assertEqual(utils.get_bbs_name(), utils.WELCOME_DEFAULT_NAME)

    def test_config_is_never_written_by_an_applied_frame(self):
        import inspect
        source = inspect.getsource(db_operations.apply_synced_fleet_identity)
        for forbidden in ("config", "write_config", "config.ini"):
            self.assertNotIn(forbidden, source.split('"""')[-1])


class AdvertisementTests(_Case):
    def setUp(self):
        super().setUp()
        db_operations._advertised_identity.clear()
        db_operations._identity_last_full_sweep.clear()
        self.sent = []

        def _capture(key, value, updated_at, peers, interface):
            self.sent.append((key, value, updated_at, tuple(peers)))
            return 1

        p = mock.patch.object(utils, "send_fleet_identity_to_bbs_nodes", _capture)
        p.start()
        self.addCleanup(p.stop)

    def test_nothing_is_advertised_before_anything_is_set(self):
        """A node that has never been edited has no opinion to assert, and
        must not overwrite a peer that does."""
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        self.assertEqual(self.sent, [])

    def test_a_set_value_is_advertised_once(self):
        db_operations.set_fleet_identity("name", "Bacon BBS", LATER)
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        self.assertEqual(len(self.sent), 1)
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        self.assertEqual(len(self.sent), 1, "re-sent an unchanged value")

    def test_a_change_is_advertised_again(self):
        db_operations.set_fleet_identity("name", "Bacon BBS", EARLY)
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        db_operations.set_fleet_identity("name", "Renamed", LATER)
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        self.assertEqual(len(self.sent), 2)

    def test_it_is_re_advertised_on_a_sweep(self):
        """The only thing that heals a dropped frame: no hash scope covers
        this, so an unchanged value still has to be said again."""
        db_operations.set_fleet_identity("name", "Bacon BBS", LATER)
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        db_operations._identity_last_full_sweep[PEER] = 0.0
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        self.assertEqual(len(self.sent), 2)

    def test_the_sweep_is_per_peer(self):
        """One global timestamp meant the first link to tick consumed the
        sweep and every other link's peers never got it -- the bug the roles
        sweep already had."""
        db_operations.set_fleet_identity("name", "Bacon BBS", LATER)
        db_operations.sync_fleet_identity_to_nodes([PEER], object())
        db_operations.sync_fleet_identity_to_nodes([STRANGER], object())
        self.assertEqual([s[3] for s in self.sent], [(PEER,), (STRANGER,)])

    def test_publishing_does_not_require_trusting_anyone(self):
        """Telling peers and adopting from peers are separate decisions. A
        node with an empty allow-list is the normal shape of a fleet with
        one editor, and it still has to publish."""
        with mock.patch.object(utils, "identity_sync_sources", lambda: set()):
            db_operations.set_fleet_identity("name", "Bacon BBS", LATER)
            db_operations.sync_fleet_identity_to_nodes([PEER], object())
        self.assertEqual(len(self.sent), 1)


class WireTests(unittest.TestCase):
    def test_the_capability_is_advertised(self):
        self.assertIn("bbsid", utils.WIRE_CAPABILITIES)

    def test_only_peers_that_understand_it_are_sent_to(self):
        calls = []
        with mock.patch.object(db_operations, "peer_supports",
                               lambda p, cap: cap == "bbsid" and p == PEER), \
             mock.patch.object(utils, "_send_one_sync",
                               lambda m, p, i: calls.append((m, p))):
            utils.send_fleet_identity_to_bbs_nodes(
                "name", "Bacon BBS", LATER, [PEER, STRANGER], object())
        self.assertEqual([p for _, p in calls], [PEER])

    def test_a_greeting_containing_a_pipe_survives_the_frame(self):
        """A greeting is free text an operator types. An unencoded '|' would
        split the frame and the far side would store a truncated name."""
        calls = []
        with mock.patch.object(db_operations, "peer_supports", lambda p, c: True), \
             mock.patch.object(utils, "_send_one_sync",
                               lambda m, p, i: calls.append(m)):
            utils.send_fleet_identity_to_bbs_nodes(
                "welcome", "Mail | boards | games", LATER, [PEER], object())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].count("|"), 3, calls[0])
        payload = calls[0].split("|")[2]
        self.assertEqual(utils.decode_text(payload), "Mail | boards | games")

    def test_the_receiver_is_wired_up(self):
        import inspect
        import message_processing
        source = inspect.getsource(message_processing.process_message)
        self.assertIn('message.startswith("BBSID|")', source)
        self.assertIn("apply_synced_fleet_identity", source)

    def test_it_rides_the_tick_not_a_sync_phase(self):
        """Anything hung off a five-phase phase runs once at the dawn of
        time -- phases_complete is persisted."""
        # Read rather than import: server.py pulls in the real meshtastic
        # package, which the suite stubs.
        source = (Path(__file__).parent.parent / "server.py").read_text(
            encoding="utf-8")
        self.assertIn("sync_fleet_identity_to_nodes(sorted(current_bbs_nodes)",
                      source)
        # And beside the roles call, which is the tick -- not in a phase.
        self.assertIn("sync_node_roles_to_nodes(sorted(current_bbs_nodes)",
                      source)


if __name__ == "__main__":
    unittest.main()
