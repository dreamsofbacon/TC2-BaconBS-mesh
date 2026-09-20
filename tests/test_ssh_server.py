import os
import asyncio
import sqlite3
import tempfile
import types
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
        self.connections = []
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
        # Close client connections before the listener. Left open, a failed
        # assertion mid-test stalled the whole run waiting on a session that
        # nothing would ever finish.
        for connection in self.connections:
            connection.close()
        for connection in self.connections:
            await asyncio.wait_for(connection.wait_closed(), timeout=5)
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
        self.connections.append(connection)
        process = await connection.create_process(term_type="xterm")
        return connection, process

    async def test_registration_and_returning_login_drive_real_bbs(self):
        connection, process = await self._open(
            "new:Caller", "long-enough-password")
        welcome = await process.stdout.readuntil("> ")
        self.assertIn("Account Caller created", welcome)
        self.assertIn("Bacon BBS", welcome)
        # Derived, not hardcoded. This used to type 3, which was Utilities
        # until Games and Public Chatter were inserted above it -- and then it
        # failed on every CI run while saying nothing about SSH at all. It
        # opens the BBS menu now that Utilities is gone.
        import command_handlers as ch
        items, title = ch.menu_items_for('main')
        bbs = {letter: digit for digit, letter in
               ch.menu_number_alias(items, title).items()}['b']
        wrong = '3' if bbs != '3' else '4'
        process.stdin.write(f"{wrong}\x7f{bbs}\n")
        bbs_menu = await process.stdout.readuntil("> ")
        self.assertIn("\b \b", bbs_menu)
        self.assertIn("BBS Menu", bbs_menu)
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

    async def test_an_auth_that_raises_is_refused_and_logged(self):
        """asyncssh swallows whatever these callbacks raise and logs nothing
        of its own once auth begins. On 2026-09-20 this service spent hours
        refusing every login while systemd reported it active, and the
        journal held "Beginning auth for user X" and then nothing -- a crash,
        a wrong password and a wedged process all looked identical."""
        from ssh_server import BBSSSHServer, SessionLimiter
        server = BBSSSHServer(self.config, SessionLimiter(3, 1))
        server.source_address = "198.51.100.7"
        with mock.patch("ssh_server.authenticate",
                        side_effect=RuntimeError("the database went away")), \
                self.assertLogs("root", level="ERROR") as logged:
            self.assertFalse(server.validate_password("someone", "a-password"))
        self.assertTrue(
            any("raised" in line for line in logged.output),
            "an auth crash was not logged")

    async def test_every_auth_outcome_is_logged(self):
        """So "it connected yesterday and not today" is answerable."""
        from ssh_server import BBSSSHServer, SessionLimiter
        server = BBSSSHServer(self.config, SessionLimiter(3, 1))
        server.source_address = "198.51.100.7"
        with mock.patch("ssh_server.authenticate", return_value=None), \
                self.assertLogs("root", level="INFO") as logged:
            self.assertFalse(server.validate_password("nobody", "a-password"))
        self.assertTrue(any("refused" in line for line in logged.output))

    async def test_a_session_refused_by_the_limiter_says_why(self):
        """The channel closes without a word, so the only place it can be
        explained is the log."""
        from ssh_server import BBSSSHServer, SessionLimiter
        limiter = SessionLimiter(1, 1)
        server = BBSSSHServer(self.config, limiter)
        server.source_address = "198.51.100.7"
        server.auth = types.SimpleNamespace(account_id="acct", alias="someone")
        self.assertTrue(limiter.reserve("someone-else"))
        with self.assertLogs("root", level="WARNING") as logged:
            self.assertFalse(server.session_requested())
        self.assertTrue(any("limit" in line for line in logged.output))

    async def _start_gated(self, name):
        """Restart the listener with a shared gate configured."""
        self.listener.close()
        await self.listener.wait_closed()
        self.config = SSHConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            host_key=os.path.join(self.temp_dir.name, name),
            username="bbs",
            password="shared-access-password",
            max_sessions=3,
            max_sessions_per_account=1,
            idle_timeout_seconds=30,
        )
        self.listener = await start_server(self.config)
        self.port = self.listener.get_port()

    async def test_a_configured_gate_does_not_lock_out_account_logins(self):
        """The live failure: setting [ssh] username/password returned from
        validate_password before per-account auth was ever reached, so every
        account login and every new: registration was refused while the gate
        was set. The operator wanted the gate available; what they got was
        the gate being the only way in, with nobody able to use their own
        name."""
        await self._start_gated("gate_plus_accounts_key")

        # Registration, with the gate configured.
        connection, process = await self._open(
            "new:DirectCaller", "long-enough-password")
        welcome = await process.stdout.readuntil("> ")
        self.assertIn("Account DirectCaller created", welcome)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

        # ...and logging straight back in as that account, no gate involved.
        connection, process = await self._open(
            "DirectCaller", "long-enough-password")
        welcome = await process.stdout.readuntil("> ")
        self.assertIn("Bacon BBS", welcome)
        self.assertNotIn("BBS username: ", welcome,
                         "an account login was sent through the gate's prompt")
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

    async def test_the_gate_still_works_alongside_accounts(self):
        """Additive means both, so the option is really an option."""
        await self._start_gated("gate_still_open_key")
        connection, process = await self._open("bbs", "shared-access-password")
        prompt = await process.stdout.readuntil("BBS username: ")
        self.assertIn("Register or log in", prompt)
        process.stdin.write_eof()
        await process.wait_closed()
        connection.close()
        await connection.wait_closed()

    async def test_a_wrong_password_is_still_refused_with_a_gate_set(self):
        """Falling through to account auth must not fall through to letting
        anyone in."""
        await self._start_gated("gate_wrong_password_key")
        for username, password in (("bbs", "wrong-access-password"),
                                   ("NoSuchAccount", "long-enough-password")):
            with self.subTest(username=username):
                with self.assertRaises(asyncssh.PermissionDenied):
                    await asyncssh.connect(
                        "127.0.0.1", port=self.port, username=username,
                        password=password, known_hosts=None)

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
