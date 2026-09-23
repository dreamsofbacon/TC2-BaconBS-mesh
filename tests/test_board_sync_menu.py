"""Setting a board's audience from a radio or over SSH.

Two things this has to get right beyond the happy path. Only an admin may
change where a board's posts travel, and the role is re-checked on every
reply rather than once when the screen opened -- a session outlives the role
that opened it. And ordinary readers must be told, on the screen where they
choose a board, that a board's posts stay put: they decide what to write
from that screen, so finding out afterwards is finding out too late.
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
import utils

SENDER = 7001
NODE = "!admin001"
PEER_A = "!aaaa1111"
PEER_B = "!bbbb2222"


class _Iface:
    protocol_name = "meshtastic"
    max_text_bytes = 220

    def __init__(self):
        self.nodes = {NODE: {'num': SENDER}}
        self.bbs_nodes = [PEER_A, PEER_B]


class _Board(unittest.TestCase):
    role = 'admin'

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()
        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        db_operations.set_node_role(NODE, self.role)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        ch.send_message = self._real_send
        utils.user_states.pop(SENDER, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def open_boards(self):
        self.sent.clear()
        ch.handle_bulletin_command(SENDER, self.iface)
        return self.sent[-1]

    def say(self, text, step=1):
        self.sent.clear()
        state = utils.get_user_state(SENDER) or {}
        if state.get('command') == 'BOARD_SYNC':
            ch.handle_board_sync_steps(SENDER, text, self.iface, state)
        else:
            ch.handle_bb_steps(SENDER, text, step, state, self.iface, [])
        return self.sent[-1] if self.sent else ""

    def state(self):
        return utils.get_user_state(SENDER) or {}


class OnlyAdminsTests(_Board):
    role = 'mod'

    def test_a_moderator_is_not_offered_the_key(self):
        self.assertNotIn("[S]ync", self.open_boards())

    def test_a_moderator_pressing_s_does_not_reach_it(self):
        self.open_boards()
        self.say('s')
        self.assertNotEqual('BOARD_SYNC', self.state().get('command'))

    def test_the_role_is_rechecked_on_every_reply(self):
        """Opened as an admin, demoted mid-session: the next reply stops."""
        db_operations.set_node_role(NODE, 'admin')
        self.open_boards()
        self.say('s')
        self.assertEqual('BOARD_SYNC', self.state().get('command'))
        db_operations.set_node_role(NODE, 'user')
        self.say('1')
        self.assertIn("admin", " ".join(self.sent).lower())
        self.assertNotEqual('BOARD_SYNC', self.state().get('command'))


class SettingItTests(_Board):
    def test_an_admin_sees_the_key(self):
        self.assertIn("[S]ync setup", self.open_boards())

    def test_this_node_only(self):
        self.open_boards()
        self.say('s')
        self.say('1')            # first board
        self.say('2')            # this node only
        board = db_operations.get_bulletin_boards()[0] if hasattr(
            db_operations, 'get_bulletin_boards') else ch.get_bulletin_boards()[0]
        self.assertEqual(('local', []), db_operations.get_board_audience(board))

    def test_choosing_nodes(self):
        self.open_boards()
        self.say('s')
        self.say('1')
        self.say('3')            # chosen nodes
        self.say('1')            # toggle the first peer
        self.say('d')            # done
        board = ch.get_bulletin_boards()[0]
        audience, peers = db_operations.get_board_audience(board)
        self.assertEqual('peers', audience)
        self.assertEqual([PEER_A], peers)

    def test_choosing_none_says_so_rather_than_silently_meaning_everywhere(self):
        self.open_boards()
        self.say('s')
        self.say('1')
        self.say('3')
        self.say('d')
        self.assertIn("stay here", " ".join(self.sent))
        self.assertEqual(('local', []),
                         db_operations.get_board_audience(ch.get_bulletin_boards()[0]))

    def test_back_to_everywhere(self):
        board = ch.get_bulletin_boards()[0]
        db_operations.set_board_audience(board, 'local', [])
        self.open_boards()
        self.say('s')
        self.say('1')
        self.say('1')            # everywhere
        self.assertEqual(('all', []), db_operations.get_board_audience(board))

    def test_zero_backs_out_one_level_at_a_time(self):
        self.open_boards()
        self.say('s')
        self.say('1')
        self.assertEqual(2, self.state().get('step'))
        self.say('0')
        self.assertEqual(1, self.state().get('step'))
        self.say('0')
        self.assertNotEqual('BOARD_SYNC', self.state().get('command'))


class ReadersAreToldTests(_Board):
    role = 'user'

    def test_a_restricted_board_is_marked_on_the_board_list(self):
        board = ch.get_bulletin_boards()[0]
        db_operations.set_board_audience(board, 'local', [])
        screen = self.open_boards()
        self.assertIn(f"{board}*", screen)
        self.assertIn("stays on chosen nodes", screen)

    def test_an_ordinary_board_list_says_nothing_extra(self):
        screen = self.open_boards()
        self.assertNotIn("*", screen)
        self.assertNotIn("stays on chosen nodes", screen)


class PacketBudgetTests(_Board):
    """Every screen here is read on a radio. Two packets is the cap the rest
    of the BBS holds itself to."""

    def _assert_fits(self, screen, limit=320):
        self.assertLessEqual(len(screen.encode('utf-8')), limit,
                             f"screen is {len(screen.encode('utf-8'))} bytes:\n{screen}")

    def test_the_board_list_with_markers_and_the_admin_key_fits(self):
        for board in ch.get_bulletin_boards():
            db_operations.set_board_audience(board, 'local', [])
        self._assert_fits(self.open_boards())

    def test_the_sync_menu_fits(self):
        self.open_boards()
        self._assert_fits(self.say('s'))

    def test_the_audience_screen_fits_in_one_packet(self):
        self.open_boards()
        self.say('s')
        self._assert_fits(self.say('1'), 200)

    def test_the_peer_screen_fits_in_one_packet(self):
        self.open_boards()
        self.say('s')
        self.say('1')
        self._assert_fits(self.say('3'), 200)


if __name__ == "__main__":
    unittest.main()
