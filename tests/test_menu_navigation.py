"""Tests for discoverable main-menu actions and consistent navigation.

Account linking and web fetches are direct main-menu actions even when an
existing config.ini predates those entries.
"""
import sqlite3
import sys
import types
import re
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import command_handlers as ch


class _FakeInterface:
    nodes = {"!abc": {"num": 1234, "user": {"id": "!abc"}}}


class MainMenuContentsTests(unittest.TestCase):
    MAIN = "\U0001F4BEBacon BBS\U0001F4BE"

    def test_new_entries_appear_even_for_a_config_written_before_them(self):
        """The exact reason these were invisible: config.ini pins the item
        list, so a new entry would never show up on an upgraded node."""
        rendered = ch.build_menu(["Q", "B", "U", "P", "N", "X"], self.MAIN)
        self.assertIn("Games", rendered)
        self.assertIn("Public Chatter", rendered)
        self.assertIn("Web Fetch", rendered)
        self.assertIn("Settings", rendered)
        self.assertIn("Node View", rendered)

    def test_a_full_config_numbers_every_entry_in_order(self):
        """Games and Public Chatter land right after BBS, not at the tail
        with the rest of MENU_REQUIRED -- see MENU_REQUIRED_AFTER."""
        rendered = ch.build_menu(["Q", "B", "U", "P", "N", "X"], self.MAIN)
        for expected in ("[1] Quick Commands", "[2] BBS", "[3] Games",
                         "[4] Public Chatter", "[5] Utilities", "[6] Profile",
                         "[7] Ask Nomad", "[8] Web Fetch", "[9] Settings",
                         "[10] Node View", "[0] Exit"):
            self.assertIn(expected, rendered)

    def test_a_trimmed_config_closes_the_gap_instead_of_skipping_numbers(self):
        """The baconbot case: a config written before Profile, Ask Nomad,
        Web Fetch, Settings, Node View, Games and Public Chatter existed.
        The menu used to read [1][2][3][6][7] -- holes that mean nothing to
        someone who just found the BBS. None of these seven can be hidden
        by an old or trimmed config any more; every one is either in
        MENU_REQUIRED or MENU_REQUIRED_AFTER."""
        rendered = ch.build_menu(["Q", "B", "U", "X"], self.MAIN)
        for expected in ("[3] Games", "[4] Public Chatter", "[6] Profile",
                         "[7] Ask Nomad", "[8] Web Fetch", "[9] Settings",
                         "[10] Node View"):
            self.assertIn(expected, rendered)

    def test_numbers_run_1_upward_with_no_gaps_for_any_config(self):
        for items in (["Q", "B", "U", "X"], ["Q", "X"], ["Q", "B", "U", "P", "N", "A", "S", "X"],
                      ["B", "U"], ["U", "X", "Q"]):
            with self.subTest(items=items):
                numbers = [line.split("]")[0][1:]
                           for line in ch.build_menu(items, self.MAIN).splitlines()[1:]
                           if line.strip()]
                body = [n for n in numbers if n != "0"]
                self.assertEqual(body, [str(n) for n in range(1, len(body) + 1)])

    def test_exit_is_always_last_wherever_the_config_put_it(self):
        lines = [l for l in ch.build_menu(["Q", "X", "B"], self.MAIN).splitlines() if l.strip()]
        self.assertTrue(lines[-1].startswith("[0] Exit"))

    def test_a_menu_without_exit_renders_none(self):
        rendered = ch.build_menu(["Q", "B"], self.MAIN)
        self.assertNotIn("[0]", rendered)

    def test_an_unknown_config_letter_never_claims_a_number(self):
        """A stale letter left in config.ini used to render a blank line and
        push the numbering along with it."""
        with_junk = ch.build_menu(["Q", "ZZ", "B", "X"], self.MAIN)
        without = ch.build_menu(["Q", "B", "X"], self.MAIN)
        self.assertEqual(with_junk, without)
        self.assertNotIn("ZZ", ch.menu_layout(["Q", "ZZ", "B", "X"], self.MAIN))

    def test_the_digits_match_the_lines_that_were_rendered(self):
        """The bug this whole layout exists to prevent: display and dispatch
        reading different tables, so 4 opened something the screen did not
        show at 4."""
        for items in (["Q", "B", "U", "X"], ["Q", "B", "U", "P", "N", "A", "S", "X"]):
            with self.subTest(items=items):
                alias = ch.menu_number_alias(items, self.MAIN)
                for line in ch.build_menu(items, self.MAIN).splitlines()[1:]:
                    if not line.strip():
                        continue
                    number, label = line[1:].split("] ", 1)
                    letter = alias[number]
                    self.assertEqual(ch.MAIN_MENU_LABELS[letter.upper()], label)

    def test_no_duplicates_when_config_already_lists_them(self):
        rendered = ch.build_menu(["Q", "B", "A", "S", "P", "G", "H", "X"], self.MAIN)
        self.assertEqual(rendered.count("Web Fetch"), 1)
        self.assertEqual(rendered.count("Settings"), 1)
        self.assertEqual(rendered.count("Profile"), 1)
        self.assertEqual(rendered.count("Games"), 1)
        self.assertEqual(rendered.count("Public Chatter"), 1)

    def test_stats_games_and_chatter_are_gone_from_utilities(self):
        """All three moved out -- Stats to Settings, Games and Public
        Chatter to the main menu -- so showing any of them here would be
        the exact duplication API Gateway's own move already guarded
        against. Utilities is left with Fortune and Wall of Shame."""
        rendered = ch.build_menu(
            ["S", "F", "W", "G", "H", "A", "X"],
            "\U0001F6E0\uFE0FUtilities Menu\U0001F6E0\uFE0F")
        self.assertNotIn("Stats", rendered)
        self.assertNotIn("Games", rendered)
        self.assertNotIn("Public Chatter", rendered)
        self.assertNotIn("API Gateway", rendered)
        self.assertIn("[1] Fortune", rendered)
        self.assertIn("[2] Wall of Shame", rendered)
        self.assertIn("[0] Back", rendered)

    def test_js8call_is_hidden_when_not_configured(self):
        with mock.patch.object(ch, "_js8call_configured", return_value=False):
            rendered = ch.build_menu(["M", "B", "C", "J", "X"], "📰BBS Menu📰")
        self.assertNotIn("JS8CALL", rendered)

    def test_js8call_is_shown_when_configured(self):
        with mock.patch.object(ch, "_js8call_configured", return_value=True):
            rendered = ch.build_menu(["M", "B", "C", "J", "X"], "📰BBS Menu📰")
        self.assertIn("JS8CALL", rendered)


class SettingsNavigationTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _FakeInterface()

    def tearDown(self):
        ch.send_message = self._real_send
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_settings_is_its_own_menu_now(self):
        """It used to jump straight into Linked Devices, which left the S
        entry mislabelled and the real Settings menu unreachable. Linking
        moved to Profile, where the rest of a user's identity lives."""
        ch.handle_settings_command(1234, self.iface)
        self.assertIn("Settings", self.sent[-1])
        self.assertNotIn("Request link code", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "SETTINGS")

    def test_it_reports_each_setting_current_value(self):
        """A menu line that names a toggle without its state makes you open
        it just to find out."""
        ch.handle_settings_command(1234, self.iface)
        body = self.sent[-1]
        self.assertIn("Offline mail relay: Off", body)
        self.assertIn("Node View: All nodes", body)

    def test_choice_one_offers_the_relay_toggle(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "1", self.iface, "!abc")
        self.assertIn("offline mail relay", self.sent[-1].lower())
        self.assertEqual(ch.get_user_state(1234).get("command"), "SETTINGS")
        self.assertEqual(ch.get_user_state(1234).get("step"), 2)

    def test_the_relay_toggle_takes_effect_and_shows_in_the_menu(self):
        ch.handle_settings_command(1234, self.iface)
        ch.handle_settings_steps(1234, "1", self.iface, "!abc")
        with mock.patch.object(ch, "send_mail_relay_preference_to_bbs_nodes",
                               lambda *a, **k: None):
            self.iface.bbs_nodes = []
            ch.handle_settings_steps(1234, "y", self.iface, "!abc")
        self.assertTrue(db_operations.get_mail_relay_preference("!abc"))
        self.assertIn("Offline mail relay: On", self.sent[-1])

    def test_choice_two_opens_the_node_view_lens(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "2", self.iface, "!abc")
        self.assertEqual(ch.get_user_state(1234).get("command"), "NODE_VIEW")

    def test_choice_three_says_what_this_node_is(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "3", self.iface, "!abc")
        self.assertIn("Bacon BBS", self.sent[0])

    def test_it_lists_view_stats_as_choice_four(self):
        ch.handle_settings_command(1234, self.iface)
        self.assertIn("[4] View Stats", self.sent[-1])

    def test_choice_four_opens_stats(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "4", self.iface, "!abc")
        self.assertIn("Stats Menu", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "STATS")

    def test_stats_back_returns_to_settings_not_the_main_menu(self):
        """Stats moved into Settings ([4] View Stats), so its own [0] has to
        follow it there -- it used to jump straight to the main menu, which
        would now skip past the screen the user actually came from."""
        ch.handle_settings_command(1234, self.iface)
        ch.handle_settings_steps(1234, "4", self.iface, "!abc")
        self.sent.clear()
        ch.handle_stats_steps(1234, "0", 1, self.iface)
        self.assertIn("Settings", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "SETTINGS")

    def test_zero_returns_to_the_main_menu(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "0", self.iface, "!abc")
        self.assertIn("Bacon BBS", self.sent[-1])

    def test_unrecognised_input_reshows_the_menu(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "9", self.iface, "!abc")
        self.assertIn("Settings", self.sent[-1])


class GamesAndChatterNavigationTests(unittest.TestCase):
    """Games and Public Chatter moved off Utilities onto the main menu, so
    their own 'back' has to mean the main menu now, not the Utilities
    screen they no longer live under."""

    def setUp(self):
        mail = mock.patch.object(ch, "get_mail", return_value=[])
        mail.start()
        self.addCleanup(mail.stop)
        self.sent = []
        self.send_patch = mock.patch.object(
            ch, "send_message", side_effect=lambda text, *_args: self.sent.append(text))
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        self.iface = _FakeInterface()

    def test_games_back_goes_to_the_main_menu(self):
        ch.handle_games_command(1234, self.iface)
        self.sent.clear()
        ch.handle_games_steps(1234, "0", self.iface)
        self.assertIn("Bacon BBS", self.sent[-1])
        self.assertNotIn("Utilities Menu", self.sent[-1])

    def test_public_chatter_back_goes_to_the_main_menu(self):
        with mock.patch.object(ch, "_chatter_windows_text", return_value="windows"):
            ch.handle_public_chatter_command(1234, self.iface)
        self.sent.clear()
        state = ch.get_user_state(1234)
        ch.handle_public_chatter_steps(1234, "0", self.iface, state)
        self.assertIn("Bacon BBS", self.sent[-1])
        self.assertNotIn("Utilities Menu", self.sent[-1])

    def test_quitting_a_game_returns_to_the_games_menu(self):
        """Not straight to Utilities, which no longer offers Games at all --
        and not straight to the main menu either, matching how Scoreboard
        and Hall of Fame already return to handle_games_command rather than
        skipping past it."""
        with mock.patch.object(ch, "stop_zork_session"):
            ch.update_user_state(1234, {'command': 'ZORK', 'step': 1, 'game_id': 'zork1'})
            ch.handle_zork_steps(1234, "quit", self.iface)
        self.assertIn("🎮 Games 🎮", self.sent[-1])


class ChannelDirectoryNavigationTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.send_patch = mock.patch.object(
            ch, "send_message", side_effect=lambda text, *_args: self.sent.append(text))
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        self.iface = _FakeInterface()

    def test_categories_are_one_based_and_offer_back(self):
        with mock.patch.object(ch, "get_channel_categories", return_value=[("General", 1)]):
            ch._send_channel_categories(1234, self.iface)
        self.assertIn("[1] General", self.sent[-1])
        self.assertIn("[0] Back", self.sent[-1])
        self.assertNotIn("[0] General", self.sent[-1])

    def test_zero_from_categories_returns_to_directory_menu(self):
        ch.handle_channel_directory_steps(
            1234, "0", 2, {"categories": [("General", 1)]}, self.iface)
        self.assertIn("CHANNEL DIRECTORY", self.sent[-1])


class ApiGatewayNavigationTests(unittest.TestCase):
    def test_main_action_opens_web_fetch_prompt(self):
        sent = []
        with mock.patch.object(ch, "_apigw_authorized", return_value=True), \
                mock.patch.object(ch, "send_message", side_effect=lambda text, *_args: sent.append(text)):
            ch.handle_apigw_command(1234, _FakeInterface())
        self.assertIn("URL to fetch", sent[-1])
        self.assertEqual(ch.get_user_state(1234), {
            'command': 'APIGW', 'step': 2, 'mode': 'http',
        })

    def test_cancel_returns_to_main_menu(self):
        sent = []
        with mock.patch.object(ch, "get_mail", return_value=[]), mock.patch.object(ch, "send_message", side_effect=lambda text, *_args: sent.append(text)):
            ch.update_user_state(1234, {'command': 'APIGW', 'step': 2, 'mode': 'ai'})
            ch.handle_apigw_steps(1234, "!cancel", _FakeInterface())
        self.assertIn("Bacon BBS", sent[-1])

    def test_a_bare_zero_is_now_part_of_the_question(self):
        """Step 2 is free text, so "0" is something the user typed, not a
        command. The menu step above still takes bare keys."""
        sent = []
        with mock.patch.object(ch, "send_message", side_effect=lambda text, *_args: sent.append(text)):
            ch.update_user_state(1234, {'command': 'APIGW', 'step': 2, 'mode': 'ai'})
            ch.handle_apigw_steps(1234, "0", _FakeInterface())
        self.assertNotIn("Bacon BBS", sent[-1])


class MenuFeedbackTests(unittest.TestCase):
    def test_invalid_bbs_choice_stays_in_bbs_menu(self):
        import message_processing as mp
        iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        ch.update_user_state(1234, {'command': 'MENU', 'menu': 'bbs', 'step': 1})
        with mock.patch.object(mp, 'handle_help_command') as help_menu:
            mp.process_message(1234, 'wat', iface)
        help_menu.assert_called_once_with(
            1234, iface, 'bbs', notice="Invalid choice.")

    def test_invalid_main_menu_choice_says_so(self):
        import message_processing as mp
        iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
        with mock.patch.object(mp, 'handle_help_command') as help_menu:
            mp.process_message(1234, 'wat', iface)
        help_menu.assert_called_once_with(
            1234, iface, None, notice="Invalid choice.")

    def test_a_wrong_key_at_the_mail_menu_answers(self):
        """This chain had no else at all: the BBS sent nothing back, which
        from the far end is indistinguishable from a dead link."""
        sent = []
        with mock.patch.object(ch, "send_message",
                               side_effect=lambda text, *_a, **_k: sent.append(text)):
            ch.update_user_state(1234, {'command': 'MAIL', 'step': 1})
            ch.handle_mail_steps(1234, "9", 1, {'command': 'MAIL', 'step': 1},
                                 _FakeInterface(), [])
        self.assertTrue(sent, "the mail menu answered a bad key with silence")
        self.assertIn("Invalid choice.", sent[0])
        self.assertIn("Mail Menu", sent[0])

    def test_a_wrong_key_on_a_board_keeps_you_on_that_board(self):
        """It used to fall through to the catch-all and drop the reader on
        the main menu, losing which board they were reading."""
        import message_processing as mp
        iface = types.SimpleNamespace(bbs_nodes=[], nodes={})
        state = {'command': 'BULLETIN_ACTION', 'step': 2,
                 'board': 'General', 'boards': ['General']}
        ch.update_user_state(1234, state)
        with mock.patch.object(mp, 'send_board_action_menu') as board_menu, \
                mock.patch.object(mp, 'handle_help_command') as help_menu:
            mp.process_message(1234, '9', iface)
        board_menu.assert_called_once_with(
            1234, iface, 'General', ['General'], notice="Invalid choice.")
        help_menu.assert_not_called()

    def test_a_wrong_key_on_the_profile_screen_says_so(self):
        sent = []
        with mock.patch.object(ch, "send_message",
                               side_effect=lambda text, *_a, **_k: sent.append(text)), \
                mock.patch.object(ch, "get_user_profile",
                                  return_value=(1234, "bot", "bot", "2026-09-03",
                                                "2026-09-03", 3, "")), \
                mock.patch.object(ch, "get_user_game_scores", return_value=[]), \
                mock.patch.object(ch, "get_mail_relay_preference", return_value=False):
            ch.update_user_state(1234, {'command': 'PROFILE', 'step': 1})
            ch.handle_profile_steps(1234, "9", _FakeInterface())
        self.assertIn("Invalid choice.", sent[-1])


class HiddenEntryTests(unittest.TestCase):
    """A letter the menu does not show needs the ! prefix.

    Bare letters used to fire for entries trimmed out of config.ini, which
    is how they collided with apps and door games wanting the same key.
    """

    def setUp(self):
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})

    def test_a_hidden_letter_is_refused_bare(self):
        # 'b' (BBS), omitted from this items list: every OTHER main-menu
        # letter is now in MENU_REQUIRED or MENU_REQUIRED_AFTER (Profile,
        # Ask Nomad, Web Fetch, Settings, Node View, Games, Public Chatter),
        # so none of them can demonstrate a genuinely hidden entry any more.
        # An operator choosing to omit BBS itself still can.
        import message_processing as mp
        with mock.patch.object(ch, "main_menu_items", ["Q", "U", "X"]), \
                mock.patch.dict(mp.main_menu_handlers,
                                {"b": mock.Mock()}, clear=False) as handlers:
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            with mock.patch.object(mp, 'handle_help_command') as help_menu:
                mp.process_message(1234, 'b', self.iface)
            handlers["b"].assert_not_called()
            help_menu.assert_called_once_with(
                1234, self.iface, None, notice="Invalid choice.")

    def test_a_hidden_letter_still_works_with_the_prefix(self):
        import message_processing as mp
        profile = mock.Mock()
        with mock.patch.object(ch, "main_menu_items", ["Q", "B", "U", "X"]), \
                mock.patch.dict(mp.main_menu_handlers, {"p": profile}, clear=False):
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            mp.process_message(1234, '!p', self.iface)
        profile.assert_called_once_with(1234, self.iface)

    def test_a_shown_letter_still_works_bare(self):
        import message_processing as mp
        quick = mock.Mock()
        with mock.patch.object(ch, "main_menu_items", ["Q", "B", "U", "X"]), \
                mock.patch.dict(mp.main_menu_handlers, {"q": quick}, clear=False):
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            mp.process_message(1234, 'q', self.iface)
        quick.assert_called_once_with(1234, self.iface)

    def test_a_digit_follows_the_trimmed_menu(self):
        """A digit means whatever that line of the screen says.

        On this node 3 is Games (inserted right after BBS) and 8 is Web
        Fetch. Both are asserted: checking only one would pass if the
        digits stopped tracking the layout and happened to land right.
        """
        import message_processing as mp
        for digit, letter in (("3", "g"), ("8", "a")):
            with self.subTest(digit=digit):
                handler = mock.Mock()
                with mock.patch.object(ch, "main_menu_items", ["Q", "B", "U", "X"]), \
                        mock.patch.dict(mp.main_menu_handlers,
                                        {letter: handler}, clear=False):
                    ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
                    mp.process_message(1234, digit, self.iface)
                handler.assert_called_once_with(1234, self.iface)


class ExitTests(unittest.TestCase):
    """[0] at the top level has to actually leave.

    It used to be stripped from the menu entirely while the SSH greeting
    told new users to type it, and typing it just redrew the same screen.
    """

    def setUp(self):
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={}, session_ended=False)

    def test_the_main_menu_offers_a_way_out(self):
        self.assertIn("[0] Exit",
                      ch.build_menu(["Q", "B", "U", "X"], "\U0001F4BEBacon BBS\U0001F4BE"))

    def test_submenus_still_say_back(self):
        rendered = ch.build_menu(["M", "B", "C", "X"], ch.BBS_MENU_TITLE)
        self.assertIn("[0] Back", rendered)
        self.assertNotIn("Exit", rendered)

    def test_exit_clears_the_menu_state_and_ends_the_session(self):
        with mock.patch.object(ch, "send_message"):
            ch.update_user_state(4321, {'command': 'MAIN_MENU', 'step': 1})
            ch.handle_exit_command(4321, self.iface)
        self.assertIsNone(ch.get_user_state(4321))
        self.assertTrue(self.iface.session_ended)

    def test_a_radio_has_no_session_to_end(self):
        radio = types.SimpleNamespace(bbs_nodes=[], nodes={})
        with mock.patch.object(ch, "send_message"):
            ch.handle_exit_command(4321, radio)
        self.assertFalse(hasattr(radio, "session_ended"))

    def test_zero_at_the_main_menu_exits(self):
        import message_processing as mp
        ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
        with mock.patch.object(mp, 'handle_exit_command') as leave:
            mp.process_message(1234, '0', self.iface)
        leave.assert_called_once_with(1234, self.iface)

    def test_zero_in_a_submenu_still_goes_back(self):
        import message_processing as mp
        ch.update_user_state(1234, {'command': 'MENU', 'menu': 'bbs', 'step': 1})
        with mock.patch.object(mp, 'handle_exit_command') as leave, \
                mock.patch.object(mp, 'handle_help_command') as help_menu:
            mp.process_message(1234, '0', self.iface)
        leave.assert_not_called()
        help_menu.assert_called_once_with(1234, self.iface)


class MenuHandlerWiringTests(unittest.TestCase):
    def test_main_menu_dispatches_the_new_letters(self):
        import message_processing as mp
        self.assertIn("s", mp.main_menu_handlers)
        self.assertIn("a", mp.main_menu_handlers)
        self.assertIs(mp.main_menu_handlers["s"], ch.handle_settings_command)
        self.assertIs(mp.main_menu_handlers["a"], ch.handle_apigw_command)

    def test_utilities_keeps_a_wired_for_the_prefixed_form(self):
        """A is no longer listed under Utilities, so the bare key is refused
        like any other hidden entry -- !a reaches the same handler."""
        import message_processing as mp
        self.assertIn("a", mp.utilities_menu_handlers)
        self.assertIs(mp.main_menu_handlers["a"], ch.handle_apigw_command)

    def test_utilities_no_longer_carries_dead_entries(self):
        """Stats, Games and Public Chatter left Utilities outright -- unlike
        'a', which was kept for muscle memory, their old letters here would
        dispatch to a menu Utilities no longer shows at all."""
        import message_processing as mp
        for letter in ("s", "g", "h", "z"):
            self.assertNotIn(letter, mp.utilities_menu_handlers)


class MenuNumberAliasTests(unittest.TestCase):
    """Every number a menu prints must actually do something.

    The rendered labels and the digit shortcuts were two hand-maintained
    tables. They drifted: the main menu printed "[5] Ask Nomad" while the
    alias table stopped at 4, so 5, 6 and 7 fell through to the catch-all
    and bounced the user back to the menu. They share one source now, and
    these tests fail if that ever comes apart again.
    """

    MAIN = "\U0001F4BEBacon BBS\U0001F4BE"

    def _menus(self):
        import message_processing as mp
        return (
            ("main", ch.MAIN_MENU_LABELS, mp.main_menu_handlers),
            ("bbs", ch.BBS_MENU_LABELS, mp.bbs_menu_handlers),
            ("utilities", ch.UTILITIES_MENU_LABELS, mp.utilities_menu_handlers),
        )

    def test_every_label_has_a_handler(self):
        """A letter that can render but cannot dispatch is a dead line."""
        for name, labels, handlers in self._menus():
            for letter in labels:
                with self.subTest(menu=name, letter=letter):
                    self.assertIn(letter.lower(), handlers)

    def test_every_rendered_number_resolves_to_a_handler(self):
        for name, _labels, handlers in self._menus():
            items, title = ch.menu_items_for(name)
            for digit, letter in ch.menu_number_alias(items, title).items():
                with self.subTest(menu=name, digit=digit):
                    self.assertIn(letter, handlers)

    def test_utilities_numbers_now_reach_only_fortune_and_shame(self):
        """Stats, Games and Public Chatter left Utilities entirely -- see
        MainMenuContentsTests.test_stats_games_and_chatter_are_gone_from_utilities."""
        items, title = ch.menu_items_for("utilities")
        alias = ch.menu_number_alias(items, title)
        self.assertEqual(alias["1"], "f")
        self.assertEqual(alias["2"], "w")
        self.assertNotIn("g", alias.values())
        self.assertNotIn("h", alias.values())
        self.assertNotIn("s", alias.values())

    def test_main_numbers_reach_games_and_public_chatter(self):
        """Where Stats/Games/Public Chatter actually live now: Games and
        Public Chatter on the main menu, right after BBS."""
        items, title = ch.menu_items_for("main")
        alias = ch.menu_number_alias(items, title)
        self.assertEqual(alias["3"], "g")
        self.assertEqual(alias["4"], "h")

    def test_a_hidden_entry_is_given_no_number_at_all(self):
        """Numbers describe the screen. Public Chatter's old [6] alias went
        with this: a digit the menu never prints must not quietly work.

        Asserted against the numbers the menu actually renders rather than
        against a literal digit -- a fixed "6 is unclaimed" only held while
        the main menu happened to be that length, and would go quiet the
        moment a required entry was added rather than catching anything."""
        # 'b' (BBS), omitted here: every other main-menu letter is required
        # or positionally forced now (see HiddenEntryTests), so none of
        # them can demonstrate a genuinely absent number any more.
        items = ["Q", "U", "X"]
        alias = ch.menu_number_alias(items, self.MAIN)
        self.assertNotIn("b", alias.values())
        self.assertIn("p", alias.values())
        rendered = set(re.findall(r"\[(\d+)\]", ch.build_menu(items, self.MAIN)))
        self.assertEqual(set(alias), rendered)

    def test_no_digit_is_claimed_twice(self):
        for name, _labels, _handlers in self._menus():
            items, title = ch.menu_items_for(name)
            alias = ch.menu_number_alias(items, title)
            with self.subTest(menu=name):
                self.assertEqual(len(alias), len(set(alias.values())))


class GameInputRoutingTests(unittest.TestCase):
    def setUp(self):
        import utils
        utils.user_states.clear()
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})

    def tearDown(self):
        import utils
        utils.user_states.clear()

    def test_zork_receives_inputs_that_overlap_global_quick_keys(self):
        import message_processing as mp

        for command in ('n', 's', 'x', 'sm,,someone,,hello'):
            with self.subTest(command=command), \
                    mock.patch.object(mp, 'handle_zork_steps') as handle_zork:
                ch.update_user_state(1234, {'command': 'ZORK', 'step': 1, 'game_id': 'zork1'})
                mp.process_message(1234, command, self.iface)
                handle_zork.assert_called_once_with(1234, command, self.iface)

    def test_zork_treats_prefixed_commands_as_game_input(self):
        import message_processing as mp

        ch.update_user_state(1234, {'command': 'ZORK', 'step': 1, 'game_id': 'zork1'})
        with mock.patch.object(mp, 'handle_zork_steps') as handle_zork, \
                mock.patch.object(mp, 'handle_check_mail_command') as check_mail:
            mp.process_message(1234, '!CM', self.iface)
        handle_zork.assert_called_once_with(1234, '!CM', self.iface)
        check_mail.assert_not_called()

    def test_trivia_receives_inputs_that_overlap_global_quick_keys(self):
        """Trivia King itself uses N for 'next question' -- the main menu
        also uses N for Ask Nomad, S for Settings. A live session must not
        let either steal a letter that means something different mid-game.
        Verified empirically rather than trusted from reading the dispatch
        order: ZORK gets its protection from an unconditional early return,
        but TRIVIA falls through much further into the function, relying
        on TRIVIA never populating the `handlers` dict that a MENU/
        MAIN_MENU state's single-letter lookup uses -- a different
        mechanism reaching the same place, worth its own proof."""
        import message_processing as mp

        for command in ('n', 's', 'x'):
            with self.subTest(command=command), \
                    mock.patch.object(mp, 'handle_trivia_steps') as handle_trivia:
                ch.update_user_state(1234, {'command': 'TRIVIA', 'step': 1})
                mp.process_message(1234, command, self.iface)
                handle_trivia.assert_called_once_with(1234, command, self.iface)

    def test_trivia_treats_prefixed_commands_as_game_input(self):
        import message_processing as mp

        ch.update_user_state(1234, {'command': 'TRIVIA', 'step': 1})
        with mock.patch.object(mp, 'handle_trivia_steps') as handle_trivia, \
                mock.patch.object(mp, 'handle_check_mail_command') as check_mail:
            mp.process_message(1234, '!CM', self.iface)
        handle_trivia.assert_called_once_with(1234, '!CM', self.iface)
        check_mail.assert_not_called()

    def test_games_menu_receives_shortcut_letters_before_main_menu(self):
        import message_processing as mp

        ch.update_user_state(1234, {'command': 'GAMES_MENU', 'step': 1})
        with mock.patch.object(mp, 'handle_games_steps') as handle_games:
            mp.process_message(1234, 's', self.iface)
            handle_games.assert_called_once_with(1234, 's', self.iface)

    def test_mail_receives_inputs_that_overlap_global_quick_keys(self):
        import message_processing as mp

        state = {'command': 'MAIL', 'step': 7, 'content': ''}
        ch.update_user_state(1234, state)
        with mock.patch.object(mp, 'handle_mail_steps') as handle_mail:
            mp.process_message(1234, 'n', self.iface)
            handle_mail.assert_called_once_with(1234, 'n', 7, state, self.iface, [])

    def test_mail_treats_prefixed_commands_as_mail_input(self):
        import message_processing as mp

        state = {'command': 'MAIL', 'step': 7, 'content': ''}
        ch.update_user_state(1234, state)
        with mock.patch.object(mp, 'handle_mail_steps') as handle_mail, \
                mock.patch.object(mp, 'handle_check_mail_command') as check_mail:
            mp.process_message(1234, '!CM', self.iface)
        handle_mail.assert_called_once_with(1234, '!CM', 7, state, self.iface, [])
        check_mail.assert_not_called()


class GlobalCommandPrefixTests(unittest.TestCase):
    def setUp(self):
        import utils
        utils.user_states.clear()
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={})

    def tearDown(self):
        import utils
        utils.user_states.clear()

    def test_prefixed_main_menu_action_dispatches_globally(self):
        import message_processing as mp

        with mock.patch.object(mp, 'handle_settings_command') as settings:
            with mock.patch.dict(mp.main_menu_handlers, {'s': settings}, clear=False):
                mp.process_message(1234, '!S', self.iface)
        settings.assert_called_once_with(1234, self.iface)

    def test_prefixed_action_interrupts_unprotected_workflow(self):
        import message_processing as mp

        state = {'command': 'PROFILE', 'step': 3, 'relay_enabled': True}
        ch.update_user_state(1234, state)
        nomad = mock.Mock()
        with mock.patch.dict(mp.main_menu_handlers, {'n': nomad}, clear=False), \
                mock.patch.object(mp, 'handle_profile_steps') as profile:
            mp.process_message(1234, '!N', self.iface, sender_node_id='!user')
        nomad.assert_called_once_with(1234, self.iface)
        profile.assert_not_called()

    def test_unprefixed_main_letter_stays_in_active_workflow(self):
        import message_processing as mp

        state = {'command': 'PROFILE', 'step': 3, 'relay_enabled': True}
        ch.update_user_state(1234, state)
        with mock.patch.object(mp, 'handle_profile_steps') as profile, \
                mock.patch.object(mp, 'handle_ask_nomad_command') as nomad:
            mp.process_message(1234, 'n', self.iface, sender_node_id='!user')
        profile.assert_called_once_with(1234, 'n', self.iface, '!user')
        nomad.assert_not_called()

    def test_prefixed_quick_command_dispatches(self):
        import message_processing as mp

        with mock.patch.object(mp, 'handle_check_mail_command') as check_mail:
            mp.process_message(1234, '!CM', self.iface)
        check_mail.assert_called_once_with(1234, self.iface)

    def test_prefixed_reply_command_dispatches(self):
        import message_processing as mp

        with mock.patch.object(mp, 'handle_quick_reply_command') as quick_reply:
            mp.process_message(1234, '!R', self.iface)
        quick_reply.assert_called_once_with(1234, self.iface)

    def test_structured_global_command_strips_prefix_for_handler(self):
        import message_processing as mp

        with mock.patch.object(mp, 'handle_send_mail_command') as send_mail:
            mp.process_message(1234, '!SM,,DEST,,Subject,,Body', self.iface)
        send_mail.assert_called_once_with(
            1234, 'SM,,DEST,,Subject,,Body', self.iface, []
        )

    def test_unprefixed_legacy_quick_command_does_not_dispatch(self):
        import message_processing as mp

        with mock.patch.object(mp, 'handle_check_mail_command') as check_mail, \
                mock.patch.object(mp, 'handle_help_command') as help_menu:
            mp.process_message(1234, 'CM', self.iface)
        check_mail.assert_not_called()
        help_menu.assert_called_once_with(1234, self.iface)

    def test_prefixed_exit_is_not_rewritten_by_double_letter_shorthand(self):
        import message_processing as mp

        exit_handler = mock.Mock()
        with mock.patch.dict(mp.main_menu_handlers, {'x': exit_handler}, clear=False):
            mp.process_message(1234, '!X', self.iface)
        exit_handler.assert_called_once_with(1234, self.iface)

    def test_local_trailing_x_shorthand_is_preserved(self):
        import message_processing as mp

        nomad = mock.Mock()
        with mock.patch.dict(mp.main_menu_handlers, {'n': nomad}, clear=False):
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            mp.process_message(1234, 'NX', self.iface)
        nomad.assert_called_once_with(1234, self.iface)

    def test_main_menu_letters_and_numbers_remain_local(self):
        import message_processing as mp

        # '9' is Settings' live position now: Q,B,G,H,U,P,N,A,S,V,X.
        settings = mock.Mock()
        with mock.patch.dict(mp.main_menu_handlers, {'s': settings}, clear=False):
            for value in ('s', '9'):
                ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
                mp.process_message(1234, value, self.iface)
        self.assertEqual(settings.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class CancelWordTests(unittest.TestCase):
    """Cancelling a content prompt takes the global prefix.

    A bare "Exit" typed at the channel-name prompt was read as a name: the
    live directory holds a channel called Exit whose URL field is a user's
    complaint about the prompt that trapped them. Menus are unaffected --
    there [0] is a line on the screen, not something the user composed.
    """

    def setUp(self):
        self.iface = types.SimpleNamespace(bbs_nodes=[], nodes={}, session_ended=False)

    def test_the_prefixed_words_cancel(self):
        for word in ("!exit", "!cancel", "!x", "!0"):
            with self.subTest(word=word):
                self.assertTrue(ch.is_cancel(word))
                self.assertTrue(ch.is_cancel(f"  {word.upper()}  "))

    def test_the_bare_words_are_content(self):
        for word in ("exit", "cancel", "x", "0", "Exit", "", "!", "!exit now"):
            with self.subTest(word=word):
                self.assertFalse(ch.is_cancel(word))

    def test_exit_is_accepted_as_a_channel_name(self):
        """The trap, from the other side: the user meant it, so honour it."""
        sent = []
        with mock.patch.object(ch, "send_message",
                               side_effect=lambda text, *_a, **_k: sent.append(text)):
            ch.update_user_state(1234, {'command': 'CHANNEL_DIRECTORY', 'step': 3})
            ch.handle_channel_directory_steps(
                1234, "Exit", 3, {'command': 'CHANNEL_DIRECTORY', 'step': 3}, self.iface)
        self.assertIn("channel URL or PSK", sent[-1])
        self.assertEqual(ch.get_user_state(1234).get('channel_name'), "Exit")

    def test_a_prefixed_cancel_backs_out_of_the_name_prompt(self):
        sent = []
        with mock.patch.object(ch, "send_message",
                               side_effect=lambda text, *_a, **_k: sent.append(text)):
            ch.update_user_state(1234, {'command': 'CHANNEL_DIRECTORY', 'step': 3})
            ch.handle_channel_directory_steps(
                1234, "!cancel", 3, {'command': 'CHANNEL_DIRECTORY', 'step': 3}, self.iface)
        self.assertIn("CHANNEL DIRECTORY", sent[-1])

    def test_the_content_prompts_advertise_the_prefixed_form(self):
        """Only the prompts that changed. The relay directory keeps its
        "[0] Cancel" because that is a menu line, not a content prompt."""
        import inspect
        source = inspect.getsource(ch)
        self.assertNotIn("to cancel:", source)
        self.assertNotIn("or 0 to cancel", source)
        self.assertNotIn("Keep it short. [0] Cancel", source)
        self.assertIn("{CANCEL_HINT} to stop", source)

    def test_exclamation_x_survives_the_trailing_x_shorthand(self):
        """NX-style shorthand collapses a two-character reply to its first
        letter, and "!x" fits that shape. Every site has to skip it or the
        cancel word arrives as a bare "!"."""
        import message_processing as mp
        for module_source in (ch, mp):
            import inspect
            for line in inspect.getsource(module_source).splitlines():
                if "== 2 and" in line and "'x'" in line:
                    with self.subTest(line=line.strip()):
                        self.assertIn("startswith('!')", line)

    def test_a_cancel_word_reaches_the_prompt_not_the_global_table(self):
        """!x resolves to main_menu_handlers['x'], which exits the BBS. It
        must not do that halfway through writing a bulletin."""
        import message_processing as mp
        self.assertTrue(mp._in_text_prompt({'command': 'BULLETIN_POST', 'step': 4}))
        self.assertTrue(mp._in_text_prompt({'command': 'CHANNEL_DIRECTORY', 'step': 3}))
        self.assertFalse(mp._in_text_prompt({'command': 'MAIN_MENU', 'step': 1}))
        self.assertFalse(mp._in_text_prompt(None))

    def test_prefixed_cancel_does_not_end_the_session(self):
        import message_processing as mp
        ch.update_user_state(1234, {'command': 'BULLETIN_POST', 'step': 4,
                                    'board': 'General', 'boards': ['General']})
        with mock.patch.object(mp, 'handle_exit_command') as leave, \
                mock.patch.object(ch, 'send_message'):
            mp.process_message(1234, '!x', self.iface)
        leave.assert_not_called()
        self.assertFalse(self.iface.session_ended)

    def test_menus_still_take_bare_keys(self):
        import message_processing as mp
        quick = mock.Mock()
        with mock.patch.dict(mp.main_menu_handlers, {"q": quick}, clear=False):
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            mp.process_message(1234, '1', self.iface)
        quick.assert_called_once_with(1234, self.iface)
