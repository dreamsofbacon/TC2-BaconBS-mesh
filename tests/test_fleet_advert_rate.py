"""Fleet state is advertised on change, not once a minute for ever.

Reported 2026-09-14 and measured again on 2026-09-17: bbs.local was sending
about 13 FLEETVER, 13 NODEVER and 13 FLEETSTATUS frames a minute in steady
state -- the apply check ran every 60 seconds and advertised to every peer
each time, whether or not anything had changed. Each frame also became a log
line and an SD card write on every node that heard it.

The periodic caller now sends only when the state changed or the heartbeat is
due. Triggered adverts -- a stored target, a deploy about to apply -- are
unaffected, since a node that is about to restart has to say so at once.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

import db_operations
from test_fleet_wire import _install_fake_meshtastic_package

_install_fake_meshtastic_package()
import server  # noqa: E402  -- needs the stub package above

CONFIG = {"fleet": {"group": "baconbbsvt", "updates": "auto"}}
PEERS = ["!peer1", "!peer2"]


class _Iface:
    bbs_nodes: list = []
    allowed_nodes: list = []
    nodes: dict = {}


class FleetAdvertRateTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        server._reset_fleet_advert_state()
        self.addCleanup(server._reset_fleet_advert_state)
        self.addCleanup(self._close)
        self.state = {"state": "healthy", "detail": "already on the target commit"}
        for name in ("send_fleet_target_to_bbs_nodes", "send_node_version_to_bbs_nodes",
                     "send_fleet_status_to_bbs_nodes"):
            patcher = mock.patch.object(server, name, return_value=1)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        node = mock.patch.object(server, "get_local_node_id", return_value="!local")
        node.start()
        self.addCleanup(node.stop)
        import fleet_update
        read = mock.patch.object(fleet_update, "read_update_state",
                                 side_effect=lambda: dict(self.state))
        read.start()
        self.addCleanup(read.stop)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def advertise(self, force=False, peers=PEERS):
        return server._advertise_fleet_state(CONFIG, peers, _Iface(), force=force)

    def test_the_first_advertisement_goes_out(self):
        self.assertEqual(self.advertise(), 2)  # version + status; no target stored

    def test_saying_the_same_thing_again_sends_nothing(self):
        self.advertise()
        self.assertEqual(self.advertise(), 0)
        self.assertEqual(self.send_node_version_to_bbs_nodes.call_count, 1)

    def test_a_changed_rollout_state_goes_out_at_once(self):
        self.advertise()
        self.state = {"state": "probation", "detail": ""}
        self.assertEqual(self.advertise(), 2)

    def test_a_new_version_goes_out_at_once(self):
        self.advertise()
        with mock.patch("version_info.get_app_version", return_value="9.9.9"):
            self.assertEqual(self.advertise(), 2)

    def test_the_heartbeat_repeats_it_for_anyone_who_missed_it(self):
        self.advertise()
        with mock.patch.object(server.time, "time",
                               return_value=server.time.time() + server.FLEET_ADVERT_HEARTBEAT_SECONDS + 1):
            self.assertEqual(self.advertise(), 2)

    def test_a_different_set_of_peers_is_told_regardless(self):
        """A link that joins later has heard none of this."""
        self.advertise()
        self.assertEqual(self.advertise(peers=["!peer3"]), 2)

    def test_forcing_always_sends(self):
        self.advertise()
        self.assertEqual(self.advertise(force=True), 2)
        self.assertEqual(self.advertise(force=True), 2)

    def test_a_stored_target_is_advertised_with_the_rest(self):
        db_operations.store_fleet_target(
            {"g": "baconbbsvt", "c": "a" * 40, "v": "0.1.999",
             "k": "fk000000", "n": "n" * 16, "t": "2026-09-17T00:00:00Z"},
            "blob-goes-here")
        self.assertEqual(self.advertise(), 3)
        self.send_fleet_target_to_bbs_nodes.assert_called_once_with(
            "blob-goes-here", PEERS, mock.ANY)

    def test_the_apply_check_itself_still_runs_every_time(self):
        """Only the advertisement is rate-limited; the check that applies a
        waiting target is local and must not be skipped."""
        with mock.patch.object(server, "_apply_fleet_target_if_due",
                               return_value=False) as apply_check:
            server._process_fleet_target(CONFIG, [], force=False)
            server._process_fleet_target(CONFIG, [], force=False)
        self.assertEqual(apply_check.call_count, 2)


if __name__ == "__main__":
    unittest.main()
