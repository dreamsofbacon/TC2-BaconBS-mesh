"""Tests for the multi-device user account system (Phase 1: schema + the
db_operations.py helper functions). See the plan's "Critical grounding
fact": accounts/linked_nodes/link_codes/link_attempts are keyed on the
STABLE STRING node id, deliberately never the numeric id user_profiles
uses, to avoid inheriting the MeshCore/Meshtastic numeric-id collision
surface described in meshcore_interface.py's _node_num().
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations


class AccountsSchemaTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_initialize_database_is_idempotent(self):
        # Calling it again (e.g. a restart against an existing DB) must not
        # raise or duplicate anything.
        db_operations.initialize_database()
        db_operations.initialize_database()
        conn = db_operations.get_db_connection()
        for table in ("accounts", "linked_nodes", "link_codes", "link_attempts"):
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_tables_exist_with_expected_columns(self):
        conn = db_operations.get_db_connection()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        self.assertEqual(cols, {"account_id", "alias", "created_at"})
        cols = {row[1] for row in conn.execute("PRAGMA table_info(linked_nodes)")}
        self.assertEqual(cols, {"node_id", "account_id", "network", "linked_at"})


class AccountLifecycleTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_create_account_and_link_first_node(self):
        account_id = db_operations.create_account()
        self.assertTrue(account_id)
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        self.assertEqual(db_operations.get_account_id_for_node("!aaa11111"), account_id)
        self.assertEqual(db_operations.get_linked_node_ids(account_id), ["!aaa11111"])
        self.assertEqual(db_operations.count_linked_nodes(account_id), 1)

    def test_link_second_node_different_network(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        nodes = db_operations.get_linked_node_ids(account_id)
        self.assertEqual(set(nodes), {"!aaa11111", "7e18ca9d30a1"})
        detail = db_operations.get_linked_nodes_detail(account_id)
        networks = {row[0]: row[1] for row in detail}
        self.assertEqual(networks["!aaa11111"], "meshtastic")
        self.assertEqual(networks["7e18ca9d30a1"], "meshcore")

    def test_unlink_refuses_to_empty_an_account(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        self.assertFalse(db_operations.unlink_node("!aaa11111"))
        self.assertEqual(db_operations.count_linked_nodes(account_id), 1)

    def test_unlink_succeeds_when_another_node_remains(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        self.assertTrue(db_operations.unlink_node("!aaa11111"))
        self.assertIsNone(db_operations.get_account_id_for_node("!aaa11111"))
        self.assertEqual(db_operations.get_linked_node_ids(account_id), ["7e18ca9d30a1"])

    def test_unlink_unknown_node_is_a_noop(self):
        self.assertFalse(db_operations.unlink_node("!nonexistent"))

    def test_alias_default_empty_and_settable(self):
        account_id = db_operations.create_account()
        self.assertEqual(db_operations.get_account_alias(account_id), "")
        db_operations.set_account_alias(account_id, "BaconFan")
        self.assertEqual(db_operations.get_account_alias(account_id), "BaconFan")

    def test_alias_is_capped_short(self):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, "X" * 100)
        self.assertLessEqual(len(db_operations.get_account_alias(account_id)), 20)


class LinkCodeTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", self.account_id, "meshtastic")

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_code_is_six_digits(self):
        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_redeem_links_the_new_node(self):
        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        ok, msg = db_operations.redeem_link_code(code, "7e18ca9d30a1", "meshcore")
        self.assertTrue(ok, msg)
        self.assertEqual(db_operations.get_account_id_for_node("7e18ca9d30a1"), self.account_id)

    def test_code_is_single_use(self):
        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        ok1, _ = db_operations.redeem_link_code(code, "7e18ca9d30a1", "meshcore")
        self.assertTrue(ok1)
        ok2, msg2 = db_operations.redeem_link_code(code, "!bbb22222", "meshtastic")
        self.assertFalse(ok2)
        self.assertIn("Invalid or already-used", msg2)
        # second node must NOT have been linked
        self.assertIsNone(db_operations.get_account_id_for_node("!bbb22222"))

    def test_expired_code_is_rejected(self):
        code = db_operations.create_link_code(self.account_id, "!aaa11111", ttl_minutes=-1)
        ok, msg = db_operations.redeem_link_code(code, "7e18ca9d30a1", "meshcore")
        self.assertFalse(ok)
        self.assertIn("expired", msg)

    def test_bogus_code_is_rejected(self):
        ok, msg = db_operations.redeem_link_code("000000", "7e18ca9d30a1", "meshcore")
        self.assertFalse(ok)

    def test_cannot_link_a_node_already_on_another_account(self):
        other_account = db_operations.create_account()
        db_operations.link_node_to_account("!ccc33333", other_account, "meshtastic")
        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        ok, msg = db_operations.redeem_link_code(code, "!ccc33333", "meshtastic")
        self.assertFalse(ok)
        self.assertIn("different account", msg)
        # unchanged
        self.assertEqual(db_operations.get_account_id_for_node("!ccc33333"), other_account)

    def test_redeeming_own_account_again_is_a_noop_rejection(self):
        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        ok, msg = db_operations.redeem_link_code(code, "!aaa11111", "meshtastic")
        self.assertFalse(ok)

    def test_max_devices_enforced(self):
        for i in range(5):
            db_operations.link_node_to_account(f"!extra{i:04d}", self.account_id, "meshtastic")
        self.assertEqual(db_operations.count_linked_nodes(self.account_id), 6)
        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        ok, msg = db_operations.redeem_link_code(code, "!oneMoreNode", "meshtastic", max_devices=6)
        self.assertFalse(ok)
        self.assertIn("maximum", msg)

    def test_numerically_colliding_ids_do_not_cross_link(self):
        """Simulates the MeshCore/Meshtastic numeric-id-collision scenario:
        two different STRING node ids that would synthesize to the SAME
        numeric sender_id (per meshcore_interface.py's _node_num taking just
        the first 8 hex chars) must still be treated as fully distinct
        identities here, because this whole layer keys on the string id."""
        meshtastic_id = "!04d2ff00"  # numeric 0x04d2ff00
        meshcore_id = "04d2ff00aabbccdd"  # first 8 hex chars collide with the above
        self.assertNotEqual(meshtastic_id, meshcore_id)

        code = db_operations.create_link_code(self.account_id, "!aaa11111")
        ok, _ = db_operations.redeem_link_code(code, meshcore_id, "meshcore")
        self.assertTrue(ok)
        # The Meshtastic-shaped id, despite numeric collision potential,
        # was never linked and has no account.
        self.assertIsNone(db_operations.get_account_id_for_node(meshtastic_id))
        self.assertEqual(db_operations.get_account_id_for_node(meshcore_id), self.account_id)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_under_limit_is_ok(self):
        for _ in range(2):
            db_operations.record_link_attempt("!aaa11111", "request_code", True)
        self.assertTrue(db_operations.link_rate_limit_ok("!aaa11111", "request_code", max_per_hour=3))

    def test_at_limit_is_rejected(self):
        for _ in range(3):
            db_operations.record_link_attempt("!aaa11111", "request_code", True)
        self.assertFalse(db_operations.link_rate_limit_ok("!aaa11111", "request_code", max_per_hour=3))

    def test_kinds_are_independent(self):
        for _ in range(5):
            db_operations.record_link_attempt("!aaa11111", "submit_code", False)
        # submit_code attempts don't count against request_code's limit
        self.assertTrue(db_operations.link_rate_limit_ok("!aaa11111", "request_code", max_per_hour=3))

    def test_nodes_are_independent(self):
        for _ in range(5):
            db_operations.record_link_attempt("!aaa11111", "submit_code", False)
        self.assertTrue(db_operations.link_rate_limit_ok("!bbb22222", "submit_code", max_per_hour=5))

    def test_zero_or_negative_limit_disables_limiting(self):
        for _ in range(50):
            db_operations.record_link_attempt("!aaa11111", "submit_code", False)
        self.assertTrue(db_operations.link_rate_limit_ok("!aaa11111", "submit_code", max_per_hour=0))


class AccountAuthorizedTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_unlinked_node_falls_through_to_plain_membership(self):
        # No account exists at all -- must behave exactly like today's
        # `node_id in allowed_nodes` check.
        self.assertTrue(db_operations.account_authorized("!aaa11111", [["!aaa11111"]]))
        self.assertFalse(db_operations.account_authorized("!zzz99999", [["!aaa11111"]]))

    def test_sibling_node_inherits_authorization(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        # Only the meshtastic node is on the configured allow-list...
        self.assertTrue(db_operations.account_authorized("!aaa11111", [["!aaa11111"]]))
        # ...but its meshcore sibling is authorized too, via the account link.
        self.assertTrue(db_operations.account_authorized("7e18ca9d30a1", [["!aaa11111"]]))

    def test_dual_radio_union_of_allow_lists(self):
        """The urgent-board case: [allow_list] and [allow_list2] are two
        SEPARATE per-radio lists. An account with one node on each network
        must be authorized via the UNION of both lists, not just whichever
        list the currently-checked node's own radio uses."""
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")
        allow_list = ["!aaa11111"]       # primary radio's list
        allow_list2 = []                  # secondary radio's list -- empty
        self.assertTrue(db_operations.account_authorized("!aaa11111", [allow_list, allow_list2]))
        self.assertTrue(db_operations.account_authorized("7e18ca9d30a1", [allow_list, allow_list2]))

    def test_unrelated_account_not_authorized(self):
        account_a = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_a, "meshtastic")
        account_b = db_operations.create_account()
        db_operations.link_node_to_account("!bbb22222", account_b, "meshtastic")
        self.assertFalse(db_operations.account_authorized("!bbb22222", [["!aaa11111"]]))


if __name__ == "__main__":
    unittest.main()
