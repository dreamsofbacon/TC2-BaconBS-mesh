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

    def test_existing_entries_keep_their_numbers(self):
        """Renumbering would break muscle memory and every doc reference."""
        rendered = ch.build_menu(["Q", "B", "U", "P", "N", "X"], self.MAIN)
        for expected in ("[1] Quick Commands", "[2] BBS", "[3] Utilities",
                         "[4] Profile", "[5] Ask Nomad"):
            self.assertIn(expected, rendered)
        self.assertNotIn("[0] Exit", rendered)

    def test_new_entries_are_inserted_before_exit(self):
        lines = [l for l in ch.build_menu(["Q", "X"], self.MAIN).splitlines() if l.strip()]
        self.assertFalse(any(line.startswith("[0]") for line in lines))

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


class MenuHandlerWiringTests(unittest.TestCase):
    def test_main_menu_dispatches_the_new_letters(self):
        import message_processing as mp
        self.assertIn("s", mp.main_menu_handlers)
        self.assertIn("a", mp.main_menu_handlers)
        self.assertIs(mp.main_menu_handlers["s"], ch.handle_settings_command)
        self.assertIs(mp.main_menu_handlers["a"], ch.handle_apigw_command)

    def test_utilities_still_accepts_a_for_muscle_memory(self):
        """No longer listed there, but an existing habit shouldn't break."""
        import message_processing as mp
        self.assertIn("a", mp.utilities_menu_handlers)
        self.assertIn("h", mp.utilities_menu_handlers)


class MenuNumberAliasTests(unittest.TestCase):
    """Every number a menu prints must actually do something.

    The rendered labels and the digit shortcuts were two hand-maintained
    tables. They drifted: the main menu printed "[5] Ask Nomad" while the
    alias table stopped at 4, so 5, 6 and 7 fell through to the catch-all
    and bounced the user back to the menu. They share one source now, and
    these tests fail if that ever comes apart again.
    """

    def _menus(self):
        import message_processing as mp
        return (
            ("main", ch.MAIN_NUMBER_MAP, mp._MAIN_NUMBER_ALIAS, mp.main_menu_handlers),
            ("bbs", ch.BBS_NUMBER_MAP, mp._BBS_NUMBER_ALIAS, mp.bbs_menu_handlers),
            ("utilities", ch.UTILITIES_NUMBER_MAP, mp._UTILITIES_NUMBER_ALIAS,
             mp.utilities_menu_handlers),
        )

    def test_every_rendered_number_resolves_to_a_handler(self):
        for name, number_map, alias, handlers in self._menus():
            for letter, label in number_map.items():
                digit = label.split("]")[0].lstrip("[")
                with self.subTest(menu=name, label=label):
                    self.assertEqual(alias.get(digit), letter.lower(),
                                     f"{name} menu prints {label} but {digit} is unmapped")
                    self.assertIn(letter.lower(), handlers)

    def test_the_numbers_that_regressed(self):
        """5/6/7 on the main menu -- the reported symptom."""
        import message_processing as mp
        self.assertEqual(mp._MAIN_NUMBER_ALIAS["5"], "n")
        self.assertEqual(mp._MAIN_NUMBER_ALIAS["6"], "a")
        self.assertEqual(mp._MAIN_NUMBER_ALIAS["7"], "s")

    def test_utilities_5_opens_public_chatter(self):
        import message_processing as mp
        self.assertEqual(mp._UTILITIES_NUMBER_ALIAS["5"], "h")

    def test_legacy_utilities_6_still_opens_public_chatter(self):
        import message_processing as mp
        self.assertEqual(mp._UTILITIES_NUMBER_ALIAS["6"], "h")

    def test_no_digit_is_claimed_twice(self):
        for name, number_map, _alias, _handlers in self._menus():
            digits = [label.split("]")[0].lstrip("[") for label in number_map.values()]
            with self.subTest(menu=name):
                self.assertEqual(len(digits), len(set(digits)))


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
