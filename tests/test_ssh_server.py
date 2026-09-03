import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import asyncssh

import bbs_emulator
import db_operations
from ssh_server import SSHConfig, SessionLimiter, load_config, start_server


class SSHConfigTests(unittest.TestCase):
    def test_missing_section_is_disabled_and_local_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            with open(config_path, "w", encoding="utf-8") as config_file:
                config_file.write("[interface]\ntype = serial\n")
            config = load_config(config_path)
        self.assertFalse(config.enabled)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 2222)

    def test_explicit_section_loads_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            with open(config_path, "w", encoding="utf-8") as config_file:
                config_file.write(
                    "[ssh]\nenabled = true\nhost = 0.0.0.0\nport = 2200\n"
                    "username = bbs\npassword = access-pass\n"
                    "max_sessions = 8\nmax_sessions_per_account = 1\n")
            config = load_config(config_path)
        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 2200)
        self.assertEqual(config.username, "bbs")
        self.assertEqual(config.password, "access-pass")
        self.assertEqual(config.max_sessions, 8)
        self.assertEqual(config.max_sessions_per_account, 1)


class SessionLimiterTests(unittest.TestCase):
    def test_enforces_account_and_total_limits(self):
        limiter = SessionLimiter(total_limit=2, account_limit=1)
        self.assertTrue(limiter.reserve("account-a"))
        self.assertFalse(limiter.reserve("account-a"))
        self.assertTrue(limiter.reserve("account-b"))
        self.assertFalse(limiter.reserve("account-c"))
        limiter.release("account-a")
        self.assertTrue(limiter.reserve("account-c"))

    def test_pending_session_can_be_promoted_to_an_account(self):
        limiter = SessionLimiter(total_limit=1, account_limit=1)
        self.assertTrue(limiter.reserve_pending())
        self.assertFalse(limiter.reserve_pending())
        self.assertTrue(limiter.promote_pending("account-a"))
        self.assertFalse(limiter.promote_pending("account-a"))
        limiter.release("account-a")
        self.assertTrue(limiter.reserve_pending())
        limiter.release_pending()


class SSHBindTests(unittest.IsolatedAsyncioTestCase):
    @mock.patch("ssh_server.ensure_host_key", return_value="host-key")
    @mock.patch("ssh_server.asyncssh.create_server", new_callable=mock.AsyncMock)
    async def test_multiple_bind_addresses_are_passed_as_a_list(
            self, create_server, _ensure_host_key):
        await start_server(SSHConfig(host="0.0.0.0, ::"))
        self.assertEqual(create_server.await_args.args[1], ["0.0.0.0", "::"])
        self.assertEqual(create_server.await_args.args[2], 2222)


class SSHServerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.config = SSHConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            host_key=os.path.join(self.temp_dir.name, "host_key"),
            max_sessions=3,
            max_sessions_per_account=1,
            idle_timeout_seconds=30,
        )
        self.listener = await start_server(self.config)
        self.port = self.listener.get_port()

    async def asyncTearDown(self):
        self.listener.close()
        await self.listener.wait_closed()
        for token in list(bbs_emulator._sessions):
            bbs_emulator.end_session(token)
        connection = getattr(db_operations.thread_local, "connection", None)
        if connection is not None:
            connection.close()
            del db_operations.thread_local.connection
        self.temp_dir.cleanup()

    async def _open(self, username, password):
        connection = await asyncssh.connect(
            "127.0.0.1", port=self.port, username=username,
            password=password, known_hosts=None)
        process = await connection.create_process(term_type="xterm")
        return connection, process

    async def test_registration_and_returning_login_drive_real_bbs(self):
        connection, process = await self._open(
            "new:Caller", "long-enough-password")
        welcome = await process.stdout.readuntil("> ")
        self.assertIn("Account Caller created", welcome)
        self.assertIn("Bacon BBS", welcome)
        process.stdin.write("2\x7f3\n")
        utilities = await process.stdout.readuntil("> ")
        self.assertIn("\b \b", utilities)
        self.assertIn("Utilities Menu", utilities)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

        connection, process = await self._open(
            "caller", "long-enough-password")
        welcome = await process.stdout.readuntil("> ")
        self.assertNotIn("Account Caller created", welcome)
        self.assertIn("Bacon BBS", welcome)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

    async def test_shared_access_registration_reprompts_on_password_mismatch(self):
        self.listener.close()
        await self.listener.wait_closed()
        self.config = SSHConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            host_key=os.path.join(self.temp_dir.name, "gated_mismatch_key"),
            username="bbs",
            password="shared-access-password",
            idle_timeout_seconds=30,
        )
        self.listener = await start_server(self.config)
        self.port = self.listener.get_port()

        connection, process = await self._open("bbs", "shared-access-password")
        await process.stdout.readuntil("BBS username: ")
        process.stdin.write("MismatchUser\n")
        await process.stdout.readuntil("Create password: ")
        process.stdin.write("first-password\n")
        await process.stdout.readuntil("Confirm password: ")
        process.stdin.write("second-password\n")
        retry = await process.stdout.readuntil("Create password: ")
        self.assertIn("Passwords do not match", retry)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

    async def test_shared_access_unknown_user_respects_disabled_registration(self):
        self.listener.close()
        await self.listener.wait_closed()
        self.config = SSHConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            host_key=os.path.join(self.temp_dir.name, "gated_disabled_key"),
            username="bbs",
            password="shared-access-password",
            registration_enabled=False,
            idle_timeout_seconds=30,
        )
        self.listener = await start_server(self.config)
        self.port = self.listener.get_port()

        connection, process = await self._open("bbs", "shared-access-password")
        await process.stdout.readuntil("BBS username: ")
        process.stdin.write("UnknownUser\n")
        response = await process.stdout.readuntil("BBS username: ")
        self.assertIn("registration is disabled", response)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

    async def test_unknown_alias_cannot_register_without_new_prefix(self):
        with self.assertRaises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1", port=self.port, username="TypoUser",
                password="long-enough-password", known_hosts=None)

    async def test_shared_access_requires_account_auth_after_connection(self):
        self.listener.close()
        await self.listener.wait_closed()
        self.config = SSHConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            host_key=os.path.join(self.temp_dir.name, "gated_host_key"),
            username="bbs",
            password="shared-access-password",
            max_sessions=3,
            max_sessions_per_account=1,
            idle_timeout_seconds=30,
        )
        self.listener = await start_server(self.config)
        self.port = self.listener.get_port()

        with self.assertRaises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1", port=self.port, username="bbs",
                password="wrong-access-password", known_hosts=None)

        connection, process = await self._open("bbs", "shared-access-password")
        prompt = await process.stdout.readuntil("BBS username: ")
        self.assertIn("Register or log in", prompt)
        process.stdin.write("GatedCaller\n")
        registration = await process.stdout.readuntil("Create password: ")
        self.assertIn("New account", registration)
        process.stdin.write("account-password\n")
        await process.stdout.readuntil("Confirm password: ")
        process.stdin.write("account-password\n")
        welcome = await process.stdout.readuntil("> ")
        self.assertNotIn("account-password", welcome)
        self.assertIn("Account GatedCaller created", welcome)
        self.assertIn("Bacon BBS", welcome)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

        connection, process = await self._open("bbs", "shared-access-password")
        await process.stdout.readuntil("BBS username: ")
        process.stdin.write("gatedcaller\n")
        login_prompt = await process.stdout.readuntil("BBS password: ")
        self.assertNotIn("New account", login_prompt)
        process.stdin.write("account-password\n")
        welcome = await process.stdout.readuntil("> ")
        self.assertNotIn("Account GatedCaller created", welcome)
        self.assertIn("Bacon BBS", welcome)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()


if __name__ == "__main__":
    unittest.main()
