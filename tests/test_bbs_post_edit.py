"""Rewriting your own post from a radio or over SSH.

[E]dit sits next to [D]elete on a post you wrote. It collects the
replacement text the same way posting does, and the permission is checked
again when END arrives: the reply that republishes the post to every node is
not the reply that opened the screen.

Body only from a radio. Subject and board are a round trip each on a link
where the round trip is the expensive part, and the web admin edits those.
"""
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import command_handlers as ch
import db_operations
import utils

SENDER = 8100
NODE = "!writer001"
STRANGER = 8200
STRANGER_NODE = "!other0002"
LOCAL = "!self0000"


class _Iface:
    protocol_name = "meshtastic"
    max_text_bytes = 220
    bbs_nodes = []

    def __init__(self):
        self.nodes = {NODE: {'num': SENDER}, STRANGER_NODE: {'num': STRANGER}}


class _BbsEdit(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()
        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        patcher = mock.patch.object(db_operations, 'get_local_node_id',
                                    return_value=LOCAL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup)
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'):
            self.unique_id = db_operations.add_bulletin(
                "General", "writer", "my post", "the original text", [], None,
                author_node_id=NODE)

    def _cleanup(self):
        ch.send_message = self._real_send
        for sender in (SENDER, STRANGER):
            utils.user_states.pop(sender, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def open_post(self, sender=SENDER):
        """Read the post, which is where [E]dit is offered."""
        self.sent.clear()
        state = {'command': 'BULLETIN_READ', 'step': 3, 'board': 'General',
                 'boards': ['General'],
                 'bulletins': [(1, 'my post')]}
        utils.update_user_state(sender, state)
        with mock.patch.object(ch, 'get_bulletin_content', return_value=(
                'writer', '2026-09-01 10:00', 'my post', 'the original text',
                self.unique_id, 1, None)):
            ch.handle_bb_steps(sender, '1', 3, state, self.iface, [])
        return self.sent[-1]

    def say(self, text, sender=SENDER):
        self.sent.clear()
        state = utils.get_user_state(sender) or {}
        if state.get('command') == 'BULLETIN_OWN_EDIT':
            ch.handle_bulletin_own_edit_steps(sender, text, self.iface, state, [])
        else:
            ch.handle_bb_steps(sender, text, int(state.get('step', 3)), state,
                               self.iface, [])
        return self.sent[-1] if self.sent else ""

    def stored(self):
        return db_operations.get_db_connection().execute(
            "SELECT unique_id, subject, content FROM bulletins").fetchall()


class EditingYourOwnPostTests(_BbsEdit):
    def test_the_key_is_offered_on_a_post_you_wrote(self):
        self.assertIn("[E]dit", self.open_post())

    def test_the_text_is_replaced_and_republished(self):
        self.open_post()
        self.say('e')
        self.say('a new shorter text')
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes') as retract:
            self.say('END')
        retract.assert_called_once()
        rows = self.stored()
        self.assertEqual(1, len(rows))
        self.assertEqual("a new shorter text", rows[0][2])
        self.assertNotEqual(self.unique_id, rows[0][0])

    def test_several_lines_are_collected(self):
        self.open_post()
        self.say('e')
        self.say('line one')
        self.say('line two')
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes'):
            self.say('END')
        self.assertEqual("line one\nline two", self.stored()[0][2])

    def test_cancelling_leaves_the_post_alone(self):
        self.open_post()
        self.say('e')
        self.say('half a thought')
        self.say('!cancel')
        self.assertEqual("the original text", self.stored()[0][2])

    def test_sending_nothing_leaves_the_post_alone(self):
        self.open_post()
        self.say('e')
        self.say('END')
        self.assertEqual("the original text", self.stored()[0][2])
        self.assertIn("unchanged", " ".join(self.sent))

    def test_the_subject_and_author_survive(self):
        self.open_post()
        self.say('e')
        self.say('rewritten')
        with mock.patch.object(db_operations, 'send_bulletin_to_bbs_nodes'), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes'):
            self.say('END')
        row = db_operations.get_db_connection().execute(
            "SELECT subject, sender_short_name, author_node_id FROM bulletins").fetchone()
        self.assertEqual(('my post', 'writer', NODE), tuple(row))


class NotYourPostTests(_BbsEdit):
    def test_a_stranger_is_not_offered_the_key(self):
        self.assertNotIn("[E]dit", self.open_post(sender=STRANGER))

    def test_a_stranger_pressing_e_starts_nothing(self):
        """The key is only offered to the author, and the reading screen
        holds no post id for anyone else, so 'e' is just a bad list entry --
        exactly what [D]elete already does for a stranger."""
        self.open_post(sender=STRANGER)
        self.say('e', sender=STRANGER)
        state = utils.get_user_state(STRANGER) or {}
        self.assertNotEqual('BULLETIN_OWN_EDIT', state.get('command'))
        self.assertEqual("the original text", self.stored()[0][2])

    def test_the_permission_is_rechecked_when_the_text_arrives(self):
        """The reply that republishes is not the reply that opened the
        screen. Losing the right in between has to stop it."""
        self.open_post()
        self.say('e')
        self.say('rewritten')
        with mock.patch.object(db_operations, 'bulletin_edit_permission',
                               return_value=(False, "You can only edit posts you wrote.")), \
                mock.patch.object(db_operations, 'send_delete_bulletin_to_bbs_nodes') as retract:
            self.say('END')
        retract.assert_not_called()
        self.assertEqual("the original text", self.stored()[0][2])


if __name__ == "__main__":
    unittest.main()
