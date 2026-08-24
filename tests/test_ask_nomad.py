"""Tests for the Ask Nomad homescreen shortcut and post-reply follow-up:
- Main-menu 'N' shortcut jumps straight to the Project Nomad question
  prompt, skipping Utilities > API Gateway > [1] Ask Project Nomad.
- After a reply (local-gateway fast path OR mesh-relay path), the user can
  immediately ask another question or send 0/x to return to the MAIN menu
  specifically (not Utilities, which the shared API-gateway HTTP flow still
  uses).
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
import command_handlers
import message_processing
import utils


class _Iface:
    def __init__(self, allowed_nodes=None, nodes=None):
        self.sent_texts = []
        self.bbs_nodes = []
        self.allowed_nodes = allowed_nodes or []
        self.nodes = nodes or {"!aaa11111": {"num": 111}}

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append((destinationId, text))


def _sent(mock_send_message):
    return [call.args[0] for call in mock_send_message.call_args_list]


class AskNomadShortcutTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        command_handlers.update_user_state(111, None)
        self.interface = _Iface()

    def tearDown(self):
        command_handlers.update_user_state(111, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_authorized_node_goes_straight_to_prompt(self):
        with mock.patch.object(command_handlers, "_apigw_authorized", return_value=True), \
             mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers.handle_ask_nomad_command(111, self.interface)
        self.assertIn("Type your question for Project Nomad:", _sent(sm))
        self.assertEqual(command_handlers.get_user_state(111), {"command": "ASK_NOMAD", "step": 1})

    def test_unauthorized_node_rejected_to_main_menu(self):
        with mock.patch.object(command_handlers, "_apigw_authorized", return_value=False), \
             mock.patch.object(command_handlers, "send_message") as sm, \
             mock.patch.object(command_handlers, "handle_help_command") as hh:
            command_handlers.handle_ask_nomad_command(111, self.interface)
        self.assertTrue(any("not on the allow-list" in m for m in _sent(sm)))
        hh.assert_called_once_with(111, self.interface)  # no menu_name -> main menu

    def test_main_menu_letter_n_routes_to_ask_nomad(self):
        with mock.patch.object(command_handlers, "_apigw_authorized", return_value=True), \
             mock.patch.object(command_handlers, "send_message") as sm:
            message_processing.process_message(
                111, "n", self.interface, is_sync_message=False, sender_node_id="!aaa11111",
            )
        self.assertIn("Type your question for Project Nomad:", _sent(sm))
        self.assertEqual(command_handlers.get_user_state(111), {"command": "ASK_NOMAD", "step": 1})

    def test_exit_choices_return_to_main_menu_not_utilities(self):
        command_handlers.update_user_state(111, {"command": "ASK_NOMAD", "step": 1})
        for choice in ("0", "x", "exit"):
            with mock.patch.object(command_handlers, "handle_help_command") as hh:
                command_handlers.handle_ask_nomad_steps(111, choice, self.interface)
            hh.assert_called_once_with(111, self.interface)  # no menu_name -> main menu

    def test_empty_question_cancels_to_main_menu(self):
        with mock.patch.object(command_handlers, "send_message") as sm, \
             mock.patch.object(command_handlers, "handle_help_command") as hh:
            command_handlers.handle_ask_nomad_steps(111, "   ", self.interface)
        self.assertTrue(any("cancelled" in m for m in _sent(sm)))
        hh.assert_called_once_with(111, self.interface)

    def test_question_text_is_submitted_to_apigw(self):
        with mock.patch.object(command_handlers, "_apigw_submit") as submit:
            command_handlers.handle_ask_nomad_steps(111, "what time is it", self.interface)
        submit.assert_called_once()
        args = submit.call_args.args
        self.assertEqual(args[0], 111)
        self.assertEqual(args[1], self.interface)
        self.assertEqual(args[2], "r")
        self.assertIn("what time is it", args[3])
        self.assertEqual(args[4], "Project Nomad")


class AskNomadFollowUpLocalGatewayTests(unittest.TestCase):
    """Local-gateway fast path: _apigw_submit's own _reply callback."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        command_handlers.update_user_state(111, None)
        self.interface = _Iface()

    def tearDown(self):
        command_handlers.update_user_state(111, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_ai_reply_shows_follow_up_prompt(self):
        # gateway.handle_apireq dispatches on a worker thread in production
        # and returns immediately -- the reply_fn callback fires LATER, well
        # after _apigw_submit's own update_user_state(None) already ran.
        # Capture reply_fn and invoke it after _apigw_submit returns, rather
        # than calling it synchronously inline, to faithfully reproduce that
        # ordering instead of racing a real background thread.
        captured = {}

        def fake_dispatch(rid, node_id, kind, payload, allowed, reply_fn):
            captured["reply_fn"] = reply_fn

        with mock.patch("gateway.is_gateway_enabled", return_value=True), \
             mock.patch("gateway.handle_apireq", side_effect=fake_dispatch), \
             mock.patch.object(command_handlers, "send_message") as sm:
            command_handlers._apigw_submit(111, self.interface, "r", "ai\x1fwhat is the answer", "Project Nomad")
            captured["reply_fn"]("200", "42")
        messages = _sent(sm)
        self.assertTrue(any("42" in m for m in messages))
        self.assertTrue(any("another question" in m for m in messages))
        self.assertEqual(command_handlers.get_user_state(111), {"command": "ASK_NOMAD", "step": 1})

    def test_http_reply_shows_no_follow_up_prompt(self):
        with mock.patch("gateway.is_gateway_enabled", return_value=True), \
             mock.patch("gateway.handle_apireq") as fake_dispatch, \
             mock.patch.object(command_handlers, "send_message") as sm:
            fake_dispatch.side_effect = lambda rid, node_id, kind, payload, allowed, reply_fn: reply_fn("200", "page contents")
            command_handlers._apigw_submit(111, self.interface, "h", "GET\x1fhttp://x\x1f", "HTTP")
        messages = _sent(sm)
        self.assertFalse(any("another question" in m for m in messages))


class AskNomadFollowUpMeshRelayTests(unittest.TestCase):
    """Mesh-relay path: message_processing._deliver_api_response, reached via
    a real process_message() call carrying an APIRESP sync frame."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        utils._apigw_pending.clear()
        message_processing._apigw_response_buffers.clear()
        command_handlers.update_user_state(111, None)
        self.interface = _Iface()

    def tearDown(self):
        command_handlers.update_user_state(111, None)
        utils._apigw_pending.clear()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_ai_response_over_mesh_shows_follow_up(self):
        utils.register_api_request("rid1", 111, gateway_node_id="!gw", kind="r")
        with mock.patch.object(message_processing, "send_message") as sm:
            message_processing.process_message(
                111, "APIRESP|rid1|200|2|42", self.interface,
                is_sync_message=True, sender_node_id="!gw",
            )
        messages = _sent(sm)
        self.assertTrue(any("42" in m for m in messages))
        self.assertTrue(any("another question" in m for m in messages))
        self.assertEqual(command_handlers.get_user_state(111), {"command": "ASK_NOMAD", "step": 1})

    def test_http_response_over_mesh_shows_no_follow_up(self):
        utils.register_api_request("rid2", 111, gateway_node_id="!gw", kind="h")
        with mock.patch.object(message_processing, "send_message") as sm:
            message_processing.process_message(
                111, "APIRESP|rid2|200|4|page", self.interface,
                is_sync_message=True, sender_node_id="!gw",
            )
        messages = _sent(sm)
        self.assertFalse(any("another question" in m for m in messages))

    def test_legacy_request_with_no_kind_shows_no_follow_up(self):
        """A request registered before this feature existed (no kind stored)
        must not crash and must not show the follow-up."""
        utils.register_api_request("rid3", 111, gateway_node_id="!gw")
        with mock.patch.object(message_processing, "send_message") as sm:
            message_processing.process_message(
                111, "APIRESP|rid3|200|2|ok", self.interface,
                is_sync_message=True, sender_node_id="!gw",
            )
        messages = _sent(sm)
        self.assertTrue(any("ok" in m for m in messages))
        self.assertFalse(any("another question" in m for m in messages))


if __name__ == "__main__":
    unittest.main()


class AskNomadStaleInterfaceTests(unittest.TestCase):
    """The answer is sent from a worker thread up to the gateway's request
    timeout after the question. If the radio reconnects in that window,
    _reconnect_link closes the old interface and puts a NEW object on
    link.interface -- so anything holding the old one is writing to a
    closed port, and send_message swallows the failure into a log line.
    That is silence from the user's side, which is how this presents.
    """

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        command_handlers.update_user_state(111, None)

    def tearDown(self):
        command_handlers.update_user_state(111, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _submit_and_capture(self, link):
        captured = {}

        def fake_dispatch(rid, node_id, kind, payload, allowed, reply_fn):
            captured["reply_fn"] = reply_fn

        fake_server = types.SimpleNamespace(link_for_interface=lambda iface: link)
        with mock.patch("gateway.is_gateway_enabled", return_value=True), \
             mock.patch("gateway.handle_apireq", side_effect=fake_dispatch), \
             mock.patch.dict(sys.modules, {"server": fake_server}):
            command_handlers._apigw_submit(
                111, link.interface, "r", "ai\x1fwhat is the answer", "Project Nomad")
        return captured["reply_fn"]

    def test_reply_goes_to_the_links_current_interface(self):
        original = _Iface()
        link = types.SimpleNamespace(interface=original)
        reply_fn = self._submit_and_capture(link)

        # The radio reconnects before the AI answers.
        replacement = _Iface()
        link.interface = replacement

        reply_fn("200", "42")
        self.assertTrue(any("42" in text for _dest, text in replacement.sent_texts),
                        "answer was not delivered to the reconnected interface")
        self.assertFalse(any("42" in text for _dest, text in original.sent_texts),
                         "answer went to the closed interface")

    def test_falls_back_to_the_captured_interface_when_no_link_is_known(self):
        """Single-radio setups and tests never register a link; the reply
        must still go out rather than being dropped."""
        iface = _Iface()
        captured = {}

        def fake_dispatch(rid, node_id, kind, payload, allowed, reply_fn):
            captured["reply_fn"] = reply_fn

        fake_server = types.SimpleNamespace(link_for_interface=lambda i: None)
        with mock.patch("gateway.is_gateway_enabled", return_value=True), \
             mock.patch("gateway.handle_apireq", side_effect=fake_dispatch), \
             mock.patch.dict(sys.modules, {"server": fake_server}):
            command_handlers._apigw_submit(111, iface, "r", "ai\x1fq", "Project Nomad")
        captured["reply_fn"]("200", "42")
        self.assertTrue(any("42" in text for _dest, text in iface.sent_texts))

    def test_undeliverable_reply_is_logged_and_skips_the_follow_up(self):
        """Prompting for another question after the answer never arrived
        would tell the user everything worked."""
        link = types.SimpleNamespace(interface=_Iface())
        reply_fn = self._submit_and_capture(link)

        with mock.patch.object(command_handlers, "send_message", return_value=False), \
             self.assertLogs(level="WARNING") as logs:
            reply_fn("200", "42")
        joined = " ".join(logs.output)
        self.assertIn("could not", joined)
        self.assertIn("Project Nomad", joined)


class SendMessageDeliveryReportingTests(unittest.TestCase):
    """send_message used to swallow every failure into an INFO line with no
    context, so an async reply that never landed left nothing to go on."""

    class _DeadIface:
        protocol_name = "Meshtastic"
        max_text_bytes = 220
        nodes = {}

        def sendText(self, **kwargs):
            raise OSError("port is closed")

    class _LiveIface:
        protocol_name = "Meshtastic"
        max_text_bytes = 220
        nodes = {}

        def __init__(self):
            self.sent = []

        def sendText(self, text, destinationId, wantAck, wantResponse):
            self.sent.append(text)
            return types.SimpleNamespace(id=1)

    def test_returns_false_and_warns_when_the_send_fails(self):
        with mock.patch.object(utils.time, "sleep"), \
             self.assertLogs(level="WARNING") as logs:
            ok = utils.send_message("hello", 111, self._DeadIface())
        self.assertFalse(ok)
        joined = " ".join(logs.output)
        self.assertIn("REPLY SEND ERROR", joined)
        self.assertIn("Meshtastic", joined)  # which radio, not just "it failed"

    def test_returns_true_when_every_chunk_goes(self):
        iface = self._LiveIface()
        with mock.patch.object(utils.time, "sleep"):
            self.assertTrue(utils.send_message("hello", 111, iface))
        self.assertEqual(iface.sent, ["hello"])

    def test_a_partial_failure_still_reports_false(self):
        """One dropped chunk means the user did not get the whole answer."""
        iface = self._LiveIface()
        calls = {"n": 0}

        def flaky(text, destinationId, wantAck, wantResponse):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("write failed")
            iface.sent.append(text)
            return types.SimpleNamespace(id=calls["n"])

        iface.sendText = flaky
        long_text = ("word " * 200).strip()
        with mock.patch.object(utils.time, "sleep"), self.assertLogs(level="WARNING"):
            ok = utils.send_message(long_text, 111, iface)
        self.assertFalse(ok)
        self.assertGreater(calls["n"], 1)
