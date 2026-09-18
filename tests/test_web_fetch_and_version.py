"""The version command, and what is left of the raw URL fetch.

Web Fetch is gone from the menu -- doors replaced it, see tests/test_doors.py
-- but ``kind='h'`` still exists on the wire so an older peer's request is
answered rather than dropped, and it is still governed by ``validate_url``.
What is tested here is that its refusals stay readable, since a node that
opts back into raw fetching inherits them.

The version number, which every deploy bumps, was reachable only from the
web admin and the Docker build, so nobody on a radio or an SSH session could
say which release they had reached.

These tests drive the HANDLERS, not the helpers underneath them: what
matters is the sentence the user gets.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import command_handlers
import gateway


class _Radio:
    """Collects what the BBS put on the air, and what it did to the state."""

    def __init__(self, max_bytes=220):
        self.sent = []
        self.max_bytes = max_bytes

    def capture(self, text, sender_id, interface):
        self.sent.append(text)
        return True

    @property
    def text(self):
        return "\n".join(self.sent)


class GatewayRefusalWordingTests(unittest.TestCase):
    """validate_url's reason is shown to the user verbatim as 'blocked: …'."""

    def test_empty_allow_list_reason_is_readable(self):
        with patch.object(gateway, "_config_raw",
                          lambda s, o: {"allowed_schemes": "https"}.get(o)):
            ok, reason = gateway.validate_url("https://wttr.in/NYC")
        self.assertFalse(ok)
        self.assertNotIn("allowed_hosts", reason)
        self.assertIn("fetch", reason)

    def test_a_disallowed_host_is_told_what_is_allowed(self):
        cfg = {"allowed_hosts": "wttr.in", "allowed_schemes": "https"}
        with patch.object(gateway, "_config_raw", lambda s, o: cfg.get(o)):
            ok, reason = gateway.validate_url("https://evil.example.com/x")
        self.assertFalse(ok)
        self.assertIn("wttr.in", reason)


class VersionCommandTests(unittest.TestCase):
    def setUp(self):
        self.radio = _Radio()
        p = patch.object(command_handlers, "send_message", self.radio.capture)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, node_id, *, link_ids=(), nicknames=None,
             display="v9.9 (abc1234)"):
        with patch("version_info.get_display_version", lambda: display), \
             patch("db_operations.get_local_node_id", lambda: node_id), \
             patch("db_operations.get_persisted_local_link_ids",
                   lambda: list(link_ids)), \
             patch.object(command_handlers, "get_node_nicknames",
                          lambda: dict(nicknames or {})):
            command_handlers.handle_version_command("!user", object())
        return self.radio.sent[-1]

    def test_the_version_reaches_the_user(self):
        self.assertIn("v9.9 (abc1234)", self._run("!0408b778"))

    def test_it_names_the_node_by_its_nickname(self):
        line = self._run("!0408b778", nicknames={"!0408b778": "Burlington"})
        self.assertIn("Burlington", line)

    def test_an_mqtt_id_reads_as_its_label(self):
        self.assertIn("Burlington-NNE",
                      self._run("mqtt:baconbbsvt:Burlington-NNE"))

    def test_it_never_says_this_node(self):
        """'on this node' answers a question nobody asked."""
        self.assertNotIn("this node", self._run("!0408b778"))

    def test_ssh_falls_back_to_the_persisted_link_ids(self):
        """bacon-ssh owns no radio, so get_local_node_id() is empty there --
        the gap that once left Node View inert over SSH."""
        line = self._run("", link_ids=["mqtt:baconbbsvt:Burlington-NNE"])
        self.assertIn("Burlington-NNE", line)

    def test_capture_ids_are_not_offered_as_a_name(self):
        """Those are radio public keys; get_persisted_local_link_ids must be
        the source, not the whole identity set."""
        import db_operations
        self.assertIn("kind = 'link'",
                      __import__("inspect").getsource(
                          db_operations.get_persisted_local_link_ids))

    def test_a_nickname_beats_an_mqtt_label(self):
        line = self._run("", link_ids=["mqtt:baconbbsvt:Burlington-NNE"],
                         nicknames={"mqtt:baconbbsvt:Burlington-NNE": "Burl"})
        self.assertIn("Burl", line)
        self.assertNotIn("Burlington-NNE", line)

    def test_no_identity_at_all_leaves_the_clause_off_entirely(self):
        line = self._run("")
        self.assertIn("v9.9 (abc1234)", line)
        self.assertNotIn(" on ", line)
        self.assertNotIn("unknown", line)

    def test_the_quick_help_screen_advertises_it(self):
        with patch.object(command_handlers, "_role_commands_available",
                          lambda s, i: False):
            command_handlers.handle_quick_help_command("!user", object())
        self.assertIn("!VER", self.radio.text)


class VersionDispatchTests(unittest.TestCase):
    def test_bang_ver_is_wired_to_the_handler(self):
        import message_processing
        self.assertIs(message_processing.handle_version_command,
                      command_handlers.handle_version_command)
        import inspect
        src = inspect.getsource(message_processing.process_message)
        self.assertIn('"ver"', src)


if __name__ == "__main__":
    unittest.main()
