"""Tests for command_handlers._urgent_board_allow_lists()'s generalization
from a hardcoded ('allow_list', 'allow_list2') tuple to a scan over every
[allow_list*] section -- the piece that lets an MQTT-linked sibling node
inherit urgent-board authorization the same way a dual-radio sibling does.

IMPORTANT: _urgent_board_allow_lists() calls config.read('config.ini')
internally. configparser.read() is ADDITIVE (it merges into existing
state, never clears it), so swapping in a fresh ConfigParser pre-loaded via
read_string() is not enough by itself -- its own .read() call would still
try to open a real 'config.ini' in the working directory and merge that in
too. Every test here also neutralizes .read() to a no-op AFTER pre-loading
sections, so this never touches the filesystem at all.
"""
import configparser
import unittest
from unittest import mock

import command_handlers


class _Iface:
    def __init__(self, allowed_nodes=None):
        self.allowed_nodes = allowed_nodes or []


class UrgentBoardAllowListMqttScanTests(unittest.TestCase):
    def _lists_for(self, body: str, interface=None) -> list:
        fresh_config = configparser.ConfigParser()
        fresh_config.read_string(body)
        with mock.patch.object(command_handlers, "config", fresh_config), \
             mock.patch.object(fresh_config, "read", lambda *a, **kw: None):
            return command_handlers._urgent_board_allow_lists(interface or _Iface())

    def test_picks_up_allow_list_and_allow_list2(self):
        body = (
            "[allow_list]\nallowed_nodes = !aaa11111\n\n"
            "[allow_list2]\nallowed_nodes = 7e18ca9d30a1\n"
        )
        lists = self._lists_for(body)
        self.assertIn(["!aaa11111"], lists)
        self.assertIn(["7e18ca9d30a1"], lists)

    def test_picks_up_multiple_mqtt_allow_list_sections(self):
        body = (
            "[allow_list]\nallowed_nodes = !aaa11111\n\n"
            "[allow_list_mqtt1]\nallowed_nodes = mqtt:baconbs/a:peer1\n\n"
            "[allow_list_mqtt2]\nallowed_nodes = mqtt:baconbs/b:peer2\n"
        )
        lists = self._lists_for(body)
        self.assertIn(["mqtt:baconbs/a:peer1"], lists)
        self.assertIn(["mqtt:baconbs/b:peer2"], lists)

    def test_unrelated_sections_are_not_picked_up(self):
        body = (
            "[allow_list]\nallowed_nodes = !aaa11111\n\n"
            "[gateway]\nallowed_nodes = !should-not-appear\n\n"
            "[sync_mqtt1]\nbbs_nodes = mqtt:baconbs/a:peer1\n"
        )
        lists = self._lists_for(body)
        flat = [n for lst in lists for n in lst]
        self.assertNotIn("!should-not-appear", flat)
        self.assertNotIn("mqtt:baconbs/a:peer1", flat)

    def test_no_allow_list_sections_behaves_like_before(self):
        lists = self._lists_for("[interface]\ntype = serial\n")
        # Only the interface's own (empty) allowed_nodes list.
        self.assertEqual(lists, [[]])

    def test_includes_interfaces_own_live_allowed_nodes_first(self):
        body = "[allow_list_mqtt1]\nallowed_nodes = mqtt:baconbs/a:peer1\n"
        lists = self._lists_for(body, interface=_Iface(allowed_nodes=["live-node"]))
        self.assertEqual(lists[0], ["live-node"])


if __name__ == "__main__":
    unittest.main()
