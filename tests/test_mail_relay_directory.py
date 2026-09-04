"""The Relay Directory lists you; the Send flow then said you do not exist.

get_mail_relay_directory only leaves the sender out when it is asked to. The
browse view does not ask, so it lists the reader. The Send flow and
_resolve_mail_relay_recipient both do ask, so typing your own name there
failed -- and the failure said "not found, is ambiguous, or has not opted
in", three causes, none of which is the real one. Someone who has just read
their own name on the previous screen is told it does not exist, which reads
as a broken lookup rather than a rule.

The browse page also printed "[0] Back" while its handler accepted only 'x',
so the single key the screen told you to press was the one that did nothing.
"""

import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations

ME = "!04058ac8"
THEM = "!bbb22222"


class _Iface:
    bbs_nodes = []
    nodes = {ME: {"num": 1234, "user": {"id": ME}}}


class RelayRefusalTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        for node_id, alias in ((ME, "myself"), (THEM, "somebody")):
            account_id = db_operations.create_account()
            db_operations.link_node_to_account(node_id, account_id, "meshtastic")
            db_operations.set_account_alias(account_id, alias)
            db_operations.set_account_mail_relay(account_id, True)

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_the_browse_directory_still_lists_you(self):
        """Kept deliberately: it is how you confirm your own opt-in.

        Driven through the handler, not through get_mail_relay_directory --
        the exclusion is the handler's choice of argument, so testing the
        database call proves nothing about which choice it made."""
        sent = []
        patches = [
            mock.patch.object(ch, "send_message",
                              side_effect=lambda text, *_a, **_k: sent.append(text)),
            mock.patch.object(ch, "get_node_id_from_num", return_value=ME),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        ch.handle_active_users_command(1234, _Iface())
        self.assertIn("myself", sent[-1])
        self.assertIn("somebody", sent[-1])

    def test_the_send_flow_still_leaves_you_out(self):
        names = [entry["display_name"]
                 for entry in db_operations.get_mail_relay_directory(ME)]
        self.assertNotIn("myself", names)
        self.assertIn("somebody", names)

    def test_addressing_yourself_says_so(self):
        message = ch._mail_recipient_refusal("myself", ME)
        self.assertIn("That is you", message)
        self.assertNotIn("not found", message)

    def test_a_genuine_miss_still_says_not_found(self):
        message = ch._mail_recipient_refusal("nobody-by-that-name", ME)
        self.assertIn("not found", message)
        self.assertNotIn("That is you", message)

    def test_addressing_somebody_else_is_not_treated_as_yourself(self):
        """Guard against the check firing whenever anything resolves at all.

        Unreachable through the menus -- a name that resolves for somebody
        else never gets as far as a refusal -- but the helper should still
        answer honestly about the name it was handed."""
        self.assertIsNotNone(ch._resolve_mail_relay_recipient("somebody", ME))
        self.assertNotIn("That is you", ch._mail_recipient_refusal("somebody", ME))

    def test_no_sender_means_no_claim_that_it_is_you(self):
        self.assertNotIn("That is you", ch._mail_recipient_refusal("myself", None))

    def test_your_own_node_id_is_recognised_too_not_just_your_alias(self):
        message = ch._mail_recipient_refusal(ME, ME)
        self.assertIn("That is you", message)

    def test_a_sibling_linked_device_counts_as_you(self):
        """Two radios on one account are one person, so mailing the other
        one is still mailing yourself."""
        account_id = db_operations.get_account_id_for_node(ME)
        db_operations.link_node_to_account("!aaa33333", account_id, "meshtastic")
        self.assertIn("That is you", ch._mail_recipient_refusal("!aaa33333", ME))

    def test_the_prefix_is_carried_for_the_quick_command(self):
        message = ch._mail_recipient_refusal("myself", ME, prefix="Relay user 'myself': ")
        self.assertTrue(message.startswith("Relay user 'myself': "))
        self.assertIn("That is you", message)


class RelayDirectoryNavigationTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self.iface = _Iface()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _browse(self, key):
        state = {'command': 'MAIL', 'step': 10,
                 'directory': [{'display_name': 'somebody', 'protocols': ['Meshtastic'],
                                'node_ids': [THEM]}],
                 'directory_page': 0}
        with mock.patch.object(ch, "send_message",
                               side_effect=lambda text, *_a, **_k: self.sent.append(text)):
            ch.update_user_state(1234, state)
            ch.handle_mail_steps(1234, key, 10, state, self.iface, [])
        return self.sent

    def test_zero_goes_back_because_the_page_says_it_does(self):
        sent = self._browse("0")
        self.assertIn("Mail Menu", sent[-1])

    def test_x_still_goes_back(self):
        self.assertIn("Mail Menu", self._browse("x")[-1])

    def test_the_page_advertises_the_key_that_works(self):
        page = ch._mail_directory_page(
            [{'display_name': 'somebody', 'protocols': ['Meshtastic']}], 0, selecting=False)
        self.assertIn("[0] Back", page)

    def test_an_unrecognised_key_reshows_the_page_rather_than_a_wrong_hint(self):
        """It used to answer "Reply N, P, or X." -- which omitted the 0 the
        page itself offers, and named keys that may not be on this page."""
        sent = self._browse("?")
        self.assertIn("Relay Directory", sent[-1])
        self.assertNotIn("Reply N, P, or X.", sent[-1])


if __name__ == "__main__":
    unittest.main()
