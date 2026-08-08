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
import command_handlers
import utils


class _Iface:
    def __init__(self, allowed=None, bbs_nodes=None, max_text_bytes=220):
        self.sent_texts = []
        self.bbs_nodes = bbs_nodes or []
        self.allowed_nodes = allowed or []
        self.max_text_bytes = max_text_bytes

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

    def test_http_response_cap_is_exact_and_utf8_safe(self):
        resp = io.BytesIO(("🙂" * 100).encode('utf-8'))
        with patch.object(gateway, "_max_response_bytes", lambda: 64):
            body = gateway._read_capped(resp)
        self.assertLessEqual(len(body.encode('utf-8')), 64)
        self.assertTrue(body.endswith("…"))
        self.assertNotIn("�", body)

    def test_auth_open_when_no_lists(self):
        with patch.object(gateway, "gateway_allowed_nodes", lambda: []):
            self.assertTrue(gateway.is_requester_authorized("!anyone", None))
            self.assertTrue(gateway.is_requester_authorized("!anyone", []))

    def test_auth_falls_back_to_general_allowlist(self):
        with patch.object(gateway, "gateway_allowed_nodes", lambda: []):
            self.assertTrue(gateway.is_requester_authorized("!u", ["!u", "!v"]))
            self.assertFalse(gateway.is_requester_authorized("!x", ["!u", "!v"]))

    def test_auth_gateway_list_overrides_general(self):
        # When the gateway-specific list is set it is authoritative: a node in the
        # general allow-list but NOT the gateway list is rejected, and vice versa.
        with patch.object(gateway, "gateway_allowed_nodes", lambda: ["!hand"]):
            self.assertTrue(gateway.is_requester_authorized("!hand", []))
            self.assertTrue(gateway.is_requester_authorized("!hand", ["!other"]))
            self.assertFalse(gateway.is_requester_authorized("!other", ["!other"]))


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

    def test_ai_relay_nomad_dialect_uses_ollama_path(self):
        iface = _Iface(allowed=["!user"])
        ai_cfg = {
            ('gateway', 'ai_base_url'): 'https://ai.bmcse.com',
            ('gateway', 'ai_dialect'): 'nomad',
            ('gateway', 'ai_model'): 'qwen2.5:3b',
            ('gateway', 'ai_system_prompt'): '',
        }
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url
            resp = io.BytesIO(json.dumps({"message": {"content": "Hello there!"}, "done": True}).encode())
            resp.__enter__ = lambda *_: resp
            resp.__exit__ = lambda *_: False
            return resp

        with patch.object(gateway, "is_gateway_enabled", lambda: True), \
             patch.object(gateway, "_config_raw", lambda s, o: ai_cfg.get((s, o))), \
             patch.object(gateway, "_max_response_bytes", lambda: 800), \
             patch("urllib.request.urlopen", _fake_urlopen):
            message_processing.process_message(
                sender_id=1, message="APIREQ|rn|!user|r|ai\x1fhi",
                interface=iface, is_sync_message=True, sender_node_id="!user",
            )
            _drain_threads()
        self.assertEqual(captured['url'], "https://ai.bmcse.com/api/ollama/chat/")
        status, body = _join_apiresp(iface.sent_texts, "rn")
        self.assertEqual(status, "200")
        self.assertIn("Hello there", body)

    def test_nomad_reply_uses_active_transport_single_message_budget(self):
        iface = _Iface(allowed=["!user"], max_text_bytes=160)
        ai_cfg = {
            ('gateway', 'ai_base_url'): 'https://ai.bmcse.com',
            ('gateway', 'ai_dialect'): 'nomad',
            ('gateway', 'ai_model'): 'gemma4:12b',
            ('gateway', 'ai_system_prompt'): 'Be accurate and practical.',
        }
        essential = (
            "A solar flare can disrupt radio propagation; keep transmissions "
            "brief and retry later."
        )
        long_reply = essential + " " + ("Extra context 🙂 should be omitted. " * 20)
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured['payload'] = json.loads(req.data.decode('utf-8'))
            resp = io.BytesIO(json.dumps({
                "message": {"content": long_reply}, "done": True,
            }).encode())
            resp.__enter__ = lambda *_: resp
            resp.__exit__ = lambda *_: False
            return resp

        with patch.object(gateway, "is_gateway_enabled", lambda: True), \
             patch.object(gateway, "_config_raw", lambda s, o: ai_cfg.get((s, o))), \
             patch.object(gateway, "_max_response_bytes", lambda: 1200), \
             patch.object(gateway, "_nomad_single_message_enabled", lambda: True), \
             patch.object(gateway, "_nomad_max_characters", lambda: 150), \
             patch("urllib.request.urlopen", _fake_urlopen):
            message_processing.process_message(
                sender_id=1, message="APIREQ|nb|!user|r|ai\x1fspace weather",
                interface=iface, is_sync_message=True, sender_node_id="!user",
            )
            _drain_threads()

        messages = captured['payload']['messages']
        self.assertEqual(messages[0]['content'], 'Be accurate and practical.')
        self.assertIn('150 characters', messages[1]['content'])
        self.assertIn('160 UTF-8 bytes', messages[1]['content'])
        self.assertIn('Emojis are welcome', messages[1]['content'])
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'space weather'})

        status, body = _join_apiresp(iface.sent_texts, "nb")
        self.assertEqual(status, "200")
        self.assertLessEqual(len(body), 150)
        self.assertLessEqual(len(body.encode('utf-8')), 160)
        self.assertTrue(body.startswith(essential))
        self.assertTrue(body.endswith("…"))

    def test_nomad_single_message_mode_can_be_disabled(self):
        ai_cfg = {
            ('gateway', 'ai_base_url'): 'https://ai.bmcse.com',
            ('gateway', 'ai_dialect'): 'nomad',
            ('gateway', 'ai_model'): 'gemma4:12b',
            ('gateway', 'ai_system_prompt'): '',
        }
        long_reply = "x" * 300
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured['payload'] = json.loads(req.data.decode('utf-8'))
            resp = io.BytesIO(json.dumps({
                "message": {"content": long_reply}, "done": True,
            }).encode())
            resp.__enter__ = lambda *_: resp
            resp.__exit__ = lambda *_: False
            return resp

        with patch.object(gateway, "_config_raw", lambda s, o: ai_cfg.get((s, o))), \
             patch.object(gateway, "_max_response_bytes", lambda: 800), \
             patch.object(gateway, "_nomad_single_message_enabled", lambda: False), \
             patch("urllib.request.urlopen", _fake_urlopen):
            status, body = gateway.perform_ai_chat("expand", response_max_bytes=160)

        self.assertEqual(status, "200")
        self.assertEqual(body, long_reply)
        self.assertEqual(captured['payload']['messages'], [
            {'role': 'user', 'content': 'expand'},
        ])

    def test_reply_budget_clipping_is_utf8_safe_and_includes_suffix(self):
        reply = gateway._fit_reply_to_budget(
            "🙂" * 100, 160, max_characters=150,
        )
        self.assertLessEqual(len(reply), 150)
        self.assertLessEqual(len(reply.encode('utf-8')), 160)
        self.assertTrue(reply.endswith("…"))
        self.assertNotIn("�", reply)

        ascii_reply = gateway._fit_reply_to_budget(
            "plain words " * 100, 220, max_characters=150,
        )
        self.assertLessEqual(len(ascii_reply), 150)
        self.assertLessEqual(len(ascii_reply.encode('utf-8')), 220)
        self.assertTrue(ascii_reply.endswith("…"))

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


class LocalGatewaySubmissionTests(unittest.TestCase):
    def test_local_nomad_submission_passes_transport_budget(self):
        iface = _Iface(allowed=["!user"], max_text_bytes=160)
        captured = {}

        def _fake_handle(rid, requester_id, kind, payload, allowed, reply_fn,
                         response_max_bytes=None):
            captured.update({
                'requester_id': requester_id,
                'kind': kind,
                'payload': payload,
                'response_max_bytes': response_max_bytes,
            })
            reply_fn("200", "short answer")

        with patch.object(gateway, "is_gateway_enabled", lambda: True), \
             patch.object(gateway, "handle_apireq", _fake_handle), \
             patch.object(command_handlers, "get_node_id_from_num", lambda *_: "!user"), \
             patch.object(command_handlers, "send_message") as send_message_mock, \
             patch.object(command_handlers, "update_user_state"):
            command_handlers._apigw_submit(
                sender_id=1,
                interface=iface,
                kind='r',
                payload="ai\x1fquestion",
                label="Project Nomad",
            )

        self.assertEqual(captured['requester_id'], "!user")
        self.assertEqual(captured['kind'], "r")
        self.assertEqual(captured['payload'], "ai\x1fquestion")
        self.assertEqual(captured['response_max_bytes'], 160)
        send_message_mock.assert_any_call("short answer", 1, iface)


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


class GapFillTests(unittest.TestCase):
    def setUp(self):
        message_processing._apigw_response_buffers.clear()
        utils._apigw_pending.clear()
        utils._apigw_sent.clear()

    def test_cap_apigf_advertised_with_gateway(self):
        with patch.object(utils, "_config_bool", lambda s, o, d: True):
            self.assertIn("apigf", utils.local_capabilities_token())
        with patch.object(utils, "_config_bool", lambda s, o, d: False):
            self.assertNotIn("apigf", utils.local_capabilities_token())

    def test_compute_response_gaps(self):
        f = message_processing._compute_response_gaps
        # offset 0 chunk "AAAAA" (0-5), missing 5-10, chunk at 10 "CCCCC" (10-15), expected 20
        gaps = f({0: "AAAAA", 10: "CCCCC"}, 20)
        self.assertEqual(gaps, [(5, 10), (15, 20)])
        # no gaps
        self.assertEqual(f({0: "AAAAA", 5: "BBBBB"}, 10), [])
        # nothing received
        self.assertEqual(f({}, 12), [(0, 12)])

    def test_parse_gap_ranges(self):
        f = utils._parse_gap_ranges
        self.assertEqual(f("*", 20), [(0, 20)])
        self.assertEqual(f("", 20), [(0, 20)])
        self.assertEqual(f("5-10,15-20", 20), [(5, 10), (15, 20)])
        self.assertEqual(f("18-99", 20), [(18, 20)])  # clamped

    def test_resend_ranges_from_retained(self):
        iface = _Iface()
        utils._retain_sent_api_response("rR", "200", "AAAAABBBBBCCCCC", "!user")
        ok = utils.resend_api_response_ranges("rR", "5-10", iface)
        self.assertTrue(ok)
        # should emit a CONT at offset 5 with "BBBBB" plus a META
        conts = [t for _, t in iface.sent_texts if t.startswith("APIRESPCONT|rR|5|")]
        self.assertTrue(any("BBBBB" in t for t in conts))
        self.assertTrue(any(t == "APIRESPMETA|rR|15" for _, t in iface.sent_texts))

    def test_resend_offset_zero_resends_header(self):
        iface = _Iface()
        utils._retain_sent_api_response("rH", "200", "HELLOWORLD", "!user")
        utils.resend_api_response_ranges("rH", "*", iface)
        self.assertTrue(any(t.startswith("APIRESP|rH|200|10|HELLOWORLD")
                            for _, t in iface.sent_texts))

    def test_resend_unknown_rid_returns_false(self):
        iface = _Iface()
        self.assertFalse(utils.resend_api_response_ranges("nope", "*", iface))

    def test_gap_request_sweep_targets_apigf_gateway(self):
        iface = _Iface()
        # Requester is waiting on rid rG via gateway !gw; got header+tail but a hole.
        utils.register_api_request("rG", 99, gateway_node_id="!gw")
        message_processing._apigw_apply_chunk("rG", 0, "AAAAA", status="200",
                                              expected=15, source="!gw")
        message_processing._apigw_apply_chunk("rG", 10, "CCCCC", source="!gw")
        # Age the buffer past max_age.
        message_processing._apigw_response_buffers["rG"]['created_at'] -= 100
        with patch("db_operations.peer_supports", lambda p, c: c == "apigf"):
            sent = message_processing.request_pending_api_gaps(iface)
        self.assertEqual(sent, 1)
        gap_frames = [t for d, t in iface.sent_texts if d == "!gw" and t.startswith("APIRESPGAP|rG|")]
        self.assertTrue(gap_frames)
        self.assertIn("5-10", gap_frames[0])

    def test_gap_sweep_skips_non_apigf_gateway(self):
        iface = _Iface()
        utils.register_api_request("rN", 1, gateway_node_id="!old")
        message_processing._apigw_apply_chunk("rN", 0, "AA", status="200",
                                              expected=10, source="!old")
        message_processing._apigw_response_buffers["rN"]['created_at'] -= 100
        with patch("db_operations.peer_supports", lambda p, c: False):
            sent = message_processing.request_pending_api_gaps(iface)
        self.assertEqual(sent, 0)
        self.assertEqual(iface.sent_texts, [])

    def test_no_premature_gap_while_gateway_still_working(self):
        """With no response yet, a request aged past the short partial-fill
        timeout but within no_response_age must NOT emit a full resend (the
        gateway may still be computing, e.g. an AI call)."""
        iface = _Iface()
        utils.register_api_request("rW", 5, gateway_node_id="!gw")
        # Age 20s: > max_age (12) but < no_response_age (45).
        utils._apigw_pending["rW"]['created_at'] -= 20
        with patch("db_operations.peer_supports", lambda p, c: c == "apigf"):
            sent = message_processing.request_pending_api_gaps(iface)
        self.assertEqual(sent, 0)
        self.assertEqual(iface.sent_texts, [])
        # Age past no_response_age → now it asks for a full resend.
        utils._apigw_pending["rW"]['created_at'] -= 40  # total 60s
        with patch("db_operations.peer_supports", lambda p, c: c == "apigf"):
            sent = message_processing.request_pending_api_gaps(iface)
        self.assertEqual(sent, 1)
        self.assertTrue(any(t == "APIRESPGAP|rW|*" for _, t in iface.sent_texts))

    def test_partial_response_hole_uses_short_timeout(self):
        """A *partial* response with a hole is refilled at the short max_age,
        not the long no_response_age."""
        iface = _Iface()
        utils.register_api_request("rPart", 6, gateway_node_id="!gw")
        message_processing._apigw_apply_chunk("rPart", 0, "AAAAA", status="200",
                                              expected=15, source="!gw")
        message_processing._apigw_apply_chunk("rPart", 10, "CCCCC", source="!gw")
        # Age 20s: past max_age (12) though well under no_response_age (45).
        message_processing._apigw_response_buffers["rPart"]['created_at'] -= 20
        with patch("db_operations.peer_supports", lambda p, c: c == "apigf"):
            sent = message_processing.request_pending_api_gaps(iface)
        self.assertEqual(sent, 1)
        self.assertTrue(any("5-10" in t for _, t in iface.sent_texts))

    def test_end_to_end_gap_recovery(self):
        """Drop the middle chunk; APIRESPGAP refill completes the delivery."""
        gw = _Iface()           # gateway node
        req = _Iface()          # requester node
        # Gateway sends a 3-part response but we only deliver header + tail to req.
        utils.register_api_request("rE", 55, gateway_node_id="!gw")
        utils._retain_sent_api_response("rE", "200", "AAAAABBBBBCCCCC", "!req")
        # Requester receives header (0-5) and tail (10-15) — middle lost.
        message_processing.process_message(sender_id=1, message="APIRESP|rE|200|15|AAAAA",
                                           interface=req, is_sync_message=True, sender_node_id="!gw")
        message_processing.process_message(sender_id=1, message="APIRESPCONT|rE|10|CCCCC",
                                           interface=req, is_sync_message=True, sender_node_id="!gw")
        self.assertFalse(any(d == 55 for d, _ in req.sent_texts))  # not delivered yet
        # Age + sweep → requester asks gateway to refill 5-10.
        message_processing._apigw_response_buffers["rE"]['created_at'] -= 100
        with patch("db_operations.peer_supports", lambda p, c: c == "apigf"):
            message_processing.request_pending_api_gaps(req)
        gap = [t for d, t in req.sent_texts if t.startswith("APIRESPGAP|rE|")][0]
        spec = gap.split("|", 2)[2]
        # Gateway handles the refill against its retained copy.
        message_processing.process_message(sender_id=2, message=f"APIRESPGAP|rE|{spec}",
                                           interface=gw, is_sync_message=True, sender_node_id="!req")
        # Feed the gateway's refilled frames back into the requester.
        for _dest, t in gw.sent_texts:
            message_processing.process_message(sender_id=1, message=t,
                                               interface=req, is_sync_message=True, sender_node_id="!gw")
        delivered = [t for d, t in req.sent_texts if d == 55]
        self.assertTrue(delivered)
        self.assertIn("AAAAABBBBBCCCCC", delivered[-1])


if __name__ == "__main__":
    unittest.main()
