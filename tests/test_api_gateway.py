"""Tests for the API gateway over mesh (Phase 1).

Drives process_message() directly with a dummy interface and a monkeypatched
urlopen — no real network or radio. Covers gateway-side validation/dispatch and
requester-side response reassembly + delivery.
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


def _join_apiresp(frames, rid):
    """Reassemble the body from APIRESP/CONT frames a gateway emitted."""
    parts = {}
    status = None
    for _dest, t in frames:
        if t.startswith(f"APIRESP|{rid}|"):
            p = t.split("|", 4)
            status = p[2]
            parts[0] = p[4] if len(p) == 5 else ""
        elif t.startswith(f"APIRESPCONT|{rid}|"):
            p = t.split("|", 3)
            parts[int(p[2])] = p[3]
    return status, "".join(parts[o] for o in sorted(parts))


def _drain_threads():
    # gateway dispatches on a daemon thread; give it a moment to finish.
    for _ in range(50):
        time.sleep(0.02)
        if any(t.name.startswith("apigw-") for t in __import__("threading").enumerate()):
            continue
        break


class GatewayValidationTests(unittest.TestCase):
    def setUp(self):
        utils._load_runtime_config.cache_clear() if hasattr(utils._load_runtime_config, 'cache_clear') else None

    def test_cap_advertised_only_when_enabled(self):
        with patch.object(utils, "_config_bool", lambda s, o, d: True):
            self.assertIn("apigw", utils.local_capabilities_token())
        with patch.object(utils, "_config_bool", lambda s, o, d: False):
            self.assertNotIn("apigw", utils.local_capabilities_token())

    def test_validate_url_host_allowlist(self):
        cfg = {('gateway', 'allowed_hosts'): 'wttr.in', ('gateway', 'allowed_schemes'): 'https'}
        with patch.object(gateway, "_config_raw", lambda s, o: cfg.get((s, o))), \
             patch.object(gateway, "_host_is_private", lambda h: False):
            ok, _ = gateway.validate_url("https://wttr.in/NYC")
            self.assertTrue(ok)
            ok2, _ = gateway.validate_url("https://evil.example.com/x")
            self.assertFalse(ok2)
            ok3, _ = gateway.validate_url("http://wttr.in/NYC")  # scheme not allowed
            self.assertFalse(ok3)

    def test_validate_url_blocks_private(self):
        cfg = {('gateway', 'allowed_hosts'): 'wttr.in', ('gateway', 'allowed_schemes'): 'https'}
        with patch.object(gateway, "_config_raw", lambda s, o: cfg.get((s, o))), \
             patch.object(gateway, "_host_is_private", lambda h: True):
            ok, reason = gateway.validate_url("https://wttr.in/NYC")
            self.assertFalse(ok)
            self.assertIn("private", reason)

    def test_rate_limit(self):
        gateway._recent_requests.clear()
        with patch.object(gateway, "_rate_limit_per_node", lambda: 2):
            self.assertTrue(gateway._rate_ok("!n"))
            self.assertTrue(gateway._rate_ok("!n"))
            self.assertFalse(gateway._rate_ok("!n"))


class GatewayDispatchTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        message_processing._apigw_response_buffers.clear()
        utils._apigw_pending.clear()
        gateway._recent_requests.clear()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_ai_relay_ollama_roundtrip(self):
        iface = _Iface(allowed=["!user"])
        ai_cfg = {
            ('gateway', 'ai_base_url'): 'http://nomad.local:11434',
            ('gateway', 'ai_dialect'): 'ollama',
            ('gateway', 'ai_model'): 'llama3.2',
            ('gateway', 'ai_system_prompt'): '',
        }
        resp = io.BytesIO(json.dumps({"message": {"content": "The sky is blue."}}).encode())
        resp.__enter__ = lambda *_: resp
        resp.__exit__ = lambda *_: False
        with patch.object(gateway, "is_gateway_enabled", lambda: True), \
             patch.object(gateway, "_config_raw", lambda s, o: ai_cfg.get((s, o))), \
             patch.object(gateway, "_max_response_bytes", lambda: 800), \
             patch("urllib.request.urlopen", return_value=resp):
            message_processing.process_message(
                sender_id=1, message="APIREQ|r1|!user|r|ai\x1fwhat color is the sky",
                interface=iface, is_sync_message=True, sender_node_id="!user",
            )
            _drain_threads()
        status, body = _join_apiresp(iface.sent_texts, "r1")
        self.assertEqual(status, "200")
        self.assertIn("sky is blue", body)

    def test_unauthorized_requester_rejected(self):
        iface = _Iface(allowed=["!someone_else"])
        with patch.object(gateway, "is_gateway_enabled", lambda: True):
            message_processing.process_message(
                sender_id=1, message="APIREQ|r2|!user|r|ai\x1fhi",
                interface=iface, is_sync_message=True, sender_node_id="!user",
            )
            _drain_threads()
        status, body = _join_apiresp(iface.sent_texts, "r2")
        self.assertEqual(status, "ERR")
        self.assertIn("not authorized", body)

    def test_disabled_gateway_ignores_apireq(self):
        iface = _Iface(allowed=["!user"])
        with patch.object(gateway, "is_gateway_enabled", lambda: False):
            message_processing.process_message(
                sender_id=1, message="APIREQ|r3|!user|r|ai\x1fhi",
                interface=iface, is_sync_message=True, sender_node_id="!user",
            )
            _drain_threads()
        self.assertEqual(iface.sent_texts, [])


class RequesterReassemblyTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        message_processing._apigw_response_buffers.clear()
        utils._apigw_pending.clear()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_single_packet_response_delivered_to_user(self):
        iface = _Iface()
        utils.register_api_request("rA", 42)  # user node 42 is waiting
        body = "short answer"
        message_processing.process_message(
            sender_id=1, message=f"APIRESP|rA|200|{len(body)}|{body}",
            interface=iface, is_sync_message=True, sender_node_id="!gw",
        )
        self.assertTrue(any(d == 42 and "short answer" in t for d, t in iface.sent_texts))
        self.assertIsNone(utils.pop_api_request("rA"))  # consumed

    def test_chunked_response_reassembled(self):
        iface = _Iface()
        utils.register_api_request("rB", 7)
        # Simulate a 3-part response: header(0..5) + META + CONT@5 + CONT@10
        message_processing.process_message(sender_id=1, message="APIRESP|rB|200|15|AAAAA",
                                           interface=iface, is_sync_message=True, sender_node_id="!gw")
        message_processing.process_message(sender_id=1, message="APIRESPMETA|rB|15",
                                           interface=iface, is_sync_message=True, sender_node_id="!gw")
        message_processing.process_message(sender_id=1, message="APIRESPCONT|rB|5|BBBBB",
                                           interface=iface, is_sync_message=True, sender_node_id="!gw")
        self.assertFalse(any(d == 7 for d, _ in iface.sent_texts))  # not complete yet
        message_processing.process_message(sender_id=1, message="APIRESPCONT|rB|10|CCCCC",
                                           interface=iface, is_sync_message=True, sender_node_id="!gw")
        delivered = [t for d, t in iface.sent_texts if d == 7]
        self.assertTrue(delivered)
        self.assertIn("AAAAABBBBBCCCCC", delivered[-1])

    def test_error_status_prefixed(self):
        iface = _Iface()
        utils.register_api_request("rC", 9)
        body = "host not in allow-list"
        message_processing.process_message(
            sender_id=1, message=f"APIRESP|rC|ERR|{len(body)}|{body}",
            interface=iface, is_sync_message=True, sender_node_id="!gw",
        )
        self.assertTrue(any(d == 9 and t.startswith("[ERR]") for d, t in iface.sent_texts))

    def test_response_for_unknown_rid_dropped(self):
        iface = _Iface()
        body = "noone waiting"
        message_processing.process_message(
            sender_id=1, message=f"APIRESP|rZ|200|{len(body)}|{body}",
            interface=iface, is_sync_message=True, sender_node_id="!gw",
        )
        self.assertEqual(iface.sent_texts, [])


class TimeoutTests(unittest.TestCase):
    def test_expire_api_requests(self):
        utils._apigw_pending.clear()
        utils.register_api_request("old", 1)
        utils._apigw_pending["old"]["created_at"] -= 1000  # force stale
        utils.register_api_request("new", 2)
        expired = utils.expire_api_requests(120)
        self.assertEqual(expired, [("old", 1)])
        self.assertIsNotNone(utils.pop_api_request("new"))


if __name__ == "__main__":
    unittest.main()
