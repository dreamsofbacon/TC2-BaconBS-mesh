"""A reply that arrives after the command must reach the terminal on its own.

Ask Nomad hands the question to a worker thread and answers through the same
interface up to a minute later; Web Fetch does the same. On this transport
nothing drained that buffer except _send_to_bbs, which runs only when the
user types. So the answer sat there until the next keypress and was then
printed alongside the reply to whatever had just been typed -- and that
keypress was consumed by the prompt the answer had only now displayed.

A field test caught it from the user's side: a blank prompt, thirty seconds
of nothing, then typing 0 to nudge it produced the answer *and* fed the 0 to
the menu the answer had just drawn. The user loses their input and their
place, and both features look hung.

The web Emulator page never had this, because it polls. This is that poll,
for a terminal.
"""

import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import ssh_server


class _Channel:
    def __init__(self):
        self.written = []
        self.exited = None

    def write(self, text):
        self.written.append(text)

    def exit(self, code):
        self.exited = code

    def text(self):
        return "".join(self.written)


class _Session:
    """Stands in for bbs_emulator.EmulatorSession."""

    def __init__(self):
        self.pending = []
        self.sent = []
        self.token = "t"
        self.interface = types.SimpleNamespace(session_ended=False)

    def send(self, text):
        self.sent.append(text)
        return [], None

    def drain(self):
        out, self.pending = self.pending, []
        return out


def _session_under_test():
    client = ssh_server.BBSClientSession(
        auth=types.SimpleNamespace(account_id="a" * 32, alias="tester",
                                   registered=False),
        config=ssh_server.SSHConfig(),
        limiter=ssh_server.SessionLimiter(5, 1),
        source_address="203.0.113.9")
    client.channel = _Channel()
    client.session = _Session()
    client._auth_stage = "bbs"
    return client


class LateReplyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.client = _session_under_test()
        # The drain reschedules itself on the running loop; there is no loop
        # in a unit test, so pin that out and drive it by hand.
        patch = mock.patch.object(ssh_server.BBSClientSession, "_schedule_drain")
        self.schedule = patch.start()
        self.addCleanup(patch.stop)

    def test_a_late_answer_reaches_the_terminal_without_a_keypress(self):
        self.client.session.pending = [{"text": "Nomad says hello"}]
        self.client._drain_late_replies()
        self.assertIn("Nomad says hello", self.client.channel.text())

    def test_the_prompt_is_redrawn_after_a_late_answer(self):
        """Otherwise the user is left with an answer and no cursor."""
        self.client.session.pending = [{"text": "Nomad says hello"}]
        self.client._drain_late_replies()
        self.assertTrue(self.client.channel.text().endswith("> "))

    def test_nothing_is_written_when_there_is_nothing_waiting(self):
        self.client._drain_late_replies()
        self.assertEqual(self.client.channel.text(), "")

    def test_the_users_keypress_is_no_longer_consumed_by_the_answer(self):
        """The specific harm: the late answer used to surface only on the
        next keypress, and that keypress was then eaten by the prompt the
        answer had just drawn."""
        self.client.session.pending = [{"text": "Nomad says hello"}]
        self.client._drain_late_replies()
        self.client.channel.written.clear()
        self.client._send_to_bbs("0")
        self.assertEqual(self.client.session.sent, ["0"],
                         "the keypress reached the BBS as its own command")
        self.assertNotIn("Nomad says hello", self.client.channel.text())

    def test_newlines_are_terminal_line_endings(self):
        self.client.session.pending = [{"text": "line one\nline two"}]
        self.client._drain_late_replies()
        self.assertIn("line one\r\nline two", self.client.channel.text())

    def test_it_keeps_polling_while_the_session_is_open(self):
        self.client._drain_late_replies()
        self.schedule.assert_called_once()

    def test_it_stops_once_the_session_closes(self):
        self.client._closed = True
        self.client.session.pending = [{"text": "too late"}]
        self.client._drain_late_replies()
        self.assertEqual(self.client.channel.text(), "")
        self.schedule.assert_not_called()

    def test_it_does_not_write_over_a_login_prompt(self):
        """Before authentication the terminal belongs to the account prompt,
        and there is no BBS session to drain anyway."""
        self.client._auth_stage = "account_username"
        self.client.session.pending = [{"text": "should not appear"}]
        self.client._drain_late_replies()
        self.assertEqual(self.client.channel.text(), "")

    def test_a_dropped_connection_cleans_up_rather_than_raising(self):
        self.client.channel.write = mock.Mock(side_effect=BrokenPipeError)
        self.client.session.pending = [{"text": "anything"}]
        with mock.patch.object(self.client, "_cleanup") as cleanup:
            self.client._drain_late_replies()
        cleanup.assert_called_once()
        self.schedule.assert_not_called()

    def test_a_handler_error_does_not_stop_the_poll(self):
        """A broken drain must not silently end late delivery for the rest
        of the session."""
        self.client.session.drain = mock.Mock(side_effect=RuntimeError("boom"))
        self.client._drain_late_replies()
        self.schedule.assert_called_once()


class DrainSchedulingTests(unittest.TestCase):
    def test_starting_the_bbs_arms_the_poll(self):
        """The wiring, not just the drain. Every other test here calls
        _drain_late_replies directly, so all of them still pass if nothing
        ever schedules it -- which is exactly the bug being fixed."""
        client = _session_under_test()
        client.session = None
        patches = [
            mock.patch.object(ssh_server.bbs_emulator, "start_ssh_session",
                              return_value=_Session()),
            mock.patch.object(ssh_server.BBSClientSession, "_reset_idle_timer"),
            mock.patch.object(ssh_server.BBSClientSession, "_send_to_bbs"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(ssh_server.BBSClientSession, "_schedule_drain") as arm:
            client._start_bbs()
        arm.assert_called_once()

    def test_the_poll_interval_is_inside_the_slow_ack_window(self):
        """Ask Nomad arms its slow ack at 8 seconds; polling slower than that
        would let the ack and the answer arrive in the wrong order."""
        self.assertLess(ssh_server.LATE_REPLY_POLL_SECONDS, 8.0)
        self.assertGreater(ssh_server.LATE_REPLY_POLL_SECONDS, 0)

    def test_closing_cancels_the_pending_poll(self):
        client = _session_under_test()
        handle = mock.Mock()
        client._late_handle = handle
        with mock.patch.object(ssh_server.bbs_emulator, "end_session"):
            client._cleanup()
        handle.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
