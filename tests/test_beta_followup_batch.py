"""Four more small screens from the same 2026-09-07 beta report, addressed
after the polish batch and the Planetfall investigation:

- The channel-creation URL/PSK prompt gave no format example and never
  said whether the field was optional.
- Scoreboard detail (Games -> Scores -> a specific title) had no [0] Back
  hint, even though 0 worked.
- !CHL's channel detail screen had the identical gap.
- Public Chatter's filter screen claims "(* = shown)" but starred nothing
  when the feed was actually showing every channel unfiltered -- read as
  "everything is hidden" on its own legend.

Each test asserts on the text actually sent, per this project's own
standing rule against testing the mechanism and never checking the
message.
"""
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import command_handlers as ch


def _sent(mock_send_message):
    return [call.args[0] for call in mock_send_message.call_args_list]


class ChannelUrlPskPromptTests(unittest.TestCase):
    def test_the_prompt_gives_a_format_example(self):
        state = {'command': 'CHANNEL_DIRECTORY', 'step': 3}
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_channel_directory_steps(111, "Test Channel", 3, state, None)
        prompt = _sent(sm)[0]
        self.assertIn("meshtastic.org/e", prompt)
        self.assertIn("PSK", prompt)

    def test_the_prompt_says_it_is_not_required(self):
        state = {'command': 'CHANNEL_DIRECTORY', 'step': 3}
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_channel_directory_steps(111, "Test Channel", 3, state, None)
        self.assertIn("Not required", _sent(sm)[0])

    def test_the_prompt_still_advertises_cancel(self):
        state = {'command': 'CHANNEL_DIRECTORY', 'step': 3}
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_channel_directory_steps(111, "Test Channel", 3, state, None)
        self.assertIn(ch.CANCEL_HINT, _sent(sm)[0])


class ScoreboardBackHintTests(unittest.TestCase):
    def test_a_populated_scoreboard_shows_a_back_hint(self):
        game_id, info = ch.GAME_LIST[1]  # any real, scoreable title
        with mock.patch.object(ch, "get_game_scoreboard",
                               return_value=[("Ada", 100, 350, 12)]), \
                mock.patch.object(ch, "send_message") as sm:
            ch.handle_scoreboard_steps(111, str(ch.GAME_LIST.index((game_id, info)) + 1),
                                       None)
        self.assertIn("[0] Back", _sent(sm)[0])

    def test_an_empty_scoreboard_also_shows_a_back_hint(self):
        game_id, info = ch.GAME_LIST[1]
        with mock.patch.object(ch, "get_game_scoreboard", return_value=[]), \
                mock.patch.object(ch, "send_message") as sm:
            ch.handle_scoreboard_steps(111, str(ch.GAME_LIST.index((game_id, info)) + 1),
                                       None)
        self.assertIn("[0] Back", _sent(sm)[0])

    def test_zero_still_returns_to_games_afterward(self):
        """The hint has to point at something real -- 0 already worked
        per the beta report, this just makes it discoverable."""
        with mock.patch.object(ch, "handle_games_command") as gc:
            ch.handle_scoreboard_steps(111, "0", None)
        gc.assert_called_once()


class ChannelDetailBackHintTests(unittest.TestCase):
    def test_the_detail_screen_shows_a_back_hint(self):
        state = {'channels': [("Test Channel", "https://meshtastic.org/e/#abc")]}
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_read_channel_command(111, "1", state, None)
        self.assertIn("[0] Back", _sent(sm)[0])

    def test_the_channel_name_and_url_are_still_shown(self):
        state = {'channels': [("Test Channel", "https://meshtastic.org/e/#abc")]}
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_read_channel_command(111, "1", state, None)
        screen = _sent(sm)[0]
        self.assertIn("Test Channel", screen)
        self.assertIn("https://meshtastic.org/e/#abc", screen)


class ChatterFilterStarTests(unittest.TestCase):
    def test_nothing_chosen_stars_every_channel(self):
        """No filter means every channel is actually shown -- the exact
        beta-report complaint was that this state starred nothing on a
        screen whose own legend says * means shown."""
        state = {
            'filter_options': [
                {'value': 'MC/#general', 'label': 'MC/#general'},
                {'value': 'MC/#wardriving', 'label': 'MC/#wardriving'},
            ],
            'channels': [],
        }
        screen = ch._chatter_filter_text(state)
        self.assertIn("[1]*MC/#general", screen)
        self.assertIn("[2]*MC/#wardriving", screen)

    def test_choosing_one_channel_unstars_the_rest(self):
        state = {
            'filter_options': [
                {'value': 'MC/#general', 'label': 'MC/#general'},
                {'value': 'MC/#wardriving', 'label': 'MC/#wardriving'},
            ],
            'channels': ['MC/#wardriving'],
        }
        screen = ch._chatter_filter_text(state)
        self.assertIn("[1] MC/#general", screen)
        self.assertIn("[2]*MC/#wardriving", screen)

    def test_the_legend_still_reads_star_equals_shown(self):
        state = {'filter_options': [{'value': 'a', 'label': 'a'}], 'channels': []}
        self.assertIn("(* = shown)", ch._chatter_filter_text(state))


if __name__ == "__main__":
    unittest.main()
