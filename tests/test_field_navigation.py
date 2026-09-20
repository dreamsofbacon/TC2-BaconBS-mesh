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


class OwnPostDeleteTests(unittest.TestCase):
    """F14 -- posting to the wrong board had no remedy short of finding a
    moderator. The delete travels as a tombstone either way, so the
    machinery already existed; only the offer was missing."""

    def setUp(self):
        self.iface = _Interface()
        self.sent = []
        patcher = mock.patch.object(
            ch, 'send_message',
            side_effect=lambda text, *a, **k: self.sent.append(text) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ch.update_user_state, 4245, None)
        self.state = {'command': 'BULLETIN_READ', 'step': 3, 'board': 'General',
                      'bulletins': [(1, 'Mine')]}
        self.content = ('me', '2026-09-20', 'Mine', 'the body', 'uid-1', 1, None)

    def _read(self, mine):
        with mock.patch.object(ch, 'get_bulletin_content', return_value=self.content), \
                mock.patch.object(ch, '_can_moderate', return_value=False), \
                mock.patch.object(ch, '_wrote_this_post', return_value=mine):
            ch.handle_bb_steps(4245, '1', 3, self.state, self.iface, [])
        return ch.get_user_state(4245)

    def test_your_own_post_offers_delete(self):
        self._read(mine=True)
        self.assertIn('[D]elete', self.sent[-1])

    def test_someone_elses_post_does_not(self):
        self._read(mine=False)
        self.assertNotIn('[D]elete', self.sent[-1])

    def test_delete_asks_first(self):
        """It goes from every node and a radio user cannot undo it."""
        state = self._read(mine=True)
        ch.handle_bb_steps(4245, 'D', 3, state, self.iface, [])
        self.assertIn('[Y]es', self.sent[-1])
        self.assertEqual('BULLETIN_OWN_DELETE',
                         ch.get_user_state(4245)['command'])

    def test_answering_no_keeps_it(self):
        state = {'command': 'BULLETIN_OWN_DELETE', 'step': 1,
                 'unique_id': 'uid-1', 'subject': 'Mine', 'board': 'General'}
        with mock.patch.object(ch, 'delete_bulletin') as gone, \
                mock.patch.object(ch, 'handle_bulletin_command'):
            ch.handle_bulletin_own_delete_steps(4245, '0', self.iface, state, [])
        gone.assert_not_called()
        self.assertIn('Left alone', self.sent[-1])

    def test_confirming_deletes_it(self):
        state = {'command': 'BULLETIN_OWN_DELETE', 'step': 1,
                 'unique_id': 'uid-1', 'subject': 'Mine', 'board': 'General'}
        with mock.patch.object(ch, '_wrote_this_post', return_value=True), \
                mock.patch.object(ch, 'delete_bulletin') as gone, \
                mock.patch.object(ch, 'handle_bulletin_command'):
            ch.handle_bulletin_own_delete_steps(4245, 'Y', self.iface, state, [])
        gone.assert_called_once()

    def test_authorship_is_rechecked_at_the_point_of_action(self):
        """A confirm arrives seconds after the offer; the moderator path
        re-checks its role for the same reason."""
        state = {'command': 'BULLETIN_OWN_DELETE', 'step': 1,
                 'unique_id': 'uid-1', 'subject': 'Mine', 'board': 'General'}
        with mock.patch.object(ch, '_wrote_this_post', return_value=False), \
                mock.patch.object(ch, 'delete_bulletin') as gone, \
                mock.patch.object(ch, 'handle_bulletin_command'):
            ch.handle_bulletin_own_delete_steps(4245, 'Y', self.iface, state, [])
        gone.assert_not_called()
        self.assertIn('not yours', self.sent[-1])

    def test_a_post_with_no_recorded_author_is_not_yours(self):
        """Posts predating v0.1.661 have no author. Unknown must not mean
        deletable."""
        with mock.patch.object(ch, 'get_post_author', return_value=''):
            self.assertFalse(ch._wrote_this_post(4245, self.iface, 'old-uid'))


class PostCountTests(unittest.TestCase):
    """F11 -- the profile's Msgs: counted keystrokes."""

    def setUp(self):
        import tempfile
        folder = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ,
                                  {'BBS_DB_PATH': os.path.join(folder, 'c.db')})
        patcher.start()
        self.addCleanup(patcher.stop)
        import db_operations
        self.db = db_operations
        self.db.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.db.get_db_connection().close()
        except Exception:
            pass

    def test_nothing_written_is_zero(self):
        """A brand-new account showed Msgs:4 straight after registering."""
        self.assertEqual(0, self.db.count_posts_by(['!aaaa1111'], ['someone']))

    def test_a_bulletin_counts_once(self):
        conn = self.db.get_db_connection()
        conn.execute("INSERT INTO bulletins (board, sender_short_name, date,"
                     " subject, content, unique_id, author_node_id)"
                     " VALUES ('General','someone','2026-09-20','s','c','u1','!aaaa1111')")
        conn.commit()
        self.assertEqual(1, self.db.count_posts_by(['!aaaa1111'], []))

    def test_an_old_post_counts_when_its_name_still_maps(self):
        """Posts before v0.1.661 carry no author, only a sender name."""
        conn = self.db.get_db_connection()
        conn.execute("INSERT INTO bulletins (board, sender_short_name, date,"
                     " subject, content, unique_id)"
                     " VALUES ('General','oldname','2026-01-01','s','c','u2')")
        conn.commit()
        self.assertEqual(1, self.db.count_posts_by([], ['oldname']))
        self.assertEqual(0, self.db.count_posts_by(['!aaaa1111'], []))

    def test_somebody_elses_post_is_not_counted(self):
        conn = self.db.get_db_connection()
        conn.execute("INSERT INTO bulletins (board, sender_short_name, date,"
                     " subject, content, unique_id, author_node_id)"
                     " VALUES ('General','them','2026-09-20','s','c','u3','!bbbb2222')")
        conn.commit()
        self.assertEqual(0, self.db.count_posts_by(['!aaaa1111'], ['someone']))

    def test_no_identity_counts_nothing(self):
        self.assertEqual(0, self.db.count_posts_by([], []))


if __name__ == '__main__':
    unittest.main()
