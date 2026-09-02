"""Hop count and the originating node's real name.

Two things the chatter list could not show. Hops answers "how far away is
this station", which is the question people actually ask of public traffic.
And sender_name only ever held the SHORT name -- for most nodes that is the
hex tail of their id ('43b5', 'a02c'), which identifies nobody. The roster
already knows the long name.

The distinction that matters throughout: 0 hops is a real answer meaning
"heard direct", and NULL means the packet carried nothing usable. Collapsing
them loses the more interesting of the two.
"""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import public_chatter


class HopExtractionTests(unittest.TestCase):
    """hop_start is the TTL the sender set; hop_limit is what was left."""

    def test_hops_are_the_difference(self):
        self.assertEqual(
            public_chatter.hops_used({"hopStart": 3, "hopLimit": 1}), 2)

    def test_a_packet_heard_direct_is_zero_not_unknown(self):
        self.assertEqual(
            public_chatter.hops_used({"hopStart": 3, "hopLimit": 3}), 0)

    def test_a_missing_hop_start_is_unknown(self):
        """Firmware before 2.x never sent it."""
        self.assertIsNone(public_chatter.hops_used({"hopLimit": 3}))

    def test_no_hop_fields_at_all_is_unknown(self):
        """protobuf omits zero-valued fields, so this is common."""
        self.assertIsNone(public_chatter.hops_used({}))

    def test_a_packet_relayed_over_mqtt_reports_nothing(self):
        """Its hop fields describe somebody else's radio path. Reporting that
        as our hop count would be worse than reporting nothing."""
        self.assertIsNone(public_chatter.hops_used(
            {"hopStart": 3, "hopLimit": 1, "viaMqtt": True}))

    def test_impossible_values_are_rejected(self):
        for packet in ({"hopStart": 1, "hopLimit": 5},      # limit above start
                       {"hopStart": 99, "hopLimit": 0},     # beyond the cap
                       {"hopStart": "x", "hopLimit": 1}):   # not a number
            with self.subTest(packet=packet):
                self.assertIsNone(public_chatter.hops_used(packet))

    def test_meshcore_reports_its_path_length_as_hops(self):
        """MeshCore has no TTL to subtract: each repeater appends its hash to
        the path, so the path length IS the hop count. Reading only the
        Meshtastic pair reported every MeshCore message as unknown."""
        self.assertEqual(public_chatter.hops_used({"path_len": 2}), 2)

    def test_a_meshcore_packet_heard_direct_is_zero(self):
        self.assertEqual(public_chatter.hops_used({"path_len": 0}), 0)

    def test_meshcores_direct_routing_sentinel_is_not_255_hops(self):
        """255 flags direct (non-flood) routing, not a very long path."""
        self.assertIsNone(public_chatter.hops_used({"path_len": 255}))

    def test_a_path_longer_than_six_bits_is_rejected(self):
        self.assertIsNone(public_chatter.hops_used({"path_len": 64}))

    def test_a_meshcore_path_beyond_the_meshtastic_cap_still_counts(self):
        """The 7-hop ceiling is a Meshtastic TTL limit and must not be
        applied to MeshCore, whose paths can legitimately run longer."""
        self.assertEqual(public_chatter.hops_used({"path_len": 12}), 12)

    def test_a_non_numeric_path_length_is_unknown(self):
        self.assertIsNone(public_chatter.hops_used({"path_len": "x"}))

    def test_meshcore_over_mqtt_still_reports_nothing(self):
        """Somebody else's radio path, same as for Meshtastic."""
        self.assertIsNone(
            public_chatter.hops_used({"path_len": 3, "viaMqtt": True}))

    def test_snake_case_keys_are_accepted(self):
        """The library hands over camelCase, but a raw protobuf dict uses
        snake_case and both reach this code path."""
        self.assertEqual(
            public_chatter.hops_used({"hop_start": 4, "hop_limit": 1}), 3)


class _Chatter(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.now = datetime.now(timezone.utc)
        self.ts = self.now.isoformat().replace("+00:00", "Z")
        self.expires = (self.now + timedelta(days=7)).isoformat().replace("+00:00", "Z")

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _roster(self, node_id, short_name, long_name):
        db_operations.thread_local.connection.execute(
            "INSERT INTO mesh_clients (node_id, short_name, long_name, "
            "link_name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, short_name, long_name, "primary", self.ts, self.ts))

    def _add(self, unique_id, **changes):
        fields = dict(
            network="meshtastic", channel_index=0, channel_name="LongFast",
            sender_node_id="!1bbecf78", sender_name="zrk", content="hello",
            message_timestamp=self.ts, captured_at=self.ts,
            capture_node_id="!me", expires_at=self.expires)
        fields.update(changes)
        return db_operations.add_public_chatter(unique_id=unique_id, **fields)

    def _first(self):
        return db_operations.get_public_chatter_history()["entries"][0]


class HopStorageTests(_Chatter):
    def test_a_hop_count_is_stored_and_returned(self):
        self._add("u1", hops=2)
        self.assertEqual(self._first()["hops"], 2)

    def test_zero_hops_survives_as_zero(self):
        """The whole point of the nullable column: 'heard direct' must not
        come back looking like 'unknown'."""
        self._add("u1", hops=0)
        self.assertEqual(self._first()["hops"], 0)

    def test_unknown_hops_stay_null(self):
        self._add("u1", hops=None)
        self.assertIsNone(self._first()["hops"])

    def test_hearing_it_again_over_a_shorter_path_wins(self):
        """The best path heard is the informative answer to 'how far away'."""
        self._add("u1", hops=4)
        self._add("u1", hops=1)
        self.assertEqual(self._first()["hops"], 1)

    def test_a_longer_path_does_not_overwrite_a_shorter_one(self):
        self._add("u1", hops=1)
        self._add("u1", hops=4)
        self.assertEqual(self._first()["hops"], 1)

    def test_an_unknown_repeat_does_not_erase_a_known_count(self):
        self._add("u1", hops=2)
        self._add("u1", hops=None)
        self.assertEqual(self._first()["hops"], 2)

    def test_a_known_repeat_fills_in_a_previously_unknown_count(self):
        self._add("u1", hops=None)
        self._add("u1", hops=3)
        self.assertEqual(self._first()["hops"], 3)


class OriginatingNameTests(_Chatter):
    def test_the_long_name_comes_from_the_roster(self):
        """sender_name holds only the short name, captured at receive time."""
        self._roster("!1bbecf78", "zrk", "Zorak")
        self._add("u1")
        entry = self._first()
        self.assertEqual(entry["sender_name"], "zrk")
        self.assertEqual(entry["sender_long_name"], "Zorak")

    def test_an_unknown_sender_has_no_long_name(self):
        self._add("u1", sender_node_id="!nevermet", sender_name="")
        self.assertIsNone(self._first()["sender_long_name"])

    def test_a_blank_long_name_in_the_roster_is_not_used(self):
        """Better to fall back to the short name than show an empty strong."""
        self._roster("!1bbecf78", "zrk", "")
        self._add("u1")
        self.assertIsNone(self._first()["sender_long_name"])

    def test_a_node_on_several_links_yields_one_row(self):
        """A node heard on both a radio and an MQTT link has a roster row per
        link; the join must not multiply the chatter entry."""
        self._roster("!1bbecf78", "zrk", "Zorak")
        db_operations.thread_local.connection.execute(
            "INSERT INTO mesh_clients (node_id, short_name, long_name, "
            "link_name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            ("!1bbecf78", "zrk", "Zorak", "mqtt1", self.ts, self.ts))
        self._add("u1")
        entries = db_operations.get_public_chatter_history()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sender_long_name"], "Zorak")

    def test_the_name_is_not_copied_into_the_row(self):
        """Joined rather than stored, so a rename is reflected everywhere
        instead of leaving old messages showing a stale name."""
        self._roster("!1bbecf78", "zrk", "Zorak")
        self._add("u1")
        stored = db_operations.thread_local.connection.execute(
            "PRAGMA table_info(public_chatter)").fetchall()
        self.assertNotIn("sender_long_name", {row[1] for row in stored})


class FilteringStillWorksTests(_Chatter):
    """The query grew a join and table aliases; every filter runs through it."""

    def test_search_still_matches_content(self):
        self._add("u1", content="2 hops to Highgate")
        self._add("u2", content="something else")
        found = db_operations.get_public_chatter_history(search_query="Highgate")
        self.assertEqual(len(found["entries"]), 1)

    def test_search_still_matches_the_sender_id(self):
        self._add("u1", sender_node_id="!1bbecf78")
        found = db_operations.get_public_chatter_history(search_query="1bbecf78")
        self.assertEqual(len(found["entries"]), 1)

    def test_the_network_filter_still_works(self):
        self._add("u1", network="meshtastic")
        self._add("u2", network="meshcore")
        self.assertEqual(
            len(db_operations.get_public_chatter_history(network="meshcore")["entries"]), 1)

    def test_the_channel_filter_still_works(self):
        self._add("u1", channel_index=0)
        self._add("u2", channel_index=3)
        self.assertEqual(
            len(db_operations.get_public_chatter_history(channel_index=3)["entries"]), 1)

    def test_pagination_still_works(self):
        for n in range(5):
            self._add(f"u{n}")
        page = db_operations.get_public_chatter_history(limit=2)
        self.assertEqual(len(page["entries"]), 2)
        self.assertTrue(page["has_more"])
        cursor = page["next_cursor"]
        second = db_operations.get_public_chatter_history(limit=2, **cursor)
        self.assertEqual(len(second["entries"]), 2)
        self.assertNotEqual(page["entries"][0]["id"], second["entries"][0]["id"])


class DisplayTests(unittest.TestCase):
    def _js(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / "static" / "js" / "public-chatter.js").read_text(encoding="utf-8")

    def test_the_long_name_is_preferred_in_the_feed(self):
        self.assertIn("sender_long_name", self._js())

    def test_zero_hops_is_not_treated_as_missing(self):
        """A falsy check would hide 'direct', which is the most interesting
        value there is."""
        js = self._js()
        self.assertIn("entry.hops !== null", js)
        self.assertNotIn("if (entry.hops)", js)


if __name__ == "__main__":
    unittest.main()
