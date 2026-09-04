"""Tests for discoverable main-menu actions and consistent navigation.

Account linking and web fetches are direct main-menu actions even when an
existing config.ini predates those entries.
"""
import sqlite3
import sys
import types
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
        self.assertIn("Web Fetch", rendered)
        self.assertIn("Linked Devices", rendered)

    def test_a_full_config_numbers_every_entry_in_order(self):
        rendered = ch.build_menu(["Q", "B", "U", "P", "N", "X"], self.MAIN)
        for expected in ("[1] Quick Commands", "[2] BBS", "[3] Utilities",
                         "[4] Profile", "[5] Ask Nomad", "[6] Web Fetch",
                         "[7] Linked Devices", "[0] Exit"):
            self.assertIn(expected, rendered)

    def test_a_trimmed_config_closes_the_gap_instead_of_skipping_numbers(self):
        """The baconbot case. This node hides Profile and Ask Nomad on
        purpose, and the menu used to read [1][2][3][6][7] -- holes that mean
        nothing to someone who just found the BBS."""
        rendered = ch.build_menu(["Q", "B", "U", "X"], self.MAIN)
        self.assertNotIn("Profile", rendered)
        self.assertNotIn("Ask Nomad", rendered)
        self.assertIn("[4] Web Fetch", rendered)
        self.assertIn("[5] Linked Devices", rendered)

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
        rendered = ch.build_menu(["Q", "A", "S", "X"], self.MAIN)
        self.assertEqual(rendered.count("Web Fetch"), 1)
        self.assertEqual(rendered.count("Linked Devices"), 1)

    def test_api_gateway_no_longer_rendered_under_utilities(self):
        """It moved to the main menu; showing it in both would be confusing."""
        rendered = ch.build_menu(
            ["S", "F", "W", "G", "A", "X"], "\U0001F6E0\uFE0FUtilities Menu\U0001F6E0\uFE0F")
        self.assertNotIn("API Gateway", rendered)
        self.assertIn("[1] Stats", rendered)
        self.assertIn("[5] Public Chatter", rendered)

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

    def test_settings_shortcut_opens_linked_devices(self):
        ch.handle_settings_command(1234, self.iface)
        self.assertIn("Linked Devices", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "ACCOUNT")
        self.assertEqual(ch.get_user_state(1234).get("return_to"), "main")

    def test_choice_one_opens_account_linking(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_settings_steps(1234, "1", self.iface, "!abc")
        self.assertIn("Request link code", self.sent[-1])
        self.assertEqual(ch.get_user_state(1234).get("command"), "ACCOUNT")
        self.assertEqual(ch.get_user_state(1234).get("return_to"), "settings")

    def test_linked_devices_back_returns_to_main(self):
        ch.handle_settings_command(1234, self.iface)
        self.sent.clear()
        ch.handle_account_steps(1234, "0", self.iface, "!abc")
        self.assertIn("Bacon BBS", self.sent[-1])

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
        with mock.patch.object(ch, "send_message", side_effect=lambda text, *_args: sent.append(text)):
            ch.update_user_state(1234, {'command': 'APIGW', 'step': 2, 'mode': 'ai'})
            ch.handle_apigw_steps(1234, "0", _FakeInterface())
        self.assertIn("Bacon BBS", sent[-1])


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
        import message_processing as mp
        with mock.patch.object(ch, "main_menu_items", ["Q", "B", "U", "X"]), \
                mock.patch.dict(mp.main_menu_handlers,
                                {"p": mock.Mock()}, clear=False) as handlers:
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            with mock.patch.object(mp, 'handle_help_command') as help_menu:
                mp.process_message(1234, 'p', self.iface)
            handlers["p"].assert_not_called()
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
        """4 is Web Fetch on this node because that is what line 4 says."""
        import message_processing as mp
        web_fetch = mock.Mock()
        with mock.patch.object(ch, "main_menu_items", ["Q", "B", "U", "X"]), \
                mock.patch.dict(mp.main_menu_handlers, {"a": web_fetch}, clear=False):
            ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
            mp.process_message(1234, '4', self.iface)
        web_fetch.assert_called_once_with(1234, self.iface)


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
        self.assertIn("h", mp.utilities_menu_handlers)
        self.assertIs(mp.main_menu_handlers["a"], ch.handle_apigw_command)


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

    def test_utilities_numbers_reach_games_and_public_chatter(self):
        items, title = ch.menu_items_for("utilities")
        alias = ch.menu_number_alias(items, title)
        self.assertEqual(alias["4"], "g")
        self.assertEqual(alias["5"], "h")

    def test_a_hidden_entry_is_given_no_number_at_all(self):
        """Numbers describe the screen. Public Chatter's old [6] alias went
        with this: a digit the menu never prints must not quietly work."""
        alias = ch.menu_number_alias(["Q", "B", "U", "X"], self.MAIN)
        self.assertNotIn("p", alias.values())
        self.assertNotIn("n", alias.values())
        self.assertNotIn("6", alias)

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

        settings = mock.Mock()
        with mock.patch.dict(mp.main_menu_handlers, {'s': settings}, clear=False):
            for value in ('s', '7'):
                ch.update_user_state(1234, {'command': 'MAIN_MENU', 'step': 1})
                mp.process_message(1234, value, self.iface)
        self.assertEqual(settings.call_count, 2)


if __name__ == "__main__":
    unittest.main()
