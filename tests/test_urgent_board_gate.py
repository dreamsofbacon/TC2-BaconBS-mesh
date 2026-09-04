"""The Urgent board is radio-gated, including when no allow list is set.

Posting to Urgent broadcasts to every node in range and syncs to peers we do
not run. The gate treated an empty allow list as "no restriction configured"
and let anyone post -- correct for radio, where possession of a radio is the
credential and a stranger cannot cheaply become your neighbour's node.

It stopped being correct when SSH self-registration opened. An SSH identity
is issued by this node to anyone who asks for one. A field test proved it:
an account created minutes earlier posted to Urgent and the bulletin went
out to the mesh and synced to a third-party node, with no barrier at all.

docs/SSH-ACCESS.md already claimed a new account gets no urgent access.
Until this, that was only true when an allow list happened to be populated,
and on the live node all four were empty.
"""

import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations

RADIO = "!04058ac8"
SSH = "ssh:32eb457146764a2c97451ba4221caab6"
MQTT = "mqtt:baconbbsvt:Chattanooga"


class UrgentGateTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    # -- radio behaviour must not change -----------------------------------

    def test_a_radio_may_post_when_no_allow_list_is_configured(self):
        """The original default, deliberately kept: holding the radio is the
        credential, and this is how every existing node behaves."""
        self.assertTrue(ch.urgent_board_permitted(RADIO, [[]]))

    def test_an_allow_listed_radio_may_post(self):
        self.assertTrue(ch.urgent_board_permitted(RADIO, [[RADIO]]))

    def test_a_radio_off_the_list_may_not(self):
        self.assertFalse(ch.urgent_board_permitted(RADIO, [["!somebodyelse"]]))

    def test_an_mqtt_peer_is_unaffected(self):
        self.assertTrue(ch.urgent_board_permitted(MQTT, [[]]))

    # -- the hole this closes ----------------------------------------------

    def test_an_ssh_account_may_not_post_when_no_allow_list_is_set(self):
        """The live failure. Empty lists meant open to all, and an SSH
        identity is handed out to anyone who registers."""
        self.assertFalse(ch.urgent_board_permitted(SSH, [[]]))
        self.assertFalse(ch.urgent_board_permitted(SSH, [[], [], []]))

    def test_an_ssh_account_not_on_the_list_may_not_post(self):
        self.assertFalse(ch.urgent_board_permitted(SSH, [[RADIO]]))

    def test_an_ssh_account_with_an_allow_listed_linked_device_may_post(self):
        """Linking through the one-time code flow proves radio possession,
        which is the only proof this board has ever accepted. Shutting that
        door too would make the board unreachable rather than gated."""
        account_id = db_operations.create_account()
        db_operations.link_node_to_account(SSH, account_id, "ssh")
        db_operations.link_node_to_account(RADIO, account_id, "meshtastic")
        self.assertTrue(ch.urgent_board_permitted(SSH, [[RADIO]]))

    def test_linking_an_unlisted_device_is_not_enough(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account(SSH, account_id, "ssh")
        db_operations.link_node_to_account(RADIO, account_id, "meshtastic")
        self.assertFalse(ch.urgent_board_permitted(SSH, [["!somebodyelse"]]))

    # -- the refusal has to be intelligible --------------------------------

    def test_the_refusal_tells_an_ssh_user_what_would_work(self):
        message = ch._urgent_refusal(SSH)
        self.assertIn("radio-only", message)
        self.assertIn("Linked Devices", message)

    def test_a_radio_gets_the_original_wording(self):
        self.assertEqual(ch._urgent_refusal(RADIO),
                         "You don't have permission to post to this board.")


class UrgentPostFlowTests(unittest.TestCase):
    """Through the real handler, not just the predicate."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self.iface = types.SimpleNamespace(
            bbs_nodes=[], allowed_nodes=[],
            nodes={"!abc": {"num": 1234, "user": {"id": "!abc"}}})

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _post_attempt(self, node_id):
        state = {'command': 'BULLETIN_ACTION', 'step': 2,
                 'board': 'Urgent', 'boards': ['General', 'Urgent']}
        with mock.patch.object(ch, "send_message",
                               side_effect=lambda text, *_a, **_k: self.sent.append(text)), \
                mock.patch.object(ch, "get_node_id_from_num", return_value=node_id), \
                mock.patch.object(ch, "_urgent_board_allow_lists", return_value=[[]]):
            ch.update_user_state(1234, state)
            ch.handle_bb_steps(1234, 'p', 2, state, self.iface, [])
        return self.sent

    def test_an_ssh_user_is_refused_before_being_asked_for_a_subject(self):
        sent = self._post_attempt(SSH)
        self.assertIn("radio-only", sent[0])
        self.assertFalse(any("subject of your bulletin" in text for text in sent))
        self.assertNotEqual(ch.get_user_state(1234).get('command'), 'BULLETIN_POST')

    def test_a_radio_user_is_still_asked_for_a_subject(self):
        sent = self._post_attempt(RADIO)
        self.assertTrue(any("subject of your bulletin" in text for text in sent))
        self.assertEqual(ch.get_user_state(1234).get('command'), 'BULLETIN_POST')


if __name__ == "__main__":
    unittest.main()
