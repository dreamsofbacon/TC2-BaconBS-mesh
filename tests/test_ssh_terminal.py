"""SSH output: word wrap to the terminal, and colour for menus and signals.

Reported 2026-09-14: SSH did not word-wrap -- long lines broke mid-word at the
terminal's edge -- and SSH could use colour. Both belong to the SSH front end
only; radios must never see a wrapped line or an escape code.
"""
import asyncio
import os
import re
import types
import unittest

import asyncssh

import ssh_terminal as t
from ssh_server import BBSClientSession, SSHConfig, SessionLimiter


WELCOME = ("Welcome to the Bacon BBS System! it is a Lora Bulletin Board System "
           "designed to allow messages, mail, relay services, and games to be synced "
           "across multiple locations over the internet, or Lora communication.")


def plain(text):
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class WrapTests(unittest.TestCase):
    def test_long_lines_break_between_words(self):
        rows = t.wrap_line(WELCOME, 40)
        self.assertGreater(len(rows), 3)
        self.assertTrue(all(t.display_width(r) <= 40 for r in rows))
        self.assertEqual(" ".join(rows), WELCOME)

    def test_short_lines_are_untouched(self):
        self.assertEqual(t.wrap_line("[1] Mail", 40), ["[1] Mail"])

    def test_indentation_carries_onto_continuation_rows(self):
        rows = t.wrap_line("    " + "word " * 20, 30)
        self.assertTrue(all(r.startswith("    ") for r in rows))

    def test_a_word_longer_than_a_row_is_split(self):
        rows = t.wrap_line("x" * 70, 30)
        self.assertEqual([len(r) for r in rows], [30, 30, 10])

    def test_emoji_count_as_two_columns(self):
        self.assertEqual(t.display_width("🥓"), 2)
        self.assertEqual(t.display_width("✉️"), 1)   # base + variation selector
        rows = t.wrap_line("🥓 " * 30, 21)
        self.assertTrue(all(t.display_width(r) <= 21 for r in rows))

    def test_blank_lines_survive(self):
        self.assertEqual(t.render("a\n\nb", 40), "a\r\n\r\nb")


class ColourTests(unittest.TestCase):
    def test_menu_titles(self):
        for title in ("💾Bacon BBS💾 (✉️:9)", "📰BBS Menu📰", "🎮 Games 🎮", "✉️Mail Menu✉️"):
            with self.subTest(title=title):
                self.assertTrue(t.colour_line(title).startswith(t.TITLE))

    def test_option_keys_only(self):
        line = t.colour_line("[1]Read [2]Send [0]Back")
        self.assertEqual(line.count(t.OPTION), 3)
        self.assertEqual(plain(line), "[1]Read [2]Send [0]Back")
        self.assertIn(t.OPTION + "[S]" + t.RESET + "cores", t.colour_line("[S]cores"))
        self.assertIn(t.OPTION + "[1-4]", t.colour_line("[1-4] Write [0] Back"))

    def test_tips_are_dim_and_errors_red(self):
        self.assertTrue(t.colour_line("Tip: Read opens your inbox.").startswith(t.TIP))
        for error in ("Invalid choice.", "[ERR] AI model is not installed",
                      "Mail not found. Reply 0 to go back.", "Too many attempts."):
            with self.subTest(error=error):
                self.assertTrue(t.colour_line(error).startswith(t.ERROR))

    def test_user_text_that_mentions_failure_is_not_red(self):
        """Only lines that start like a BBS error."""
        self.assertFalse(t.colour_line("The test failed but it was fine").startswith(t.ERROR))
        self.assertFalse(t.colour_line("Hello, not found yet").startswith(t.ERROR))

    def test_plain_lines_are_left_alone(self):
        self.assertEqual(t.colour_line("From: 🥓"), "From: 🥓")

    def test_colour_does_not_count_towards_width(self):
        """Wrapped first, coloured after: the escape codes are not columns."""
        text = "[1] " + "option " * 12
        rows = t.render(text, 30, colour=True).split("\r\n")
        self.assertTrue(all(t.display_width(plain(r)) <= 30 for r in rows))

    def test_dumb_terminals_get_no_colour(self):
        self.assertFalse(t.wants_colour("dumb"))
        self.assertFalse(t.wants_colour(""))
        self.assertTrue(t.wants_colour("xterm-256color"))


class SessionTests(unittest.TestCase):
    def session(self):
        session = BBSClientSession(None, SSHConfig(), SessionLimiter(5, 1), "127.0.0.1")
        written = []
        session.channel = types.SimpleNamespace(write=written.append)
        return session, written

    def test_output_wraps_to_the_clients_terminal(self):
        session, written = self.session()
        session.pty_requested("xterm", (41, 24, 0, 0), {})
        session._write_chunks([{"text": WELCOME}])
        rows = plain(written[0]).split("\r\n")
        self.assertTrue(all(t.display_width(r) <= 40 for r in rows if r))
        self.assertGreater(len(rows), 3)

    def test_a_resized_window_rewraps(self):
        session, written = self.session()
        session.pty_requested("xterm", (120, 24, 0, 0), {})
        session.terminal_size_changed(31, 24, 0, 0)
        session._write_chunks([{"text": WELCOME}])
        self.assertTrue(all(t.display_width(r) <= 30
                            for r in plain(written[0]).split("\r\n") if r))

    def test_colour_follows_the_terminal_type(self):
        session, written = self.session()
        session.pty_requested("xterm", (80, 24, 0, 0), {})
        session._write_chunks([{"text": "📰BBS Menu📰\n[1] Mail"}])
        self.assertIn(t.TITLE, written[0])
        session, written = self.session()
        session.pty_requested("dumb", (80, 24, 0, 0), {})
        session._write_chunks([{"text": "📰BBS Menu📰\n[1] Mail"}])
        self.assertNotIn("\x1b", written[0])

    def test_no_pty_means_no_colour_and_80_columns(self):
        session, written = self.session()
        session._write_chunks([{"text": WELCOME}])
        self.assertNotIn("\x1b", written[0])
        self.assertTrue(all(t.display_width(r) <= 80 for r in written[0].split("\r\n")))

    def test_the_bbs_text_itself_is_unchanged_for_radios(self):
        """Wrapping and colour happen in ssh_server only."""
        import inspect
        import utils
        self.assertNotIn("ssh_terminal", inspect.getsource(utils))


if __name__ == "__main__":
    unittest.main()


class ServerOwnLinesWrapTests(unittest.IsolatedAsyncioTestCase):
    """Found in the field, 2026-09-17: on a 60-column terminal the connection
    banner ran to 74 characters. Everything the BBS itself sends was wrapped;
    the few lines the SSH front end writes on its own account were not."""

    async def asyncSetUp(self):
        import sqlite3
        import tempfile
        import db_operations
        from ssh_server import SSHConfig, start_server
        self.temp_dir = tempfile.TemporaryDirectory()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.listener = await start_server(SSHConfig(
            enabled=True, host="127.0.0.1", port=0,
            host_key=os.path.join(self.temp_dir.name, "host_key"),
            idle_timeout_seconds=30))
        self.connections = []

    async def asyncTearDown(self):
        import bbs_emulator
        import db_operations
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

    async def _opening(self, width):
        connection = await asyncssh.connect(
            "127.0.0.1", port=self.listener.get_port(), username=f"new:Caller{width}",
            password="long-enough-password", known_hosts=None)
        self.connections.append(connection)
        process = await connection.create_process(term_type="xterm", term_size=(width, 24))
        return await process.stdout.readuntil("> ")

    async def test_the_registration_and_connection_lines_fit_the_terminal(self):
        for width in (60, 100):
            opening = re.sub(r"\x1b\[[0-9;]*m", "", await self._opening(width))
            longest = max(len(line) for line in opening.replace("\r", "").split("\n"))
            with self.subTest(width=width):
                self.assertLessEqual(longest, width,
                                     f"a line ran to {longest} on a {width}-column terminal")
            self.assertIn("Connected to Bacon BBS", opening)
