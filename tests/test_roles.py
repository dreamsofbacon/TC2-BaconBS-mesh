"""Roles: who someone is, and the one role that actually stops them.

Most of these are labels today. Banned is not -- it is refused before
anything dispatches, so it has to hold from the string node id, survive a
peer trying to lift it, and not accidentally catch anyone else.

The rule underneath the whole thing: an unrecognised role resolves to the
LEAST privilege. A typo in config, or a role a newer peer invented, must
never land somewhere powerful just because nothing here recognises it.
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations
import message_processing
import utils

NODE = "!04058ac8"
OTHER = "!0408b778"
SECOND_DEVICE = "meshcore-abc123"


class _RoleCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config_path = Path(self.temp_dir.name) / "config.ini"
        self.config_path.write_text("[boards]\nbulletin_boards = General\n",
                                    encoding="utf-8")
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_CONFIG_PATH": str(self.config_path)}, clear=False)
        self.env_patch.start()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _config(self, body):
        self.config_path.write_text(body, encoding="utf-8")

    def _account_for(self, *node_ids):
        account_id = db_operations.create_account()
        for node_id in node_ids:
            db_operations.link_node_to_account(node_id, account_id, "meshtastic")
        return account_id


class RankTests(unittest.TestCase):
    def test_the_ladder_is_ordered(self):
        ladder = ['banned', 'unregistered', 'user', 'bot', 'vip',
                  'mod', 'admin', 'developer']
        ranks = [db_operations.role_rank(r) for r in ladder]
        self.assertEqual(ranks, sorted(ranks))

    def test_bot_sits_with_user_rather_than_above_it(self):
        """A label, not a promotion."""
        self.assertTrue(db_operations.role_at_least('bot', 'user'))
        self.assertFalse(db_operations.role_at_least('bot', 'vip'))

    def test_an_unknown_role_is_the_least_privileged(self):
        """The safety property. A role this build does not know -- a typo, or
        one a newer peer invented -- must not satisfy any threshold."""
        self.assertEqual(db_operations.normalize_role('wizard'), 'unregistered')
        self.assertFalse(db_operations.role_at_least('wizard', 'user'))
        self.assertFalse(db_operations.role_at_least('', 'user'))
        self.assertFalse(db_operations.role_at_least(None, 'user'))

    def test_banned_never_satisfies_a_threshold(self):
        for minimum in ('unregistered', 'user', 'mod', 'admin'):
            with self.subTest(minimum=minimum):
                self.assertFalse(db_operations.role_at_least('banned', minimum))

    def test_unregistered_is_not_assignable(self):
        """It means "has no account", which assigning would contradict."""
        self.assertNotIn('unregistered', db_operations.ASSIGNABLE_ROLES)
        self.assertIn('banned', db_operations.ASSIGNABLE_ROLES)


class ResolutionTests(_RoleCase):
    def test_a_node_with_nothing_is_unregistered(self):
        self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_a_node_role_applies_without_an_account(self):
        db_operations.set_node_role(NODE, 'vip')
        self.assertEqual(db_operations.get_node_role(NODE), 'vip')

    def test_an_account_role_covers_every_linked_device(self):
        """One answer per person. A mod with three radios is a mod on all
        three, and demoting them does not leave a grant on a device they
        forgot they linked."""
        self._account_for(NODE, SECOND_DEVICE)
        db_operations.set_node_role(NODE, 'mod')
        self.assertEqual(db_operations.get_node_role(SECOND_DEVICE), 'mod')

    def test_the_account_wins_over_a_stale_node_row(self):
        db_operations.set_node_role(NODE, 'vip')
        self._account_for(NODE)
        db_operations.set_node_role(NODE, 'user')
        self.assertEqual(db_operations.get_node_role(NODE), 'user')

    def test_one_persons_role_does_not_leak_to_another(self):
        db_operations.set_node_role(NODE, 'admin')
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')

    def test_a_blank_node_id_is_unregistered(self):
        for value in ('', '   ', None):
            with self.subTest(value=value):
                self.assertEqual(db_operations.get_node_role(value), 'unregistered')


class RemoteAssertionTests(_RoleCase):
    """Roles sync fleet-wide, so a peer can assert one for any node id."""

    def test_a_peer_can_ban(self):
        self.assertTrue(
            db_operations.apply_synced_node_role(NODE, 'banned', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'banned')

    def test_a_peer_cannot_grant_above_the_ceiling(self):
        """The whole reason the ceiling exists: the frame is unsigned like
        every other sync frame, so anything on the broker can send one, and
        nobody outside this node should be able to hand themselves admin."""
        for role in ('admin', 'developer'):
            with self.subTest(role=role):
                self.assertFalse(db_operations.apply_synced_node_role(
                    NODE, role, '2026-09-06T10:00:00'))
                self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_the_ceiling_still_allows_what_it_is_for(self):
        self.assertTrue(db_operations.apply_synced_node_role(
            NODE, 'mod', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'mod')

    def test_the_ceiling_is_configurable(self):
        self._config("[roles]\nremote_role_ceiling = user\n")
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'mod', '2026-09-06T10:00:00'))
        self.assertTrue(db_operations.apply_synced_node_role(
            NODE, 'user', '2026-09-06T10:00:00'))

    def test_a_local_operator_is_not_subject_to_the_ceiling(self):
        """The ceiling constrains peers, not the console behind a password."""
        self.assertTrue(db_operations.set_node_role(NODE, 'developer'))
        self.assertEqual(db_operations.get_node_role(NODE), 'developer')

    def test_a_stale_assertion_cannot_undo_a_newer_decision(self):
        db_operations.set_node_role(NODE, 'banned', '2026-09-06T12:00:00')
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'user', '2026-09-06T09:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'banned')

    def test_a_newer_assertion_does_apply(self):
        db_operations.set_node_role(NODE, 'banned', '2026-09-06T09:00:00')
        self.assertTrue(db_operations.apply_synced_node_role(
            NODE, 'user', '2026-09-06T12:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'user')

    def test_role_sync_can_be_turned_off_entirely(self):
        self._config("[roles]\nsync_roles = false\n")
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'banned', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_an_unknown_role_from_a_peer_is_refused(self):
        self.assertFalse(db_operations.apply_synced_node_role(
            NODE, 'superuser', '2026-09-06T10:00:00'))
        self.assertEqual(db_operations.get_node_role(NODE), 'unregistered')

    def test_an_assertion_with_no_timestamp_is_refused(self):
        """Without one there is no way to order it against a local decision."""
        self.assertFalse(db_operations.apply_synced_node_role(NODE, 'banned', ''))


class _Iface:
    bbs_nodes = []
    nodes = {NODE: {"num": 4242, "user": {"id": NODE}}}
    node_id_from_num = {4242: NODE}


class BanEnforcementTests(_RoleCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        # Both modules, because the refusal is sent from message_processing
        # and everything it lets through replies from command_handlers --
        # patching one would make "was anything sent?" mean two things.
        self._real = (message_processing.send_message, ch.send_message)
        capture = lambda text, sid, iface: self.sent.append(text)
        message_processing.send_message = capture
        ch.send_message = capture
        message_processing._banned_notified.clear()
        self.iface = _Iface()
        self.addCleanup(self._restore)

    def _restore(self):
        message_processing.send_message, ch.send_message = self._real
        message_processing._banned_notified.clear()
        utils.user_states.pop(4242, None)

    def _say(self, text="hello"):
        self.sent.clear()
        message_processing.process_message(
            sender_id=4242, message=text, interface=self.iface,
            sender_node_id=NODE)
        return self.sent

    def test_an_ordinary_user_is_not_stopped(self):
        sent = self._say()
        self.assertTrue(sent)
        self.assertNotIn(message_processing.BANNED_NOTICE, sent)

    def test_a_banned_sender_is_told_once(self):
        db_operations.set_node_role(NODE, 'banned')
        self.assertEqual(self._say(), [message_processing.BANNED_NOTICE])

    def test_and_then_ignored(self):
        """Answering every message would let someone banned for flooding keep
        making the node transmit on demand -- the behaviour they were banned
        for."""
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        for _ in range(3):
            self.assertEqual(self._say(), [])

    def test_nothing_they_send_reaches_the_bbs(self):
        """Not just no menu: no side effects either."""
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        self._say("!pb,,General,,subject,,body")
        rows = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM bulletins").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_lifting_the_ban_lets_them_back_in(self):
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        db_operations.set_node_role(NODE, 'user')
        sent = self._say()
        self.assertTrue(sent)
        self.assertNotIn(message_processing.BANNED_NOTICE, sent)

    def test_a_re_ban_explains_itself_again(self):
        """Otherwise someone banned, unbanned, then banned again gets
        silence and no idea why."""
        db_operations.set_node_role(NODE, 'banned')
        self._say()
        db_operations.set_node_role(NODE, 'user')
        self._say()
        db_operations.set_node_role(NODE, 'banned')
        self.assertEqual(self._say(), [message_processing.BANNED_NOTICE])

    def test_sync_traffic_is_not_subject_to_the_ban(self):
        """A ban is about a person using the BBS. Dropping a peer's sync
        frames because some node id is banned would corrupt replication."""
        db_operations.set_node_role(NODE, 'banned')
        self.sent.clear()
        message_processing.process_message(
            sender_id=4242, message="SYNCSTATE|0|0|0|0|0|0|0",
            interface=self.iface, is_sync_message=True, sender_node_id=NODE)
        self.assertNotIn(message_processing.BANNED_NOTICE, self.sent)


class RoleWireTests(_RoleCase):
    """The ROLE frame, end to end through process_message."""

    def _receive(self, frame):
        message_processing.process_message(
            sender_id=1, message=frame, interface=_Iface(),
            is_sync_message=True, sender_node_id="!peer1")

    def test_a_role_frame_is_applied(self):
        self._receive(f"ROLE|{OTHER}|vip|2026-09-06T10:00:00")
        self.assertEqual(db_operations.get_node_role(OTHER), "vip")

    def test_a_ban_travels(self):
        """The point of fleet-wide: someone abusive is stopped everywhere."""
        self._receive(f"ROLE|{OTHER}|banned|2026-09-06T10:00:00")
        self.assertEqual(db_operations.get_node_role(OTHER), "banned")

    def test_the_frame_cannot_grant_above_the_ceiling(self):
        """Same guarantee as the unit test, but through the real wire path,
        so a handler that bypassed apply_synced_node_role would be caught."""
        self._receive(f"ROLE|{OTHER}|developer|2026-09-06T10:00:00")
        self.assertEqual(db_operations.get_node_role(OTHER), "unregistered")

    def test_a_malformed_frame_changes_nothing(self):
        for frame in (f"ROLE|{OTHER}", f"ROLE|{OTHER}|vip", "ROLE|||",
                      f"ROLE||vip|2026-09-06T10:00:00"):
            with self.subTest(frame=frame):
                self._receive(frame)
                self.assertEqual(db_operations.get_node_role(OTHER), "unregistered")

    def test_the_frame_is_recognised_as_sync_traffic(self):
        """on_receive decides what counts as sync from a literal prefix
        list. A frame missing from it is handled as somebody typing
        "ROLE|..." at the menu and never reaches the sync path at all."""
        import inspect
        source = inspect.getsource(message_processing.on_receive)
        self.assertIn('"ROLE|"', source)

    def test_a_peer_that_does_not_understand_roles_is_not_sent_any(self):
        """Gated on the capability like every other optional frame, so an
        older peer is not handed a frame it will log as malformed."""
        sent = []
        with mock.patch.object(utils, "_send_one_sync",
                               side_effect=lambda m, p, i, **k: sent.append((m, p))),              mock.patch.object(db_operations, "peer_supports",
                               side_effect=lambda peer, cap: cap != 'role'):
            utils.send_node_role_to_bbs_nodes(
                OTHER, "vip", "2026-09-06T10:00:00", ["!old"], mock.MagicMock())
        self.assertEqual(sent, [])

    def test_a_capable_peer_is_sent_the_frame(self):
        sent = []
        with mock.patch.object(utils, "_send_one_sync",
                               side_effect=lambda m, p, i, **k: sent.append((m, p))),              mock.patch.object(db_operations, "peer_supports",
                               side_effect=lambda peer, cap: True):
            utils.send_node_role_to_bbs_nodes(
                OTHER, "vip", "2026-09-06T10:00:00", ["!new"], mock.MagicMock())
        self.assertEqual(sent, [(f"ROLE|{OTHER}|vip|2026-09-06T10:00:00", "!new")])

    def test_role_is_advertised_as_a_capability(self):
        self.assertIn('role', utils.WIRE_CAPABILITIES)


class BbsRoleCommandTests(_RoleCase):
    """!ROLE and !WHO from a radio or SSH session."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self._real = (message_processing.send_message, ch.send_message)
        capture = lambda text, sid, iface: self.sent.append(text)
        message_processing.send_message = capture
        ch.send_message = capture
        message_processing._banned_notified.clear()
        self.iface = _Iface()
        self.addCleanup(self._restore_send)

    def _restore_send(self):
        message_processing.send_message, ch.send_message = self._real
        utils.user_states.pop(4242, None)

    def _as(self, role):
        if role != 'unregistered':
            db_operations.set_node_role(NODE, role)

    def _say(self, text):
        self.sent.clear()
        message_processing.process_message(
            sender_id=4242, message=text, interface=self.iface,
            sender_node_id=NODE)
        return "\n".join(self.sent)

    def test_a_user_cannot_use_it(self):
        self._as('user')
        self.assertIn("for moderators", self._say(f"!role,,{OTHER},,vip"))
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')

    def test_a_mod_can_assign_up_to_vip(self):
        self._as('mod')
        self.assertIn("is now vip", self._say(f"!role,,{OTHER},,vip"))
        self.assertEqual(db_operations.get_node_role(OTHER), 'vip')

    def test_a_mod_cannot_appoint_another_mod(self):
        """The power to appoint is what turns one bad moderator into
        several, so it stays with admin."""
        self._as('mod')
        self.assertIn("can assign up to", self._say(f"!role,,{OTHER},,mod"))
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')

    def test_an_admin_can_appoint_a_mod(self):
        self._as('admin')
        self.assertIn("is now mod", self._say(f"!role,,{OTHER},,mod"))

    def test_nobody_can_assign_above_their_own_rank(self):
        self._as('admin')
        self.assertIn("can assign up to", self._say(f"!role,,{OTHER},,developer"))
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')

    def test_a_mod_can_ban(self):
        """The thing moderators are for."""
        self._as('mod')
        self._say(f"!role,,{OTHER},,banned")
        self.assertEqual(db_operations.get_node_role(OTHER), 'banned')

    def test_you_cannot_change_your_own_role(self):
        self._as('admin')
        self.assertIn("your own role", self._say(f"!role,,{NODE},,developer"))
        self.assertEqual(db_operations.get_node_role(NODE), 'admin')

    def test_you_cannot_demote_someone_who_outranks_you(self):
        self._as('mod')
        db_operations.set_node_role(OTHER, 'admin')
        self.assertIn("outranks you", self._say(f"!role,,{OTHER},,user"))
        self.assertEqual(db_operations.get_node_role(OTHER), 'admin')

    def test_an_unknown_role_is_refused(self):
        self._as('admin')
        self.assertIn("Unknown role", self._say(f"!role,,{OTHER},,wizard"))

    def test_bad_usage_explains_itself(self):
        self._as('admin')
        self.assertIn("Usage:", self._say("!role,,onlyone"))

    def test_who_reports_a_role(self):
        self._as('mod')
        db_operations.set_node_role(OTHER, 'vip')
        self.assertIn("vip", self._say(f"!who,,{OTHER}"))

    def test_who_is_not_for_ordinary_users(self):
        """It reports on other people."""
        self._as('user')
        self.assertIn("for moderators", self._say(f"!who,,{OTHER}"))

    def test_the_commands_can_be_turned_off(self):
        self._config("[roles]\nbbs_commands = false\n")
        self._as('admin')
        self.assertIn("turned off", self._say(f"!role,,{OTHER},,vip"))
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')

    def test_quick_help_hides_them_from_ordinary_users(self):
        """A moderator toolkit on everyone's help screen invites attempts,
        and every attempt costs the node a refusal on air."""
        self._as('user')
        self.assertNotIn("!ROLE", self._say("!q"))

    def test_quick_help_shows_them_to_a_mod(self):
        self._as('mod')
        self.assertIn("!ROLE", self._say("!q"))

    def test_quick_help_hides_them_when_the_commands_are_off(self):
        self._config("[roles]\nbbs_commands = false\n")
        self._as('admin')
        self.assertNotIn("!ROLE", self._say("!q"))

    def test_a_banned_moderator_gets_nowhere(self):
        """Banned is checked before dispatch, so rank cannot rescue it."""
        db_operations.set_node_role(NODE, 'banned')
        self.assertEqual(self._say(f"!role,,{OTHER},,vip"),
                         message_processing.BANNED_NOTICE)
        self.assertEqual(db_operations.get_node_role(OTHER), 'unregistered')


class WebAdminRoleTests(unittest.TestCase):
    """Assigning a role, from the console that is actually behind a password."""

    def setUp(self):
        import configparser
        from web_admin import create_app
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp_dir.name)
        self.config_path = root / "config.ini"
        config = configparser.ConfigParser()
        config["admin"] = {"username": "admin", "password": "oldpass"}
        config["boards"] = {"bulletin_boards": "General"}
        with open(self.config_path, "w", encoding="utf-8") as handle:
            config.write(handle)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_CONFIG_PATH": str(self.config_path),
             "BBS_DB_PATH": str(root / "bulletins.db"),
             "BBS_WEBGUI_SECRET": "test-secret"},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self.account_id = db_operations.create_account()
        db_operations.link_node_to_account(NODE, self.account_id, "meshtastic")
        db_operations.set_account_alias(self.account_id, "somebody")
        self.create_app = create_app
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin
        db_operations.remove_connection_log_handler()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _client(self):
        client = self.create_app().test_client()
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        client.post("/login", data={"username": "admin", "password": "oldpass",
                                    "csrf_token": token})
        return client

    def _post(self, client, path, data):
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        form = dict(data)
        form["csrf_token"] = token
        return client.post(path, data=form, follow_redirects=False)

    def test_the_account_page_offers_every_assignable_role(self):
        page = self._client().get(f"/accounts/{self.account_id}").get_data(as_text=True)
        for role in db_operations.ASSIGNABLE_ROLES:
            with self.subTest(role=role):
                self.assertIn(f'value="{role}"', page)
        self.assertNotIn('value="unregistered"', page)

    def test_setting_a_role_takes_effect_for_the_linked_device(self):
        client = self._client()
        self._post(client, f"/accounts/{self.account_id}", {"action": "set_role",
                                                            "role": "mod"})
        self.assertEqual(db_operations.get_node_role(NODE), "mod")

    def test_banning_from_the_account_page_works(self):
        client = self._client()
        self._post(client, f"/accounts/{self.account_id}", {"action": "set_role",
                                                            "role": "banned"})
        self.assertEqual(db_operations.get_node_role(NODE), "banned")

    def test_an_unknown_role_is_refused(self):
        client = self._client()
        self._post(client, f"/accounts/{self.account_id}", {"action": "set_role",
                                                            "role": "wizard"})
        self.assertEqual(db_operations.get_node_role(NODE), "user")

    def test_the_local_console_is_not_subject_to_the_remote_ceiling(self):
        """The ceiling constrains peers. An operator at the web admin can
        grant developer, or the ceiling would lock them out of their own
        node."""
        client = self._client()
        self._post(client, f"/accounts/{self.account_id}", {"action": "set_role",
                                                            "role": "developer"})
        self.assertEqual(db_operations.get_node_role(NODE), "developer")

    def test_a_node_with_no_account_gets_its_role_on_the_client_page(self):
        """Most radio users never register, so without this the only people
        who could be banned are the ones who did."""
        client = self._client()
        self._post(client, f"/clients/{OTHER}/role", {"role": "banned"})
        self.assertEqual(db_operations.get_node_role(OTHER), "banned")

    def test_a_linked_device_is_sent_to_its_account_instead(self):
        """Two places to set one thing is how they end up disagreeing."""
        client = self._client()
        self._post(client, f"/clients/{NODE}/role", {"role": "banned"})
        self.assertEqual(db_operations.get_node_role(NODE), "user")

    def test_the_accounts_list_shows_the_role(self):
        db_operations.set_account_role(self.account_id, "vip")
        page = self._client().get("/accounts").get_data(as_text=True)
        self.assertIn("vip", page)

    def test_setting_a_role_needs_a_login(self):
        client = self.create_app().test_client()
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        response = client.post(f"/clients/{OTHER}/role",
                               data={"role": "developer", "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        self.assertEqual(db_operations.get_node_role(OTHER), "unregistered")

    def test_setting_a_role_needs_a_csrf_token(self):
        client = self._client()
        client.post(f"/clients/{OTHER}/role", data={"role": "developer"})
        self.assertEqual(db_operations.get_node_role(OTHER), "unregistered")


if __name__ == "__main__":
    unittest.main()
