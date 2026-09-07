"""Two features that existed but could not be reached from the outside.

Web Fetch was enabled on the live node with an empty ``[gateway]
allowed_hosts``, so every URL a user typed came back
``[ERR] blocked: no allowed_hosts configured`` -- the name of a setting
they cannot read, after a round trip over the radio. And the version
number, which every deploy bumps, was reachable only from the web admin
and the Docker build, so nobody on a radio or an SSH session could say
which release they had reached.

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


class WebFetchRefusalTests(unittest.TestCase):
    """An empty allow-list must be said once, up front, in English."""

    def setUp(self):
        self.radio = _Radio()
        patches = [
            patch.object(command_handlers, "send_message", self.radio.capture),
            patch.object(command_handlers, "get_max_text_bytes",
                         lambda interface=None: self.radio.max_bytes),
            patch.object(command_handlers, "_apigw_authorized", lambda s, i: True),
            patch.object(command_handlers, "handle_help_command",
                         lambda *a, **k: None),
            patch.object(command_handlers, "update_user_state",
                         lambda sid, st: self.states.append(st)),
        ]
        self.states = []
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _gateway(self, enabled=True, hosts=()):
        p1 = patch.object(gateway, "is_gateway_enabled", lambda: enabled)
        p2 = patch.object(gateway, "allowed_hosts", lambda: list(hosts))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_no_allowed_hosts_refuses_before_the_url_prompt(self):
        self._gateway(enabled=True, hosts=())
        command_handlers.handle_apigw_command("!user", object())
        self.assertIn("not set up", self.radio.text)
        # And it did NOT park the user in the URL prompt, which is the whole
        # point: nothing they could type there would work.
        self.assertEqual(
            [], [s for s in self.states if s.get("command") == "APIGW"])

    def test_the_refusal_never_names_a_config_setting(self):
        self._gateway(enabled=True, hosts=())
        command_handlers.handle_apigw_command("!user", object())
        self.assertNotIn("allowed_hosts", self.radio.text)
        self.assertNotIn("[gateway]", self.radio.text)

    def test_the_prompt_names_the_sites_that_will_work(self):
        self._gateway(enabled=True, hosts=("wttr.in", "api.open-meteo.com"))
        command_handlers.handle_apigw_command("!user", object())
        self.assertIn("wttr.in", self.radio.text)
        self.assertIn("api.open-meteo.com", self.radio.text)
        # The user IS put in the URL prompt this time.
        self.assertEqual(
            "http",
            [s for s in self.states if s.get("command") == "APIGW"][-1]["mode"])

    def test_the_prompt_fits_one_packet(self):
        self._gateway(enabled=True,
                      hosts=[f"host{n}.example.com" for n in range(40)])
        self.radio.max_bytes = 160
        command_handlers.handle_apigw_command("!user", object())
        self.assertLessEqual(len(self.radio.sent[0].encode("utf-8")), 160)
        self.assertIn("...", self.radio.sent[0])

    def test_a_forwarding_node_does_not_guess_at_a_peers_list(self):
        """Not a gateway itself -- it cannot see the peer's allow-list, so it
        must neither refuse nor claim to know which sites are allowed."""
        self._gateway(enabled=False, hosts=())
        command_handlers.handle_apigw_command("!user", object())
        self.assertNotIn("not set up", self.radio.text)
        self.assertNotIn("allowed:", self.radio.text)
        self.assertEqual(
            "http",
            [s for s in self.states if s.get("command") == "APIGW"][-1]["mode"])


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

    def _run(self, node_id, display="v9.9 (abc1234)"):
        with patch("version_info.get_display_version", lambda: display), \
             patch("db_operations.get_local_node_id", lambda: node_id):
            command_handlers.handle_version_command("!user", object())
        return self.radio.sent[-1]

    def test_the_version_reaches_the_user(self):
        self.assertIn("v9.9 (abc1234)", self._run("!0408b778"))

    def test_it_names_the_node_by_its_nickname(self):
        with patch.object(command_handlers, "node_display_name",
                          lambda nid, **k: "Burlington"):
            self.assertIn("Burlington", self._run("!0408b778"))

    def test_this_node_is_replaced_by_something_quotable(self):
        """'on this node' answers a question nobody asked. The id does."""
        with patch.object(command_handlers, "node_display_name",
                          lambda nid, **k: "this node"):
            line = self._run("!0408b778")
        self.assertNotIn("this node", line)
        self.assertIn("0408b778", line)

    def test_an_unknown_id_leaves_the_clause_off_entirely(self):
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
