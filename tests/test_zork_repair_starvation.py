"""zork_saves goes last in a repair cycle, but it must not go never.

Zork saves carry the largest, most chunk-loss-prone payloads of any scope,
so when several scopes are out of sync with a peer they are deliberately
held back and repaired on "a later cycle". That reasoning holds for a
transient backlog and collapses for a peer that never fully converges.

On the live mesh, Chattanooga sat with mail, channels and game_scores
permanently mismatched. Over six hours zork_saves was deferred on all
fourteen repair cycles and requested zero times, while its copy of the
saves stayed a record behind ours. "Later" never arrived.
"""

import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import message_processing as mp


PEER = "mqtt:baconbbsvt:Chattanooga"
# Exactly what the node reported for that peer.
LIVE_SCOPES = ['mail', 'channels', 'zork_saves', 'game_scores', 'tombstones']


class ZorkRepairStarvationTests(unittest.TestCase):
    def setUp(self):
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        self.requested = []
        mp._recent_syncstate_repairs.clear()
        mp._pending_hashreq.clear()
        mp._zork_deferrals.clear()
        self.addCleanup(mp._recent_syncstate_repairs.clear)
        self.addCleanup(mp._pending_hashreq.clear)
        self.addCleanup(mp._zork_deferrals.clear)

        patches = [
            mock.patch.object(mp, 'get_sync_progress', return_value={'in_progress': False}),
            # Zero cycle time so the rate limiter does not swallow the repeat
            # calls; a real node simply waits between them.
            mock.patch.object(mp, 'get_repair_cycle_seconds', return_value=0),
            mock.patch.object(mp, 'get_scopes_to_request_repair',
                              side_effect=lambda _peer, scopes: list(scopes)),
            mock.patch.object(mp, 'send_hash_request_to_bbs_nodes',
                              side_effect=self._record),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _record(self, peers, interface, scope=None):
        self.requested.append(scope)

    def _cycle(self, scopes=LIVE_SCOPES):
        """One repair opportunity, as if a SYNCSTATE had just arrived."""
        self.requested = []
        # A real cycle's HASHREQs have completed or timed out by the next one.
        mp._pending_hashreq.clear()
        mp._recent_syncstate_repairs.clear()
        with mock.patch.object(mp, 'get_mismatched_peer_scopes',
                               return_value={PEER: list(scopes)}):
            mp._request_targeted_repair_if_needed(PEER, self.iface)
        return self.requested

    # -- the behaviour that was correct and must survive -------------------

    def test_zork_goes_last_while_other_scopes_are_being_repaired(self):
        self.assertNotIn('zork_saves', self._cycle())

    def test_the_other_scopes_are_still_repaired_meanwhile(self):
        requested = self._cycle()
        for scope in ('mail', 'channels', 'game_scores'):
            self.assertIn(scope, requested)

    def test_zork_is_requested_immediately_when_nothing_else_is_waiting(self):
        self.assertIn('zork_saves', self._cycle(['zork_saves', 'tombstones']))

    # -- the starvation itself ---------------------------------------------

    def test_zork_is_eventually_requested_even_if_nothing_else_converges(self):
        """The live failure: the other scopes never converge, so under the
        old rule zork was dropped from every cycle forever."""
        seen = [scope for _ in range(mp.ZORK_DEFERRAL_LIMIT + 1)
                for scope in self._cycle()]
        self.assertIn('zork_saves', seen)

    def test_it_waits_its_turn_before_that(self):
        for cycle in range(mp.ZORK_DEFERRAL_LIMIT):
            with self.subTest(cycle=cycle):
                self.assertNotIn('zork_saves', self._cycle())
        self.assertIn('zork_saves', self._cycle())

    def test_a_served_peer_starts_waiting_again(self):
        """Otherwise zork would jump the queue on every later cycle, which
        is the opposite failure -- its payloads would crowd out the rest."""
        for _ in range(mp.ZORK_DEFERRAL_LIMIT + 1):
            self._cycle()
        self.assertNotIn('zork_saves', self._cycle())

    def test_waiting_is_tracked_per_peer(self):
        other = "mqtt:baconbbs:node2"
        for _ in range(mp.ZORK_DEFERRAL_LIMIT + 1):
            self._cycle()
        self.requested = []
        mp._pending_hashreq.clear()
        mp._recent_syncstate_repairs.clear()
        with mock.patch.object(mp, 'get_mismatched_peer_scopes',
                               return_value={other: list(LIVE_SCOPES)}):
            mp._request_targeted_repair_if_needed(other, self.iface)
        self.assertNotIn('zork_saves', self.requested,
                         "one peer's wait must not spend another peer's")

    def test_a_converged_peer_forgets_its_wait(self):
        """A peer that stops needing the deferral should not carry a part-used
        allowance into the next time it does."""
        self._cycle()
        self.assertIn(PEER, mp._zork_deferrals)
        self._cycle(['zork_saves', 'tombstones'])
        self.assertNotIn(PEER, mp._zork_deferrals)


if __name__ == "__main__":
    unittest.main()
