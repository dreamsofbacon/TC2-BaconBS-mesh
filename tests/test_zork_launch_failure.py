"""_launch_game() used to claim success no matter what.

start_zork_session() and resume_zork_session() already return a genuine,
informative error string on every real failure mode -- no interpreter
installed, the story file missing with autodownload off, the interpreter
process failing to spawn, or a live session having died since it was last
checked. _launch_game() showed that text and then, unconditionally, piled
a false "Zork I started. Send commands..." (or "resumed"/"restored") right
on top of it, and parked the session in ZORK state regardless -- so the
next command a player sent hit "No active game session. Go to the Games
menu to start a game." with no link back to the error they'd just been
shown.

has_zork_session() is the ground truth here, not the returned text: a
real success always populates the session registry before returning
anything, so checking it again right after the call distinguishes a
genuine launch from a failure without depending on the exact wording of
whatever start_zork_session() happened to say.
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

import command_handlers as ch
import db_operations

SENDER = 424242


class _Iface:
    def __init__(self):
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = {}


def _sent(mock_send_message):
    return [call.args[0] for call in mock_send_message.call_args_list]


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()
        ch.update_user_state(SENDER, {'command': 'GAMES_MENU', 'step': 1})

    def tearDown(self):
        ch.update_user_state(SENDER, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection


class FreshStartFailureTests(_Case):
    def test_a_missing_interpreter_does_not_claim_the_game_started(self):
        error = ("No Z-machine interpreter found.\nTried: dfrotz, frotz\n"
                "Install frotz/dfrotz, or set [zork] interpreter in config.ini.")
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, False]), \
                mock.patch.object(ch, "has_zork_save", return_value=False), \
                mock.patch.object(ch, "start_zork_session", return_value=error), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""), \
                mock.patch.object(ch, "handle_games_command") as games_menu:
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        texts = _sent(sm)
        self.assertIn(error, texts)
        self.assertFalse(any("started" in t for t in texts))
        games_menu.assert_called_once_with(SENDER, self.iface)

    def test_a_missing_interpreter_does_not_leave_the_session_in_zork_state(self):
        with mock.patch.object(ch, "send_message"), \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, False]), \
                mock.patch.object(ch, "has_zork_save", return_value=False), \
                mock.patch.object(ch, "start_zork_session", return_value="No interpreter."), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""), \
                mock.patch.object(ch, "handle_games_command"):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        self.assertEqual(ch.get_user_state(SENDER), {'command': 'GAMES_MENU', 'step': 1})

    def test_a_genuine_fresh_start_still_claims_success(self):
        """The fix must not turn a real launch into a false failure."""
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, True]), \
                mock.patch.object(ch, "has_zork_save", return_value=False), \
                mock.patch.object(ch, "start_zork_session", return_value="West of House"), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        texts = _sent(sm)
        self.assertTrue(any("started" in t for t in texts))
        self.assertEqual(ch.get_user_state(SENDER),
                         {'command': 'ZORK', 'step': 1, 'game_id': 'zork1'})


class RestoreFromSaveFailureTests(_Case):
    def test_a_failed_restore_does_not_claim_the_save_was_restored(self):
        error = "Story file not found at 'data/zork1.z3'.\nSet BBS_ZORK_AUTODOWNLOAD=true to auto-download."
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, False]), \
                mock.patch.object(ch, "has_zork_save", return_value=True), \
                mock.patch.object(ch, "start_zork_session", return_value=error), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""), \
                mock.patch.object(ch, "handle_games_command") as games_menu:
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        texts = _sent(sm)
        self.assertIn(error, texts)
        self.assertFalse(any("restored" in t for t in texts))
        games_menu.assert_called_once_with(SENDER, self.iface)

    def test_a_genuine_restore_still_claims_success(self):
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, True]), \
                mock.patch.object(ch, "has_zork_save", return_value=True), \
                mock.patch.object(ch, "start_zork_session", return_value="Resuming: Deck Nine"), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        self.assertTrue(any("restored" in t for t in _sent(sm)))
        self.assertEqual(ch.get_user_state(SENDER),
                         {'command': 'ZORK', 'step': 1, 'game_id': 'zork1'})


class ResumeLiveSessionFailureTests(_Case):
    def test_a_session_that_died_since_the_last_check_does_not_claim_resumed(self):
        """has_zork_session() said True a moment ago (the sweep/idle check
        that decided which branch to take), but the process had already
        exited by the time resume_zork_session() actually looked -- it
        pops the dead entry and explains that in its own text. The bug
        was piling "Zork I resumed. Send X to exit." on top of that."""
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", side_effect=[True, False]), \
                mock.patch.object(ch, "resume_zork_session",
                                  return_value="Your previous session ended. "
                                              "Start a new one from Games."), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""), \
                mock.patch.object(ch, "handle_games_command") as games_menu:
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        texts = _sent(sm)
        self.assertIn("Your previous session ended. Start a new one from Games.", texts)
        self.assertFalse(any("resumed" in t for t in texts))
        games_menu.assert_called_once_with(SENDER, self.iface)

    def test_a_genuine_resume_still_claims_success(self):
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", side_effect=[True, True]), \
                mock.patch.object(ch, "resume_zork_session",
                                  return_value="Resuming:\n\nWest of House"), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        self.assertTrue(any("resumed" in t for t in _sent(sm)))
        self.assertEqual(ch.get_user_state(SENDER),
                         {'command': 'ZORK', 'step': 1, 'game_id': 'zork1'})


if __name__ == "__main__":
    unittest.main()
