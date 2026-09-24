"""What a node behind on everything asks for first.

Repair moves one record at a time, so the order of the scopes is the order a
new node fills up in. A node that joined a fleet with a long chatter history
spent hours pulling public_chatter -- overheard radio traffic that expires in
seven days -- before a single bulletin arrived. Measured on the VPS: 666
chatter records in, 0 bulletins, while the peers held 8 bulletins, 19 mail,
14 channels and 3,752 chatter. From the outside that is indistinguishable
from a node that is not syncing, and it was read that way.

So the things someone came for go first, and chatter waits until they are
done. A node with no radio can also refuse chatter outright: it overhears
nothing itself, and carrying thousands of other nodes' observations is the
first thing a new link would otherwise spend itself on.
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
import message_processing as mp

EVERYTHING = ["public_chatter", "zork_saves", "bulletins", "tombstones",
              "mail", "channels", "profiles"]


class OrderTests(unittest.TestCase):
    def test_content_comes_before_bulk(self):
        ordered = mp._order_repair_scopes(EVERYTHING)
        self.assertLess(ordered.index("bulletins"), ordered.index("public_chatter"))
        self.assertLess(ordered.index("mail"), ordered.index("public_chatter"))
        self.assertLess(ordered.index("channels"), ordered.index("public_chatter"))

    def test_zork_stays_last(self):
        """Biggest payloads, most chunk-loss-prone, least missed."""
        self.assertEqual("zork_saves", mp._order_repair_scopes(EVERYTHING)[-1])

    def test_nothing_is_dropped_by_ordering(self):
        self.assertCountEqual(EVERYTHING, mp._order_repair_scopes(EVERYTHING))

    def test_an_unknown_scope_is_kept_at_the_end(self):
        ordered = mp._order_repair_scopes(["something_new", "bulletins"])
        self.assertEqual(["bulletins", "something_new"], ordered)


class ChatterWaitsTests(unittest.TestCase):
    def test_chatter_is_deferred_while_posts_are_missing(self):
        scopes = mp._defer_chatter_if_content_is_behind(
            ["public_chatter", "bulletins"])
        self.assertEqual(["bulletins"], scopes)

    def test_chatter_proceeds_once_it_is_the_only_gap(self):
        self.assertEqual(["public_chatter"],
                         mp._defer_chatter_if_content_is_behind(["public_chatter"]))

    def test_tombstones_alone_do_not_hold_chatter_back(self):
        """Tombstones ride along with everything and would defer it for ever."""
        scopes = mp._defer_chatter_if_content_is_behind(
            ["public_chatter", "tombstones"])
        self.assertIn("public_chatter", scopes)

    def test_zork_alone_does_not_hold_chatter_back(self):
        scopes = mp._defer_chatter_if_content_is_behind(
            ["public_chatter", "zork_saves"])
        self.assertIn("public_chatter", scopes)


class OptingOutOfChatterTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _off(self):
        return mock.patch.object(db_operations, "is_public_chatter_sync_enabled",
                                 return_value=False)

    def test_it_is_on_by_default(self):
        """A node with radios has always kept its neighbours' chatter."""
        self.assertTrue(db_operations.is_public_chatter_sync_enabled())

    def test_an_opted_out_node_never_asks_for_it(self):
        with self._off():
            scopes = mp._defer_chatter_if_content_is_behind(["public_chatter"])
        self.assertEqual([], scopes)

    def test_it_advertises_a_sentinel_rather_than_looking_empty(self):
        """"I do not take part" has to read differently from "I have none",
        or peers try to close a gap this node refuses to fill, for ever."""
        with self._off():
            counts = db_operations.get_local_record_counts()
        self.assertEqual(0, counts["public_chatter"])
        self.assertEqual(db_operations.public_chatter_disabled_hash(),
                         counts["public_chatter_hash"])

    def test_a_peer_can_tell_the_difference(self):
        self.assertTrue(db_operations.peer_opts_out_of_public_chatter(
            db_operations.public_chatter_disabled_hash()))
        self.assertFalse(db_operations.peer_opts_out_of_public_chatter(""))
        self.assertFalse(db_operations.peer_opts_out_of_public_chatter("AfVd4NyJxE4"))

    def test_a_participating_node_still_hashes_its_chatter(self):
        counts = db_operations.get_local_record_counts()
        self.assertNotEqual(db_operations.public_chatter_disabled_hash(),
                            counts["public_chatter_hash"])


if __name__ == "__main__":
    unittest.main()
