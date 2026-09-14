"""The "ideas to be organized" batch from feature requests.txt (2026-09-14).

1. The Relay Directory's "[#] Write" footer read as a key to press, and #
   did nothing. It also listed accounts no relay can reach.
2. Project Nomad errors: a model the AI server does not have was reported as
   a wrong API path. (Covered in test_api_gateway.)
3. Node View never said which node "This node" is.
4. The chatter page labelled radios by their raw key.
5. Entering a link code on a device already on another account refused with
   no names -- and a device alone on its account could never be unlinked, so
   one person's two radios on two accounts could never be joined.
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

import command_handlers
import db_operations
import utils


class _DbCase(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        for user in (1, 2):
            command_handlers.update_user_state(user, None)
        self.iface = types.SimpleNamespace(nodes={}, bbs_nodes=[], allowed_nodes=[])
        send = mock.patch.object(command_handlers, "send_message")
        self.send = send.start()
        self.addCleanup(send.stop)

    def tearDown(self):
        for user in (1, 2):
            command_handlers.update_user_state(user, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def sent(self):
        return [call.args[0] for call in self.send.call_args_list]


class RelayDirectoryTests(_DbCase):
    def _opt_in(self, node_id, network, alias):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, alias)
        db_operations.link_node_to_account(node_id, account_id, network)
        db_operations.set_account_mail_relay(account_id, True)
        return account_id

    def test_the_footer_names_the_numbers_to_type(self):
        entries = [{"display_name": n, "protocols": ["MeshCore"]} for n in ("A", "B", "C")]
        page = command_handlers._mail_directory_page(entries, 0, selecting=False)
        self.assertIn("[1-3] Write", page)
        self.assertNotIn("[#]", page)
        one = command_handlers._mail_directory_page(entries[:1], 0, selecting=False)
        self.assertIn("[1] Write", one)

    def test_hash_or_w_explains_what_to_type(self):
        self._opt_in("ab" * 32, "meshcore", "Materva")
        self._opt_in("!0a1b2c3d", "meshtastic", "Bacon")
        command_handlers.handle_active_users_command(1, self.iface)
        for key in ("#", "w"):
            with self.subTest(key=key):
                self.send.reset_mock()
                command_handlers.handle_mail_steps(
                    1, key, 10, command_handlers.get_user_state(1), self.iface, [])
                self.assertIn("Reply with the number of the person to write to (1-2)",
                              self.sent()[0])

    def test_a_number_still_starts_the_mail(self):
        self._opt_in("ab" * 32, "meshcore", "Materva")
        command_handlers.handle_active_users_command(1, self.iface)
        command_handlers.handle_mail_steps(
            1, "1", 10, command_handlers.get_user_state(1), self.iface, [])
        self.assertIn("message to Materva", self.sent()[-1])

    def test_logins_that_are_not_radios_say_what_they_are(self):
        """Live: 'baconbot (Unknown)' was an SSH login and 'emu:1 (Unknown)'
        an emulator session. They stay listed -- the directory is also how
        Mail > Send picks a recipient -- but no longer as Unknown."""
        self._opt_in("ssh:" + "f" * 32, "ssh", "baconbot")
        db_operations.apply_synced_mail_relay_preference("emu:1", True, "2099-01-01T00:00:00+00:00")
        self._opt_in("ab" * 32, "meshcore", "Materva")
        entries = {e["display_name"]: e["protocols"] for e in db_operations.get_mail_relay_directory()}
        self.assertEqual(entries["baconbot"], ["SSH"])
        self.assertEqual(entries["emu:1"], ["Emulator"])
        self.assertNotIn(["Unknown"], entries.values())


class MqttRelayTests(unittest.TestCase):
    """v0.1.651 cancelled relay mail for MQTT users along with SSH logins. They
    can receive direct messages, so they are relay targets again."""

    def test_mqtt_is_a_relay_target_and_ssh_is_not(self):
        self.assertTrue(db_operations.is_mail_relay_radio_target("mqtt:home:guest"))
        self.assertTrue(db_operations.is_mail_relay_radio_target("!0a1b2c3d"))
        self.assertTrue(db_operations.is_mail_relay_radio_target("ab" * 32))
        self.assertFalse(db_operations.is_mail_relay_radio_target("ssh:abc"))
        self.assertFalse(db_operations.is_mail_relay_radio_target("emu:1"))


class NodeViewNameTests(_DbCase):
    def test_this_node_says_which_node(self):
        with mock.patch.object(command_handlers, "_this_node_label", return_value="burlington"):
            options = command_handlers._node_view_options(1)
        self.assertEqual(options[1]["label"], "This node (burlington)")

    def test_an_unnamed_node_still_reads_this_node(self):
        with mock.patch.object(command_handlers, "_this_node_label", return_value=""):
            options = command_handlers._node_view_options(1)
        self.assertEqual(options[1]["label"], "This node")

    def test_the_settings_lens_line_matches(self):
        with mock.patch.object(command_handlers, "_this_node_label", return_value="burlington"), \
                mock.patch.object(command_handlers, "local_identities_for_display",
                                  return_value={"mqtt:x:burlington"}):
            self.assertEqual(command_handlers._scope_label(["mqtt:x:burlington"]),
                             "This node (burlington)")


class CaptureLabelTests(unittest.TestCase):
    LOCAL_MC = "88166fee0f70" + "0" * 52
    LOCAL_MT = "hW9UeHYKg+eUfhBDhHNBoT38QCnmlAXebk1OR/l6LGc="
    REMOTE = "5a582498f3d5f2b91a9ea3bbb21c6f1f2355bc3eca060cfac6a98a5105f69930"

    def labels(self, captures, radios=(), local_ids=(), nicknames=None):
        with mock.patch.object(utils, "local_node_label", return_value="burlington"):
            return utils.capture_radio_labels(captures, radios, local_ids=set(local_ids),
                                              nicknames=nicknames or {})

    def test_this_nodes_radio_by_its_own_name(self):
        labels = self.labels({self.LOCAL_MC: "meshcore"},
                             [{"capture_node_id": self.LOCAL_MC, "local_long_name": "🥓 BBS"}])
        self.assertEqual(labels[self.LOCAL_MC], "🥓 BBS (burlington)")

    def test_a_remote_radio_by_network_and_place(self):
        labels = self.labels({self.REMOTE: "meshcore"}, nicknames={self.REMOTE: "chattanooga"})
        self.assertEqual(labels[self.REMOTE], "MeshCore radio (chattanooga)")

    def test_a_local_radio_the_snapshot_has_not_described_yet(self):
        labels = self.labels({self.LOCAL_MT: "meshtastic"}, local_ids={self.LOCAL_MT})
        self.assertEqual(labels[self.LOCAL_MT], "Meshtastic radio (burlington)")

    def test_an_unknown_radio_gets_no_label(self):
        """The page falls back to the shortened key, as before."""
        self.assertEqual(self.labels({"deadbeef" * 8: "meshcore"}), {})

    def test_the_api_attaches_them(self):
        import inspect
        import web_admin
        source = inspect.getsource(web_admin)
        self.assertIn("_label_chatter_captures(result)", source)
        with open("static/js/public-chatter.js", encoding="utf-8") as handle:
            self.assertIn("entry.capture_label ||", handle.read())


class LinkMoveTests(_DbCase):
    MESHCORE = "ab" * 32

    def setUp(self):
        super().setUp()
        # The live shape: one person, two radios, two accounts -- the
        # MeshCore radio got one by switching on mail relay.
        self.home = db_operations.create_account()
        db_operations.set_account_alias(self.home, "Colin")
        db_operations.link_node_to_account("!0a1b2c3d", self.home, "meshtastic")
        self.stray = db_operations.create_account()
        db_operations.link_node_to_account(self.MESHCORE, self.stray, "meshcore")
        self.code = db_operations.create_link_code(self.home, "!0a1b2c3d")

    def enter(self, text, node=None, step=2, **state):
        command_handlers.update_user_state(1, dict({"command": "ACCOUNT", "step": step}, **state))
        command_handlers.handle_account_steps(1, text, self.iface,
                                              sender_node_id=node or self.MESHCORE)

    def test_it_names_both_accounts_and_asks(self):
        self.enter(self.code)
        prompt = self.sent()[0]
        self.assertIn("This device is on an unnamed account (only this device)", prompt)
        self.assertIn("for 'Colin'", prompt)
        self.assertIn("[Y/N]", prompt)
        self.assertLessEqual(len(prompt.encode()), 160, "one MeshCore packet")
        self.assertEqual(db_operations.get_account_id_for_node(self.MESHCORE), self.stray)

    def test_yes_moves_the_device_even_when_it_was_alone(self):
        self.enter(self.code)
        self.enter("y", step=7, code=self.code)
        self.assertEqual(db_operations.get_account_id_for_node(self.MESHCORE), self.home)
        self.assertIn("Device moved to 'Colin'.", self.sent())
        self.assertEqual(db_operations.count_linked_nodes(self.home), 2)

    def test_no_leaves_it_and_keeps_the_code(self):
        self.enter(self.code)
        self.enter("n", step=7, code=self.code)
        self.assertEqual(db_operations.get_account_id_for_node(self.MESHCORE), self.stray)
        self.assertEqual(db_operations.describe_link_code(self.code, "!ffffffff")["status"], "ok")

    def test_the_code_is_checked_again_at_yes(self):
        self.enter(self.code)
        db_operations.get_db_connection().execute(
            "UPDATE link_codes SET expires_at = '2000-01-01 00:00:00'")
        db_operations.get_db_connection().commit()
        self.enter("y", step=7, code=self.code)
        self.assertEqual(db_operations.get_account_id_for_node(self.MESHCORE), self.stray)
        self.assertIn("expired", self.sent()[-2])

    def test_a_used_code_cannot_move_another_device(self):
        self.enter(self.code)
        self.enter("y", step=7, code=self.code)
        other = db_operations.create_account()
        db_operations.link_node_to_account("cd" * 32, other, "meshcore")
        ok, msg = db_operations.move_node_with_link_code(self.code, "cd" * 32, "meshcore")
        self.assertFalse(ok)
        self.assertEqual(db_operations.get_account_id_for_node("cd" * 32), other)

    def test_the_same_account_is_called_yours(self):
        own = db_operations.create_link_code(self.home, "!0a1b2c3d")
        self.enter(own, node="!0a1b2c3d")
        self.assertIn("already linked to your account, 'Colin'", self.sent()[0])

    def test_an_ssh_login_is_never_moved(self):
        ssh = "ssh:" + "e" * 32
        db_operations.link_node_to_account(ssh, self.stray, "ssh")
        self.enter(self.code, node=ssh)
        self.assertIn("can't be moved", self.sent()[0])
        self.assertEqual(db_operations.get_account_id_for_node(ssh), self.stray)
        self.assertNotEqual((command_handlers.get_user_state(1) or {}).get("step"), 7)

    def test_a_full_account_refuses_before_asking(self):
        for n in range(6):
            db_operations.link_node_to_account(f"!0000000{n}", self.home, "meshtastic")
        self.enter(self.code)
        self.assertIn("maximum number", self.sent()[0])
        self.assertEqual(db_operations.get_account_id_for_node(self.MESHCORE), self.stray)


if __name__ == "__main__":
    unittest.main()
