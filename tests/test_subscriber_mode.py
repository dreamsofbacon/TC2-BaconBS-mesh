"""Gateway subscriber-mode routing: a pull-only node's WANT/HASHMISS are
accepted as sync, but it can't push records to us, and non-listed nodes are
unaffected."""

import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import message_processing as mp


class _MyInfo:
    my_node_num = 123


class _Iface:
    def __init__(self):
        self.bbs_nodes = ["!peer"]
        self.allowed_nodes = []
        self.subscriber_nodes = ["!pico"]
        self.myInfo = _MyInfo()
        self.nodes = {}


def _packet(text, from_id, to=123):
    return {"decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode()},
            "from": 1, "to": to, "fromId": from_id}


class SubscriberRoutingTests(unittest.TestCase):
    def setUp(self):
        self._orig = {k: getattr(mp, k) for k in (
            "get_node_short_name", "get_node_id_from_num", "log_connection_event",
            "log_sync_transmission", "process_message")}
        mp.get_node_short_name = lambda *a, **k: "x"
        mp.get_node_id_from_num = lambda *a, **k: "x"
        mp.log_connection_event = lambda *a, **k: None
        mp.log_sync_transmission = lambda *a, **k: None
        self.calls = []
        mp.process_message = lambda sender_id, msg, iface, is_sync_message=False, sender_node_id=None: \
            self.calls.append((is_sync_message, sender_node_id, msg))

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(mp, k, v)

    def test_subscriber_want_accepted_as_sync(self):
        mp.on_receive(_packet("WANT|bulletins|!gw|1", "!pico"), _Iface())
        self.assertEqual(self.calls, [(True, "!pico", "WANT|bulletins|!gw|1")])

    def test_subscriber_hashmiss_accepted_as_sync(self):
        mp.on_receive(_packet("HASHMISS|mail|m1", "!pico"), _Iface())
        self.assertEqual(self.calls, [(True, "!pico", "HASHMISS|mail|m1")])

    def test_subscriber_cannot_push_record(self):
        # A BULLETIN from a subscriber is NOT applied as sync; it falls through to
        # the direct-message (user) path instead (is_sync_message False).
        mp.on_receive(_packet("BULLETIN|G|S|x|body|uid", "!pico"), _Iface())
        self.assertEqual(self.calls, [(False, "!pico", "BULLETIN|G|S|x|body|uid")])

    def test_non_subscriber_want_not_synced(self):
        # A stranger DMing a WANT is treated as a plain user message, not sync.
        mp.on_receive(_packet("WANT|bulletins|!gw|1", "!stranger"), _Iface())
        self.assertEqual(self.calls, [(False, "!stranger", "WANT|bulletins|!gw|1")])

    def test_bbs_peer_still_full_sync(self):
        # An actual bbs_node is unaffected — its sync frames are processed as sync.
        mp.on_receive(_packet("WANT|bulletins|!gw|1", "!peer"), _Iface())
        self.assertEqual(self.calls, [(True, "!peer", "WANT|bulletins|!gw|1")])


if __name__ == "__main__":
    unittest.main()
