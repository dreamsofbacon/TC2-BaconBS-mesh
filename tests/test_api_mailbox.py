"""Tests for the Phase 2 store-and-forward API mailbox.

A gateway persists API responses keyed by the requester node so an
intermittently-connected node can retrieve them later via APIPOLL.
"""

import io
import json
import sqlite3
import sys
import types
import time
import unittest
from unittest.mock import patch

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import message_processing
import gateway
import utils


class _Iface:
    def __init__(self, allowed=None, bbs_nodes=None):
        self.sent_texts = []
        self.bbs_nodes = bbs_nodes or []
        self.allowed_nodes = allowed or []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append((destinationId, text))


def _drain_threads():
    for _ in range(50):
        time.sleep(0.02)
        if any(t.name.startswith("apigw-") for t in __import__("threading").enumerate()):
            continue
        break


class MailboxStoreTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_enqueue_fetch_mark_delivered(self):
        rid_id = db_operations.enqueue_api_response("r1", "!node", "200", "hello")
        self.assertGreater(rid_id, 0)
        pending = db_operations.fetch_undelivered_api_responses("!node")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['rid'], "r1")
        self.assertEqual(pending[0]['body'], "hello")
        db_operations.mark_api_responses_delivered([pending[0]['id']])
        self.assertEqual(db_operations.fetch_undelivered_api_responses("!node"), [])

    def test_fetch_is_per_node(self):
        db_operations.enqueue_api_response("rA", "!a", "200", "x")
        db_operations.enqueue_api_response("rB", "!b", "200", "y")
        self.assertEqual(len(db_operations.fetch_undelivered_api_responses("!a")), 1)
        self.assertEqual(len(db_operations.fetch_undelivered_api_responses("!b")), 1)

    def test_prune_by_rows(self):
        for i in range(10):
            db_operations.enqueue_api_response(f"r{i}", "!n", "200", "z")
        deleted = db_operations.prune_api_mailbox(max_age_days=0, max_rows=4)
        self.assertEqual(deleted, 6)
        # newest 4 remain
        remaining = db_operations.fetch_undelivered_api_responses("!n", limit=100)
        self.assertEqual(len(remaining), 4)

    def test_prune_delivered_by_age(self):
        rid_id = db_operations.enqueue_api_response("old", "!n", "200", "z")
        db_operations.mark_api_responses_delivered([rid_id])
        # force the delivered_at into the past
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE api_mailbox SET delivered_at = '2000-01-01 00:00:00' WHERE id = ?", (rid_id,))
        conn.commit()
        deleted = db_operations.prune_api_mailbox(max_age_days=7, max_rows=5000)
        self.assertEqual(deleted, 1)

    def test_maintenance_summary_includes_mailbox(self):
        for i in range(3):
            db_operations.enqueue_api_response(f"r{i}", "!n", "200", "z")
        with patch.object(db_operations, "get_maintenance_config", lambda: {
            'interval_minutes': 60, 'sync_transmissions_max_rows': 10000,
            'op_log_max_rows': 20000, 'sync_session_history_max_rows': 2000,
            'tombstone_max_age_days': 30, 'vacuum_interval_hours': 24,
            'api_mailbox_max_age_days': 0, 'api_mailbox_max_rows': 1,
        }):
            summary = db_operations.run_db_maintenance(do_vacuum=False)
        self.assertEqual(summary['api_mailbox_deleted'], 2)


class MailboxProtocolTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        message_processing._apigw_response_buffers.clear()
        utils._apigw_pending.clear()
        utils._apigw_sent.clear()
        gateway._recent_requests.clear()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_cap_apimb_advertised_with_gateway(self):
        with patch.object(utils, "_config_bool", lambda s, o, d: True):
            self.assertIn("apimb", utils.local_capabilities_token())
        with patch.object(utils, "_config_bool", lambda s, o, d: False):
            self.assertNotIn("apimb", utils.local_capabilities_token())

    def test_send_api_poll_targets_apimb_peers_only(self):
        iface = _Iface(bbs_nodes=["!gw", "!plain"])
        def _supports(peers, cap):
            return peers == ["!gw"] and cap == "apimb"
        with patch.object(utils, "peers_all_support", _supports):
            sent = utils.send_api_poll("!me", iface)
        self.assertEqual(sent, 1)
        self.assertEqual(iface.sent_texts, [("!gw", "APIPOLL|!me")])

    def test_apipoll_flushes_queued_responses(self):
        iface = _Iface()
        db_operations.enqueue_api_response("rX", "!offline", "200", "queued answer")
        message_processing.process_message(
            sender_id=1, message="APIPOLL|!offline",
            interface=iface, is_sync_message=True, sender_node_id="!offline",
        )
        # The queued response should have been sent to the polling node...
        self.assertTrue(any(d == "!offline" and "queued answer" in t for d, t in iface.sent_texts))
        # ...and marked delivered so a second poll returns nothing.
        self.assertEqual(db_operations.fetch_undelivered_api_responses("!offline"), [])

    def test_apipoll_empty_mailbox_sends_nothing(self):
        iface = _Iface()
        message_processing.process_message(
            sender_id=1, message="APIPOLL|!nobody",
            interface=iface, is_sync_message=True, sender_node_id="!nobody",
        )
        self.assertEqual(iface.sent_texts, [])

    def test_gateway_reply_path_persists_response(self):
        """The APIREQ reply path persists to the mailbox AND sends immediately.
        handle_apireq is stubbed to call reply_fn synchronously so the enqueue
        runs on this thread's in-memory DB (real gateway dispatches off-thread)."""
        iface = _Iface(allowed=["!user"])

        def _fake_handle(rid, requester_id, kind, payload, allowed, reply_fn,
                         response_max_bytes=None):
            self.assertEqual(response_max_bytes, 220)
            reply_fn("200", "blue sky")

        with patch.object(gateway, "is_gateway_enabled", lambda: True), \
             patch.object(gateway, "handle_apireq", _fake_handle):
            message_processing.process_message(
                sender_id=1, message="APIREQ|rP|!user|r|ai\x1fwhat color is the sky",
                interface=iface, is_sync_message=True, sender_node_id="!reqnode",
            )
        # Persisted for later poll, keyed by the requesting node...
        pending = db_operations.fetch_undelivered_api_responses("!reqnode")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['rid'], "rP")
        self.assertIn("blue sky", pending[0]['body'])
        # ...and also sent immediately (best-effort) to a listening node.
        self.assertTrue(any(d == "!reqnode" for d, _ in iface.sent_texts))


if __name__ == "__main__":
    unittest.main()
