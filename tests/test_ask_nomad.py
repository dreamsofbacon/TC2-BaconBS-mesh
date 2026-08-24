"""Tests for the Ask Nomad homescreen shortcut and post-reply follow-up:
- Main-menu 'N' shortcut jumps straight to the Project Nomad question
  prompt, skipping Utilities > API Gateway > [1] Ask Project Nomad.
- After a reply (local-gateway fast path OR mesh-relay path), the user can
  immediately ask another question or send 0/x to return to the MAIN menu
  specifically (not Utilities, which the shared API-gateway HTTP flow still
  uses).
"""
import os
import sqlite3
import sys
import threading
import time
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
        # The answer and the invitation must share one packet -- a separate
        # second DM races the first one's relay traffic and loses. The
        # "asked shortly" ack only exists on the slow-answer path (see
        # ApigwSlowAckTests).
        answer = [m for m in messages if "42" in m]
        self.assertEqual(len(answer), 1, f"answer split across packets: {messages}")
        self.assertIn("another question", answer[0])
        self.assertEqual(command_handlers.get_user_state(111), {"command": "ASK_NOMAD", "step": 1})

    def test_http_reply_shows_no_follow_up_prompt(self):
        with mock.patch("gateway.is_gateway_enabled", return_value=True), \
             mock.patch("gateway.handle_apireq") as fake_dispatch, \
             mock.patch.object(command_handlers, "send_message") as sm:
            fake_dispatch.side_effect = lambda rid, node_id, kind, payload, allowed, reply_fn: reply_fn("200", "page contents")
            command_handlers._apigw_submit(111, self.interface, "h", "GET\x1fhttp://x\x1f", "HTTP")
        messages = _sent(sm)
        self.assertFalse(any("another question" in m for m in messages))


class ApigwSlowAckTests(unittest.TestCase):
    """The 'Asked … reply will arrive shortly' ack must only go out when the
    answer is genuinely slow. Sending it first put a warm answer (arriving a
    second later) on the air while the ack -- a reliable multi-hop DM -- was
    still being relayed; they collided and the answer was lost every time
    the model was warm, which is every question except the first after boot.
    """

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

    def _submit(self, sm_side_effect=None):
        captured = {}

        def fake_dispatch(rid, node_id, kind, payload, allowed, reply_fn):
            captured["reply_fn"] = reply_fn

        patcher = mock.patch.object(command_handlers, "send_message",
                                    side_effect=sm_side_effect)
        sm = patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch("gateway.is_gateway_enabled", return_value=True), \
             mock.patch("gateway.handle_apireq", side_effect=fake_dispatch):
            command_handlers._apigw_submit(
                111, self.interface, "r", "ai\x1fq", "Project Nomad")
        return captured["reply_fn"], sm

    def test_fast_answer_sends_no_asked_ack(self):
        reply_fn, sm = self._submit()
        reply_fn("200", "42")
        messages = _sent(sm)
        self.assertFalse(any("reply will arrive shortly" in m for m in messages),
                         f"ack sent despite a fast answer: {messages}")
        self.assertTrue(any("42" in m for m in messages))

    def test_slow_answer_sends_the_ack_then_waits_before_the_answer(self):
        ack_sent = threading.Event()

        def record(message, *a, **kw):
            if "reply will arrive shortly" in message:
                ack_sent.set()
            return True

        slept = []
        with mock.patch.object(command_handlers, "APIGW_SLOW_ACK_SECONDS", 0.05), \
             mock.patch.object(command_handlers, "APIGW_ACK_CLEAR_SECONDS", 30.0):
            reply_fn, sm = self._submit(sm_side_effect=record)
            self.assertTrue(ack_sent.wait(timeout=5), "slow ack never fired")
            with mock.patch.object(command_handlers.time, "sleep",
                                   side_effect=lambda s: slept.append(s)):
                reply_fn("200", "42")
        messages = _sent(sm)
        self.assertTrue(any("reply will arrive shortly" in m for m in messages))
        self.assertTrue(any("42" in m for m in messages))
        self.assertTrue(slept and slept[0] > 0,
                        "answer was not held clear of the ack's relay traffic")

    def test_ack_never_follows_an_already_delivered_answer(self):
        """If the answer wins the race at the timer boundary, the timer must
        not fire a late ack behind it (that is just the collision reversed)."""
        with mock.patch.object(command_handlers, "APIGW_SLOW_ACK_SECONDS", 0.05):
            reply_fn, sm = self._submit()
            reply_fn("200", "42")
            time.sleep(0.2)  # give a not-yet-cancelled timer time to misfire
        messages = _sent(sm)
        self.assertFalse(any("reply will arrive shortly" in m for m in messages))


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

    def test_ai_response_over_mesh_arrives_as_one_message(self):
        """Answer and invitation must share a packet: two DMs two seconds
        apart race the first one's relay traffic on a multi-hop mesh, and
        the second loses. The AI path delegates to command_handlers, so the
        send happens there rather than in message_processing."""
        utils.register_api_request("rid1", 111, gateway_node_id="!gw", kind="r")
        with mock.patch.object(command_handlers, "send_message") as sm:
            message_processing.process_message(
                111, "APIRESP|rid1|200|2|42", self.interface,
                is_sync_message=True, sender_node_id="!gw",
            )
        messages = _sent(sm)
        self.assertEqual(len(messages), 1, f"expected one packet, got {messages}")
        self.assertIn("42", messages[0])
        self.assertIn("another question", messages[0])
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

    def test_an_empty_body_reports_failure_instead_of_sending_nothing(self):
        """_split_into_chunks yields nothing for a blank body, so the send
        loop never runs: no message, no exception, and the caller told it
        worked. That is how an empty AI answer became silence."""
        iface = self._LiveIface()
        for body in ("", "   ", "\n\n"):
            with self.subTest(body=repr(body)):
                with mock.patch.object(utils.time, "sleep"), \
                     self.assertLogs(level="WARNING") as logs:
                    ok = utils.send_message(body, 111, iface)
                self.assertFalse(ok)
                self.assertIn("empty body", " ".join(logs.output))
        self.assertEqual(iface.sent, [])


class ConcurrentSendSerializationTests(unittest.TestCase):
    """meshtastic-python's _sendToRadio is a retransmit queue, not a write:
    it mutates a shared self.queue while draining it and decrements
    queueStatus.free via _queueClaim(). Two threads inside it pop each
    other's packets and over-claim queue slots, and free only refills from
    the radio's QueueStatus messages -- so once it goes wrong every later
    send parks in time.sleep(0.5) forever, silently, until a restart.

    The API gateway answering on a worker thread is the one place this
    project has two threads at the radio at once.
    """

    class _OverlapDetectingIface:
        protocol_name = "Meshtastic"
        max_text_bytes = 220
        nodes = {}

        def __init__(self):
            self.inside = 0
            self.max_concurrent = 0
            self.sent = []
            self._guard = threading.Lock()

        def sendText(self, text, destinationId, wantAck, wantResponse):
            with self._guard:
                self.inside += 1
                self.max_concurrent = max(self.max_concurrent, self.inside)
            try:
                time.sleep(0.01)  # widen the window a real radio write has
                self.sent.append(text)
                return types.SimpleNamespace(id=len(self.sent))
            finally:
                with self._guard:
                    self.inside -= 1

    def test_concurrent_senders_never_overlap_in_sendtext(self):
        iface = self._OverlapDetectingIface()

        def worker(n):
            with mock.patch.object(utils.time, "sleep"):
                utils.send_message(f"message {n}", 111, iface)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(iface.max_concurrent, 1,
                         "two threads were inside sendText at once")
        self.assertEqual(len(iface.sent), 6, "a message was lost")

    def test_each_interface_serializes_independently(self):
        """One radio stalling must not block sends on the other."""
        a, b = self._OverlapDetectingIface(), self._OverlapDetectingIface()
        self.assertIsNot(utils.interface_send_lock(a), utils.interface_send_lock(b))

    def test_the_lock_is_stable_across_calls(self):
        iface = self._OverlapDetectingIface()
        self.assertIs(utils.interface_send_lock(iface), utils.interface_send_lock(iface))


class UserMessagePacingTests(unittest.TestCase):
    """Consecutive DMs to one node must not race each other's relay traffic."""

    class _Lora:
        protocol_name = "Meshtastic"

    class _LowLatency:
        protocol_name = "MQTT"
        is_low_latency = True

    def setUp(self):
        self._saved = os.environ.pop("BBS_USER_MESSAGE_PAUSE_SECONDS", None)
        utils._user_pause_cache = None  # the value is cached for a few seconds

    def tearDown(self):
        os.environ.pop("BBS_USER_MESSAGE_PAUSE_SECONDS", None)
        utils._user_pause_cache = None
        if self._saved is not None:
            os.environ["BBS_USER_MESSAGE_PAUSE_SECONDS"] = self._saved

    def test_lora_keeps_a_gap_by_default(self):
        self.assertEqual(utils.get_user_message_pause_seconds(self._Lora()), 2.0)

    def test_low_latency_transport_needs_no_gap(self):
        self.assertEqual(utils.get_user_message_pause_seconds(self._LowLatency()), 0.0)

    def test_the_gap_is_tunable_for_a_slow_mesh(self):
        os.environ["BBS_USER_MESSAGE_PAUSE_SECONDS"] = "5"
        self.assertEqual(utils.get_user_message_pause_seconds(self._Lora()), 5.0)

    def test_a_bad_value_falls_back_rather_than_crashing_every_send(self):
        os.environ["BBS_USER_MESSAGE_PAUSE_SECONDS"] = "not-a-number"
        self.assertEqual(utils.get_user_message_pause_seconds(self._Lora()), 2.0)

    def test_sync_turbo_does_not_remove_the_gap(self):
        """sync_turbo speeds SYNC pacing; the radio's airtime limits are
        unchanged, and turbo nodes are the busiest ones."""
        with mock.patch.object(utils, "_is_sync_turbo_enabled", return_value=True):
            self.assertEqual(utils.get_user_message_pause_seconds(self._Lora()), 2.0)
