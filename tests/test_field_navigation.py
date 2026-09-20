"""Navigation defects found by driving the live BBS as a user.

Every test here names a finding from BBS-FIELD-TEST-REPORT-2026-09-20.md.
They are about what a person can and cannot get out of, which is the part
that does not show up in a unit test of any single handler: each one failed
against the code as shipped in v0.1.686.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import command_handlers as ch
import message_processing as mp


class _Interface:
    protocol_name = "meshtastic"
    max_text_bytes = 220

    def __init__(self):
        self.bbs_nodes = []
        self.nodes = {}


class BangShortcutsTests(unittest.TestCase):
    """F2 -- the shortcuts died inside Games and Public Chatter.

    The main-menu tip and docs/MENU-MAP.txt both promise `!<letter>` reaches
    any main-menu item from anywhere. Inside the Games menu every one of them
    came back "Invalid choice. Enter 1-10, S, H, F, or 0." -- an error that
    does not mention `!` at all, so a user reasonably concludes the BBS has
    frozen. Two of the eight top-level destinations were dead zones.
    """

    def setUp(self):
        self.iface = _Interface()
        self.addCleanup(ch.update_user_state, 4242, None)

    def _dispatch(self, state, message):
        """Run one message through the real router, with the BBS stubbed."""
        ch.update_user_state(4242, state)
        opened = []
        with mock.patch.dict(mp.main_menu_handlers, {
                'b': lambda *a, **k: opened.append('bbs'),
                'q': lambda *a, **k: opened.append('quick'),
        }, clear=False), \
                mock.patch.object(mp, '_auto_update_profile', lambda *a, **k: None), \
                mock.patch.object(ch, 'send_message', lambda *a, **k: True), \
                mock.patch.object(mp, 'handle_games_steps',
                                  lambda *a, **k: opened.append('games')), \
                mock.patch.object(mp, 'handle_public_chatter_steps',
                                  lambda *a, **k: opened.append('chatter')), \
                mock.patch.object(mp, 'handle_zork_steps',
                                  lambda *a, **k: opened.append('zork')):
            mp.process_message(4242, message, self.iface, is_sync_message=False,
                               sender_node_id='!abcd1234')
        return opened

    def test_a_shortcut_escapes_the_games_menu(self):
        self.assertEqual(['bbs'], self._dispatch(
            {'command': 'GAMES_MENU', 'step': 1}, '!B'))

    def test_a_shortcut_escapes_the_public_chatter_picker(self):
        self.assertEqual(['bbs'], self._dispatch(
            {'command': 'PUBLIC_CHATTER', 'step': 1}, '!B'))

    def test_the_games_menu_still_takes_its_own_keys(self):
        self.assertEqual(['games'], self._dispatch(
            {'command': 'GAMES_MENU', 'step': 1}, '3'))

    def test_a_door_session_still_owns_every_key(self):
        """Inside Zork, '!' and '?' are the game's input, not the BBS's --
        the 'quick keys steal game input' complaint, which stays fixed."""
        self.assertEqual(['zork'], self._dispatch(
            {'command': 'ZORK', 'step': 1}, '!CM'))
        self.assertEqual(['zork'], self._dispatch(
            {'command': 'ZORK', 'step': 1}, '?'))


class QuestionMarkTests(unittest.TestCase):
    """F4 -- every new account is told "Send ? any time for the menu" on its
    first screen, and nothing implemented it. It answered "Invalid choice."
    the first time they tried it."""

    def setUp(self):
        self.iface = _Interface()
        self.addCleanup(ch.update_user_state, 4243, None)

    def _ask(self, state):
        ch.update_user_state(4243, state)
        shown = []
        with mock.patch.object(mp, '_auto_update_profile', lambda *a, **k: None), \
                mock.patch.object(mp, 'handle_help_command',
                                  lambda *a, **k: shown.append('menu')), \
                mock.patch.object(mp, 'handle_mail_steps',
                                  lambda *a, **k: shown.append('mail')), \
                mock.patch.object(ch, 'send_message', lambda *a, **k: True):
            mp.process_message(4243, '?', self.iface, is_sync_message=False,
                               sender_node_id='!abcd1234')
        return shown

    def test_it_shows_the_menu_from_a_menu(self):
        self.assertEqual(['menu'], self._ask({'command': 'MENU', 'menu': 'main',
                                              'step': 1}))

    def test_it_shows_the_menu_from_the_games_menu(self):
        self.assertEqual(['menu'], self._ask({'command': 'GAMES_MENU', 'step': 1}))

    def test_it_is_content_while_a_prompt_is_collecting_text(self):
        """A subject line of "?" is a subject, not a request for the menu."""
        self.assertEqual(['mail'], self._ask({'command': 'MAIL', 'step': 5}))


class MainMenuKeyTests(unittest.TestCase):
    """F3 -- a bare letter at the main menu silently disconnected you."""

    def test_a_letter_that_is_never_printed_is_not_a_selection(self):
        """X is rendered as "[0] Exit", so the letter itself never appears.
        Typing it hung up with no confirmation, while 99 and -1 were politely
        refused -- the one destructive key was the unguessable one."""
        letter, on_screen = mp._menu_input('main', 'x')
        self.assertFalse(on_screen)

    def test_zero_still_reaches_exit(self):
        letter, on_screen = mp._menu_input('main', '0')
        self.assertEqual('x', letter)
        self.assertTrue(on_screen)

    def test_a_printed_number_still_resolves(self):
        letter, on_screen = mp._menu_input('main', '2')
        self.assertTrue(on_screen)
        self.assertNotEqual('x', letter)


class BulletinListTests(unittest.TestCase):
    """F7 and F9 -- the post list had no exit, and reading a post threw you
    out of the board entirely."""

    def setUp(self):
        self.iface = _Interface()
        self.sent = []
        patcher = mock.patch.object(
            ch, 'send_message',
            side_effect=lambda text, *a, **k: self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ch.update_user_state, 4244, None)
        self.state = {'command': 'BULLETIN_READ', 'step': 3, 'board': 'General',
                      'bulletins': [(1, 'First post'), (2, 'Second post')]}

    def test_zero_leaves_the_list(self):
        """Every other list offers [0]; this one reported it as a bad post
        number -- the one key the banner promises always goes back."""
        with mock.patch.object(ch, 'handle_bulletin_command') as back:
            ch.handle_bb_steps(4244, '0', 3, self.state, self.iface, [])
        back.assert_called_once()

    def test_a_bad_number_names_the_way_out(self):
        ch.handle_bb_steps(4244, '99', 3, self.state, self.iface, [])
        self.assertIn('[0]', self.sent[-1])

    def test_reading_a_post_keeps_you_on_the_list(self):
        content = ('someone', '2026-09-20', 'First post', 'the body',
                   'uid-1', 1, None)
        with mock.patch.object(ch, 'get_bulletin_content', return_value=content), \
                mock.patch.object(ch, '_can_moderate', return_value=False):
            ch.handle_bb_steps(4244, '1', 3, self.state, self.iface, [])
        state = ch.get_user_state(4244)
        self.assertEqual('BULLETIN_READ', state['command'])
        self.assertEqual(3, state['step'])
        self.assertIn('another number', self.sent[-1])

    def test_the_invitation_rides_in_the_same_message(self):
        """A second packet for one line of text is what the mesh cannot
        afford -- the same reason Ask Nomad bundles its follow-up."""
        content = ('someone', '2026-09-20', 'First post', 'the body',
                   'uid-1', 1, None)
        before = len(self.sent)
        with mock.patch.object(ch, 'get_bulletin_content', return_value=content), \
                mock.patch.object(ch, '_can_moderate', return_value=False):
            ch.handle_bb_steps(4244, '1', 3, self.state, self.iface, [])
        self.assertEqual(1, len(self.sent) - before)


if __name__ == '__main__':
    unittest.main()
