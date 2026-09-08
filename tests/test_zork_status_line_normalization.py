"""A brand-new game should always open at Score: 0, Moves: 0 -- there is no
save, no restore, no player input yet for it to report anything else. On
the live node's dfrotz build, Planetfall's own opening status line showed a
different nonzero Moves figure on every single launch of the exact same
byte-for-byte story file: 4596, 4514, 4470, 4599, confirmed even invoking
dfrotz bare on the command line with no Python involved at all. That ruled
out a save/session/account bug entirely -- it's the interpreter's own
opening status line for that title, and we were putting it in front of a
player unfiltered, reading exactly like someone else's save had loaded.

These tests cover the fix (_zeroed_fresh_status_line) both as a pure string
function and wired into start_zork_session, checking specifically that a
restore's real, legitimate move count is never touched by it.
"""
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import zork_port


class NormalizeStatusLineTests(unittest.TestCase):
    def test_zeroes_a_garbage_moves_figure(self):
        text = (" Deck Nine                    Score: 0        Moves: 4599\n\n"
                "PLANETFALL\n...")
        out = zork_port._zeroed_fresh_status_line(text)
        self.assertIn("Moves: 0", out)
        self.assertNotIn("4599", out)

    def test_zeroes_a_garbage_score_too(self):
        text = "West of House          Score: 47        Moves: 12\n"
        out = zork_port._zeroed_fresh_status_line(text)
        self.assertIn("Score: 0", out)
        self.assertIn("Moves: 0", out)

    def test_handles_a_negative_score(self):
        text = "Some Room          Score: -10        Moves: 3\n"
        out = zork_port._zeroed_fresh_status_line(text)
        self.assertIn("Score: 0", out)
        self.assertNotIn("-10", out)

    def test_leaves_a_time_based_status_line_alone(self):
        """Deadline shows a clock, not a turn counter -- that's the story's
        actual premise, not interpreter noise, and must not be zeroed."""
        text = "Mr. Robner's Study          Time: 9:04 am\n"
        out = zork_port._zeroed_fresh_status_line(text)
        self.assertIn("Time: 9:04 am", out)

    def test_only_the_first_matching_line_is_touched(self):
        """A later, in-story mention of a score/moves-shaped phrase (in
        room text, dialogue, whatever) must survive untouched -- only the
        interpreter's own opening status line is ever suspect."""
        text = ("Status Score: 99        Moves: 55\n"
                "\n"
                "A plaque on the wall reads: Score: 100 Moves: 1\n")
        out = zork_port._zeroed_fresh_status_line(text)
        lines = out.split("\n")
        self.assertIn("Score: 0", lines[0])
        self.assertIn("Moves: 0", lines[0])
        self.assertIn("Score: 100 Moves: 1", lines[2])

    def test_text_with_no_status_line_is_returned_unchanged(self):
        text = "Failed to start game: no such file"
        self.assertEqual(zork_port._zeroed_fresh_status_line(text), text)

    def test_empty_text_is_returned_unchanged(self):
        self.assertEqual(zork_port._zeroed_fresh_status_line(""), "")


class _FakeProcess:
    """Enough of subprocess.Popen's surface for ZorkSession to drive."""

    def __init__(self, output_text):
        import io
        self.stdin = io.StringIO()
        self.stdout = iter(line + "\n" for line in output_text.split("\n"))
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self._returncode = -9


class StartZorkSessionIntegrationTests(unittest.TestCase):
    """Through the real start_zork_session, with a stubbed interpreter
    process -- proves the mask is actually wired in, not just defined."""

    def setUp(self):
        zork_port._sessions.clear()

    def tearDown(self):
        zork_port._sessions.clear()

    def test_a_fresh_launch_shows_zeroed_moves_not_the_raw_figure(self):
        garbage = (" Deck Nine          Score: 0        Moves: 4599\n"
                  "\nPLANETFALL\nAnother routine day...")
        with mock.patch.object(zork_port, "_get_interpreter_command",
                               return_value=["dfrotz"]), \
                mock.patch.object(zork_port, "_ensure_story_file",
                                  return_value=(True, "data/planetfall.z3")), \
                mock.patch("subprocess.Popen", return_value=_FakeProcess(garbage)), \
                mock.patch.object(zork_port, "get_zork_save", return_value=None):
            intro = zork_port.start_zork_session(999001, "planetfall")
        self.assertIn("Moves: 0", intro)
        self.assertNotIn("4599", intro)

    def test_a_restored_saves_real_move_count_is_never_touched(self):
        """The mask only ever applies to the pre-restore banner, which a
        real restore discards in favor of the post-restore look output --
        proving that discard, not just the masking function in isolation."""
        banner = " Deck Nine          Score: 0        Moves: 4599\n\nPLANETFALL\n..."
        with mock.patch.object(zork_port, "_get_interpreter_command",
                               return_value=["dfrotz"]), \
                mock.patch.object(zork_port, "_ensure_story_file",
                                  return_value=(True, "data/planetfall.z3")), \
                mock.patch("subprocess.Popen", return_value=_FakeProcess(banner)), \
                mock.patch.object(zork_port, "get_zork_save",
                                  return_value=b"fake-save-bytes"), \
                mock.patch.object(zork_port, "_temp_save_file_path",
                                  return_value="/tmp/does-not-matter.qzl"), \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch("os.path.exists", return_value=False), \
                mock.patch.object(zork_port.ZorkSession, "read_output",
                                  side_effect=[
                                      zork_port._zeroed_fresh_status_line(banner),  # intro
                                      "restored.",  # raw restore-command ack
                                      "Deck Nine          Score: 0        Moves: 812\n"
                                      "You are standing where you left off.",  # look
                                  ]):
            intro = zork_port.start_zork_session(999002, "planetfall")
        # The restored session's own real, legitimate move count survives.
        self.assertIn("Moves: 812", intro)


class LaunchTextRespectsTheCallersTransportTests(unittest.TestCase):
    """start_zork_session's launch text -- the intro, or the restore/look
    output for a saved game -- used to always fall back to the flat
    900-char radio ceiling, regardless of max_chars, because nothing
    calling it ever passed one. send_zork_command already scales this to
    the caller's transport per turn; only the very first message a player
    ever sees from a game did not, which meant a long opening scene (like
    Planetfall's, several times a short Zork I opening) was truncated over
    SSH -- 8192 bytes per message there -- for no reason ordinary play
    already avoided."""

    def setUp(self):
        zork_port._sessions.clear()

    def tearDown(self):
        zork_port._sessions.clear()

    def test_a_fresh_intro_is_not_cut_at_the_flat_radio_ceiling_when_given_room(self):
        long_intro = "Deck Nine Score: 0 Moves: 0\n" + ("x" * 1200)
        with mock.patch.object(zork_port, "_get_interpreter_command",
                               return_value=["dfrotz"]), \
                mock.patch.object(zork_port, "_ensure_story_file",
                                  return_value=(True, "data/planetfall.z3")), \
                mock.patch("subprocess.Popen", return_value=_FakeProcess(long_intro)), \
                mock.patch.object(zork_port, "get_zork_save", return_value=None):
            intro = zork_port.start_zork_session(
                999003, "planetfall", max_chars=8192)
        self.assertNotIn("[Output truncated]", intro)
        self.assertGreater(len(intro), zork_port.MAX_RESPONSE_CHARS)

    def test_a_fresh_intro_still_respects_a_tight_radio_budget(self):
        """The fix threads max_chars through -- it must not simply stop
        truncating altogether once a transport actually needs it."""
        long_intro = "Deck Nine Score: 0 Moves: 0\n" + ("x" * 1200)
        with mock.patch.object(zork_port, "_get_interpreter_command",
                               return_value=["dfrotz"]), \
                mock.patch.object(zork_port, "_ensure_story_file",
                                  return_value=(True, "data/planetfall.z3")), \
                mock.patch("subprocess.Popen", return_value=_FakeProcess(long_intro)), \
                mock.patch.object(zork_port, "get_zork_save", return_value=None):
            intro = zork_port.start_zork_session(
                999004, "planetfall", max_chars=160)
        self.assertIn("[Output truncated]", intro)
        self.assertLess(len(intro), 250)

    def test_with_no_max_chars_given_the_old_flat_radio_ceiling_still_applies(self):
        """Backward compatible for any caller that still doesn't pass one."""
        long_intro = "Deck Nine Score: 0 Moves: 0\n" + ("x" * 1200)
        with mock.patch.object(zork_port, "_get_interpreter_command",
                               return_value=["dfrotz"]), \
                mock.patch.object(zork_port, "_ensure_story_file",
                                  return_value=(True, "data/planetfall.z3")), \
                mock.patch("subprocess.Popen", return_value=_FakeProcess(long_intro)), \
                mock.patch.object(zork_port, "get_zork_save", return_value=None):
            intro = zork_port.start_zork_session(999005, "planetfall")
        self.assertIn("[Output truncated]", intro)

    def test_a_restore_and_its_look_output_also_get_the_wider_room(self):
        banner = "Deck Nine Score: 0 Moves: 0\n\nPLANETFALL\n..."
        long_look = "Deck Nine\n" + ("y" * 1200)
        with mock.patch.object(zork_port, "_get_interpreter_command",
                               return_value=["dfrotz"]), \
                mock.patch.object(zork_port, "_ensure_story_file",
                                  return_value=(True, "data/planetfall.z3")), \
                mock.patch("subprocess.Popen", return_value=_FakeProcess(banner)), \
                mock.patch.object(zork_port, "get_zork_save",
                                  return_value=b"fake-save-bytes"), \
                mock.patch.object(zork_port, "_temp_save_file_path",
                                  return_value="/tmp/does-not-matter.qzl"), \
                mock.patch("builtins.open", mock.mock_open()), \
                mock.patch("os.path.exists", return_value=False), \
                mock.patch.object(zork_port.ZorkSession, "read_output",
                                  side_effect=[
                                      zork_port._zeroed_fresh_status_line(banner),
                                      "restored.",
                                  ]) as read_mock, \
                mock.patch.object(zork_port.ZorkSession, "send",
                                  return_value=long_look) as send_mock:
            intro = zork_port.start_zork_session(
                999006, "planetfall", max_chars=8192)
        send_mock.assert_called_once_with("look", max_chars=8192)
        # The second read_output call is the restore handshake's own output
        # (the first is the pre-restore banner, always max_chars-scaled
        # already and covered by its own test above) -- this is the one
        # the fix specifically had to reach.
        self.assertEqual(read_mock.call_args_list[1].kwargs.get("max_chars"), 8192)
        self.assertNotIn("[Output truncated]", intro)
        self.assertGreater(len(intro), zork_port.MAX_RESPONSE_CHARS)


class LaunchCallSitesPassTheCallersTransportTests(unittest.TestCase):
    """Through _launch_game in command_handlers.py -- the fix only matters
    if the actual caller passes the transport's real limit, not just that
    zork_port.py is capable of accepting one."""

    def setUp(self):
        self.iface = types.SimpleNamespace(max_text_bytes=8192)

    def test_a_fresh_launch_passes_the_interfaces_own_limit(self):
        import command_handlers as ch
        with mock.patch.object(ch, "send_message"), \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, True]), \
                mock.patch.object(ch, "has_zork_save", return_value=False), \
                mock.patch.object(ch, "start_zork_session",
                                  return_value="You are here.") as start, \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""), \
                mock.patch.object(ch, "response_limit_for", return_value=8192) as limit:
            ch._launch_game(424243, self.iface, "planetfall", "Planetfall")
        limit.assert_called_once_with(self.iface)
        start.assert_called_once_with(424243, "planetfall", max_chars=8192)

    def test_a_restore_from_save_passes_the_interfaces_own_limit(self):
        import command_handlers as ch
        with mock.patch.object(ch, "send_message"), \
                mock.patch.object(ch, "has_zork_session", side_effect=[False, True]), \
                mock.patch.object(ch, "has_zork_save", return_value=True), \
                mock.patch.object(ch, "start_zork_session",
                                  return_value="Restored.") as start, \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""), \
                mock.patch.object(ch, "response_limit_for", return_value=8192) as limit:
            ch._launch_game(424244, self.iface, "planetfall", "Planetfall")
        limit.assert_called_once_with(self.iface)
        start.assert_called_once_with(424244, "planetfall", max_chars=8192)


if __name__ == "__main__":
    unittest.main()
