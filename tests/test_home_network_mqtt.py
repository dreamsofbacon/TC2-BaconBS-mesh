"""Tests for utils.home_network()'s third bucket (MQTT-bridged node ids)
and the storage-only call sites that persist its return value."""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import utils


class HomeNetworkThreeWayClassificationTests(unittest.TestCase):
    def test_meshtastic_shape(self):
        self.assertEqual(utils.home_network("!04058ac8"), "meshtastic")

    def test_meshcore_shape(self):
        self.assertEqual(utils.home_network("7e18ca9d30a1"), "meshcore")

    def test_mqtt_shape(self):
        self.assertEqual(utils.home_network("mqtt:baconbs/city-a-b:node-b"), "mqtt")

    def test_empty_or_none_falls_through_to_meshcore_default(self):
        # Preserves exact prior behavior for an unrecognized/empty id.
        self.assertEqual(utils.home_network(""), "meshcore")
        self.assertEqual(utils.home_network(None), "meshcore")

    def test_mqtt_prefix_without_full_shape_does_not_misclassify(self):
        # Only the real mqtt_interface._mqtt_node_id shape ("mqtt:...")
        # counts -- a bare id that merely starts with the letters "mqtt"
        # but isn't colon-delimited must not be silently treated as MQTT.
        self.assertEqual(utils.home_network("mqttish-node-name"), "meshcore")


class StorageOnlyCallSitesAcceptMqttTests(unittest.TestCase):
    """command_handlers.py's account-link call sites and web_admin.py's
    force_link action only STORE home_network()'s return value alongside a
    linked node id -- they must accept the new 'mqtt' value without error."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_link_node_to_account_accepts_mqtt_network_label(self):
        account_id = db_operations.create_account()
        node_id = "mqtt:baconbs/city-a-b:node-b"
        db_operations.link_node_to_account(node_id, account_id, utils.home_network(node_id))
        detail = db_operations.get_linked_nodes_detail(account_id)
        # (node_id, network, linked_at) tuples.
        networks = {row[1] for row in detail}
        self.assertIn("mqtt", networks)


if __name__ == "__main__":
    unittest.main()
