"""An SSH user can reset a forgotten password with a code from a linked radio.

Reported 2026-09-14: there was no user-side reset. A radio linked to the
account proves who the user is; it asks for a one-time code (Settings &
Profile > Linked devices > [7]) and the user logs in as "reset:<alias>" with
that code as the password, then chooses a new one.
"""
import asyncio
import os
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

import asyncssh

import bbs_emulator
import command_handlers
import db_operations
import ssh_auth
from ssh_server import SSHConfig, start_server


def _account(alias="Colin", password="old-password-123"):
    password_hash, password_salt = ssh_auth.hash_password(password)
    account_id = db_operations.create_ssh_account(alias, password_hash, password_salt)
    db_operations.link_node_to_account("!0a1b2c3d", account_id, "meshtastic")
    return account_id


class _DbCase(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection


class ResetCodeTests(_DbCase):
    def test_a_code_resets_the_password(self):
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(db_operations.check_password_reset_code("Colin", code), account_id)
        new_hash, new_salt = ssh_auth.hash_password("brand-new-password")
        self.assertTrue(db_operations.set_ssh_password_with_reset_code("Colin", code, new_hash, new_salt))
        self.assertIsNotNone(ssh_auth.authenticate("Colin", "brand-new-password", "1.2.3.4"))
        self.assertIsNone(ssh_auth.authenticate("Colin", "old-password-123", "1.2.3.4"))

    def test_a_code_is_single_use(self):
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        h, s = ssh_auth.hash_password("brand-new-password")
        self.assertTrue(db_operations.set_ssh_password_with_reset_code("Colin", code, h, s))
        self.assertFalse(db_operations.set_ssh_password_with_reset_code("Colin", code, h, s))

    def test_checking_does_not_use_it_up(self):
        """A dropped connection between login and new password wastes nothing."""
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        db_operations.check_password_reset_code("Colin", code)
        self.assertEqual(db_operations.check_password_reset_code("Colin", code), account_id)

    def test_wrong_expired_and_replaced_codes_fail(self):
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        self.assertIsNone(db_operations.check_password_reset_code("Colin", wrong))
        self.assertIsNone(db_operations.check_password_reset_code("Colin", "abc"))
        self.assertIsNone(db_operations.check_password_reset_code("Nobody", code))
        newer = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        if newer != code:
            self.assertIsNone(db_operations.check_password_reset_code("Colin", code))
        past = (datetime.now() - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
        db_operations.get_db_connection().execute("UPDATE password_reset_codes SET expires_at = ?", (past,))
        self.assertIsNone(db_operations.check_password_reset_code("Colin", newer))

    def test_the_code_is_not_stored_in_the_clear(self):
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        row = db_operations.get_db_connection().execute(
            "SELECT * FROM password_reset_codes").fetchone()
        self.assertNotIn(code, [str(v) for v in row])

    def test_no_code_for_an_account_without_a_password_here(self):
        """Passwords stay on the node where the account registered for SSH."""
        account_id = db_operations.create_account()
        self.assertIsNone(db_operations.create_password_reset_code(account_id, "!x"))


class ResetLoginTests(_DbCase):
    def test_reset_prefix_authenticates_with_the_code(self):
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        result = ssh_auth.authenticate(f"reset:Colin", code, "1.2.3.4")
        self.assertEqual((result.account_id, result.reset_code), (account_id, code))
        self.assertIsNone(ssh_auth.authenticate("reset:Colin", "000000" if code != "000000" else "111111",
                                                "1.2.3.4"))

    def test_guessing_is_rate_limited_per_account(self):
        account_id = _account()
        code = db_operations.create_password_reset_code(account_id, "!0a1b2c3d")
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        for n in range(5):
            ssh_auth.authenticate("reset:Colin", wrong, f"10.0.0.{n}")   # new address each time
        self.assertIsNone(ssh_auth.authenticate("reset:Colin", code, "10.0.0.99"),
                          "the right code is refused once the account's tries are spent")

    def test_a_normal_login_is_unaffected(self):
        _account()
        result = ssh_auth.authenticate("Colin", "old-password-123", "1.2.3.4")
        self.assertEqual(result.reset_code, "")


class RadioRequestTests(_DbCase):
    def setUp(self):
        super().setUp()
        self.iface = types.SimpleNamespace(nodes={}, bbs_nodes=[], allowed_nodes=[])
        send = mock.patch.object(command_handlers, "send_message")
        self.send = send.start()
        self.addCleanup(send.stop)
        self.addCleanup(command_handlers.update_user_state, 1, None)

    def choose(self, node):
        command_handlers.update_user_state(1, {"command": "ACCOUNT", "step": 1})
        command_handlers.handle_account_steps(1, "7", self.iface, sender_node_id=node)
        return [c.args[0] for c in self.send.call_args_list]

    def test_the_menu_offers_it(self):
        self.assertIn("[7] Reset SSH password", command_handlers._ACCOUNT_MENU_TEXT)

    def test_a_linked_radio_gets_a_code_and_instructions(self):
        _account()
        sent = self.choose("!0a1b2c3d")
        self.assertRegex(sent[0], r"SSH reset code: \d{6}")
        self.assertIn("reset:Colin", sent[0])

    def test_not_from_ssh(self):
        account_id = _account()
        sent = self.choose(f"ssh:{account_id}")
        self.assertIn("not from SSH", sent[0])
        self.assertIsNone(db_operations.get_db_connection().execute(
            "SELECT 1 FROM password_reset_codes").fetchone())

    def test_an_unlinked_radio_or_account_without_password(self):
        self.assertIn("isn't linked", self.choose("!ffffffff")[0])
        self.send.reset_mock()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!eeeeeeee", account_id, "meshtastic")
        self.assertIn("no SSH password on this node", self.choose("!eeeeeeee")[0])

    def test_requests_are_rate_limited(self):
        _account()
        for _ in range(3):
            self.choose("!0a1b2c3d")
        self.send.reset_mock()
        self.assertIn("Too many reset requests", self.choose("!0a1b2c3d")[0])


class ResetOverSSHTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []
        self.temp_dir = tempfile.TemporaryDirectory()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.account_id = _account()

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.close()
        for connection in self.connections:
            await asyncio.wait_for(connection.wait_closed(), timeout=5)
        self.listener.close()
        await self.listener.wait_closed()
        for token in list(bbs_emulator._sessions):
            bbs_emulator.end_session(token)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        self.temp_dir.cleanup()

    async def _listen(self, **extra):
        config = SSHConfig(enabled=True, host="127.0.0.1", port=0,
                           host_key=os.path.join(self.temp_dir.name, "host_key"),
                           idle_timeout_seconds=30, **extra)
        self.listener = await start_server(config)
        return self.listener.get_port()

    async def _open(self, port, username, password):
        connection = await asyncssh.connect("127.0.0.1", port=port, username=username,
                                            password=password, known_hosts=None)
        self.connections.append(connection)
        return connection, await connection.create_process(term_type="xterm")

    async def test_direct_reset_then_log_in_with_the_new_password(self):
        port = await self._listen()
        code = db_operations.create_password_reset_code(self.account_id, "!0a1b2c3d")
        _, process = await self._open(port, "reset:Colin", code)
        prompt = await process.stdout.readuntil("New password: ")
        self.assertIn("Reset code accepted for Colin", prompt)
        process.stdin.write("short\n")
        self.assertIn("10-128 characters", await process.stdout.readuntil("New password: "))
        process.stdin.write("brand-new-password\n")
        await process.stdout.readuntil("Confirm new password: ")
        process.stdin.write("brand-new-password\n")
        done = await process.stdout.read()
        self.assertIn("Password changed", done)
        await process.wait_closed()

        _, process = await self._open(port, "Colin", "brand-new-password")
        self.assertIn("Connected to Bacon BBS", await process.stdout.readuntil("> "))
        with self.assertRaises(asyncssh.PermissionDenied):
            await asyncssh.connect("127.0.0.1", port=port, username="reset:Colin",
                                   password=code, known_hosts=None)

    async def test_a_bad_code_never_opens_a_session(self):
        port = await self._listen()
        with self.assertRaises(asyncssh.PermissionDenied):
            await asyncssh.connect("127.0.0.1", port=port, username="reset:Colin",
                                   password="123456", known_hosts=None)

    async def test_the_new_password_is_never_echoed(self):
        port = await self._listen()
        code = db_operations.create_password_reset_code(self.account_id, "!0a1b2c3d")
        _, process = await self._open(port, "reset:Colin", code)
        await process.stdout.readuntil("New password: ")
        process.stdin.write("brand-new-password\n")
        echoed = await process.stdout.readuntil("Confirm new password: ")
        self.assertNotIn("brand-new-password", echoed)

    async def test_shared_gate_reset(self):
        port = await self._listen(username="bbs", password="shared-access-password")
        code = db_operations.create_password_reset_code(self.account_id, "!0a1b2c3d")
        _, process = await self._open(port, "bbs", "shared-access-password")
        banner = await process.stdout.readuntil("BBS username: ")
        self.assertIn("reset:<username>", banner)
        process.stdin.write("reset:Colin\n")
        await process.stdout.readuntil("Reset code from your linked radio: ")
        process.stdin.write(code + "\n")
        await process.stdout.readuntil("New password: ")
        process.stdin.write("gate-new-password\n")
        await process.stdout.readuntil("Confirm new password: ")
        process.stdin.write("gate-new-password\n")
        self.assertIn("Password changed", await process.stdout.read())
        self.assertIsNotNone(ssh_auth.authenticate("Colin", "gate-new-password", "9.9.9.9"))


if __name__ == "__main__":
    unittest.main()
