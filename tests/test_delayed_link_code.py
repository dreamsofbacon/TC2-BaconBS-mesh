"""Tests for the delayed link-code option.

A dual-boot device can't receive anything while it's rebooting into its
other protocol, so the delayed option queues the code and sends it to the
account's linked devices once the device has had time to come back.
"""
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import patch

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class QueueAndTakeTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def test_code_is_not_returned_before_its_delay_elapses(self):
        db_operations.queue_delayed_link_code("acct1", "123456", "!req", 2, 12)
        self.assertEqual(db_operations.take_due_link_codes(), [])

    def test_due_code_is_returned_with_its_details(self):
        db_operations.queue_delayed_link_code("acct1", "123456", "!req", 0, 12)
        due = db_operations.take_due_link_codes()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["account_id"], "acct1")
        self.assertEqual(due[0]["code"], "123456")
        self.assertEqual(due[0]["requested_by_node_id"], "!req")
        self.assertEqual(due[0]["ttl_minutes"], 12)

    def test_taking_a_code_consumes_it(self):
        """The row is deleted as it's handed over, so a delivery is
        attempted once -- re-sending the secret on every tick would be both
        noisy and a needless widening of how often it crosses the mesh."""
        db_operations.queue_delayed_link_code("acct1", "123456", "!req", 0, 12)
        self.assertEqual(len(db_operations.take_due_link_codes()), 1)
        self.assertEqual(db_operations.take_due_link_codes(), [])

    def test_only_due_codes_are_taken(self):
        db_operations.queue_delayed_link_code("a", "111111", "!x", 0, 12)
        db_operations.queue_delayed_link_code("b", "222222", "!y", 5, 12)
        due = db_operations.take_due_link_codes()
        self.assertEqual([d["code"] for d in due], ["111111"])


_CACHE_KEYS = ("config_init", "server", "radio_link")


def _install_fake_meshtastic():
    def _stub(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m
    mesh = _stub("meshtastic")
    mesh.BROADCAST_NUM = 0
    mesh.mesh_interface = _stub("meshtastic.mesh_interface")
    mesh.stream_interface = _stub("meshtastic.stream_interface")
    mesh.serial_interface = _stub("meshtastic.serial_interface")
    mesh.tcp_interface = _stub("meshtastic.tcp_interface")
    mesh.stream_interface.StreamInterface = object
    mesh.mesh_interface.MeshInterface = types.SimpleNamespace(MeshInterfaceError=Exception)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: sys.modules.pop(k, None) for k in _CACHE_KEYS}
        self._saved_mesh = {n: m for n, m in sys.modules.items()
                            if n == "meshtastic" or n.startswith("meshtastic.")}
        for n in list(self._saved_mesh):
            del sys.modules[n]
        _install_fake_meshtastic()
        import server as _server
        from radio_link import RadioLink as _RadioLink
        self.server, self.RadioLink = _server, _RadioLink

        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        for n in list(sys.modules):
            if n == "meshtastic" or n.startswith("meshtastic.") or n in _CACHE_KEYS:
                del sys.modules[n]
        sys.modules.update(self._saved_mesh)
        for k, m in self._saved.items():
            if m is not None:
                sys.modules[k] = m

    def _account_with(self, *node_ids):
        account_id = db_operations.create_account()
        for nid in node_ids:
            db_operations.link_node_to_account(nid, account_id, "meshtastic")
        db_operations.set_account_mail_relay(account_id, True)
        return account_id

    def _active_client(self, node_id, link_name="primary", protocol="Meshtastic"):
        db_operations.upsert_mesh_clients([{
            "link_name": link_name,
            "node_id": node_id,
            "node_num": node_id,
            "protocol": protocol,
            "short_name": "RCPT",
            "long_name": "Relay Recipient",
            "hw_model": "",
            "role": "CLIENT",
            "battery_level": None,
            "last_heard_epoch": None,
        }])

    def _due_mail(self, recipient="!alpha"):
        unique_id = db_operations.add_mail(
            "!sender", "Sender", recipient, "Relay subject", "Relay body", [], None
        )
        db_operations.get_db_connection().execute(
            "UPDATE mail_dm_deliveries SET not_before_epoch = 0 WHERE mail_unique_id = ?",
            (unique_id,),
        )
        db_operations.get_db_connection().commit()
        return unique_id

    def test_due_code_is_sent_to_every_linked_device(self):
        account_id = self._account_with("!alpha", "!beta")
        db_operations.queue_delayed_link_code(account_id, "654321", "!alpha", 0, 12)

        sent = []
        links = [self.RadioLink("primary", object())]
        with patch("utils.send_message", lambda text, node, iface: sent.append((node, text))):
            count = self.server.deliver_due_link_codes(links)

        self.assertEqual(count, 2)
        self.assertEqual({n for n, _t in sent}, {"!alpha", "!beta"})
        self.assertIn("654321", sent[0][1])

    def test_nothing_due_sends_nothing(self):
        account_id = self._account_with("!alpha")
        db_operations.queue_delayed_link_code(account_id, "654321", "!alpha", 5, 12)
        sent = []
        links = [self.RadioLink("primary", object())]
        with patch("utils.send_message", lambda text, node, iface: sent.append(node)):
            self.assertEqual(self.server.deliver_due_link_codes(links), 0)
        self.assertEqual(sent, [])

    def test_one_unreachable_device_does_not_block_the_others(self):
        account_id = self._account_with("!good", "!bad")
        db_operations.queue_delayed_link_code(account_id, "654321", "!good", 0, 12)

        delivered = []
        def flaky(text, node, iface):
            if node == "!bad":
                raise IOError("radio down")
            delivered.append(node)

        links = [self.RadioLink("primary", object())]
        with patch("utils.send_message", flaky):
            count = self.server.deliver_due_link_codes(links)

        self.assertEqual(delivered, ["!good"])
        self.assertEqual(count, 1)

    def test_mail_dm_uses_observed_link_and_marks_delivery(self):
        self._account_with("!alpha")
        self._active_client("!alpha", link_name="secondary")
        unique_id = self._due_mail()
        primary = self.RadioLink("primary", object())
        secondary_interface = object()
        secondary = self.RadioLink("secondary", secondary_interface)
        sent = []

        with patch("utils.send_message", lambda text, node, iface: sent.append((text, node, iface)) or True):
            count = self.server.deliver_due_mail_dms([primary, secondary])

        self.assertEqual(count, 1)
        self.assertEqual(sent[0][1:], ("!alpha", secondary_interface))
        self.assertIn("Relay subject", sent[0][0])
        self.assertIn("Relay body", sent[0][0])
        state = db_operations.get_db_connection().execute(
            "SELECT state FROM mail_dm_deliveries WHERE mail_unique_id = ?", (unique_id,)
        ).fetchone()[0]
        self.assertEqual(state, "delivered")

    def test_mail_dm_waits_without_attempt_when_target_is_offline(self):
        self._account_with("!alpha")
        unique_id = self._due_mail()
        sent = []

        with patch("utils.send_message", lambda *args: sent.append(args) or True):
            count = self.server.deliver_due_mail_dms([self.RadioLink("primary", object())])

        self.assertEqual(count, 0)
        self.assertEqual(sent, [])
        state, attempts = db_operations.get_db_connection().execute(
            "SELECT state, attempts FROM mail_dm_deliveries WHERE mail_unique_id = ?", (unique_id,)
        ).fetchone()
        self.assertEqual((state, attempts), ("pending", 0))

    def test_mail_dm_failed_send_remains_pending_with_attempt(self):
        self._account_with("!alpha")
        self._active_client("!alpha")
        unique_id = self._due_mail()

        with patch("utils.send_message", return_value=False):
            count = self.server.deliver_due_mail_dms([self.RadioLink("primary", object())])

        self.assertEqual(count, 0)
        state, attempts, error = db_operations.get_db_connection().execute(
            "SELECT state, attempts, last_error FROM mail_dm_deliveries WHERE mail_unique_id = ?",
            (unique_id,),
        ).fetchone()
        self.assertEqual((state, attempts), ("pending", 1))
        self.assertIn("false", error)

    def test_mail_dm_cancels_stale_queue_after_consent_is_revoked(self):
        account_id = self._account_with("!alpha")
        self._active_client("!alpha")
        unique_id = self._due_mail()
        db_operations.get_db_connection().execute(
            "UPDATE accounts SET mail_relay_enabled = 0 WHERE account_id = ?", (account_id,)
        )
        db_operations.get_db_connection().commit()
        sent = []

        with patch("utils.send_message", lambda *args: sent.append(args) or True):
            count = self.server.deliver_due_mail_dms([self.RadioLink("primary", object())])

        self.assertEqual(count, 0)
        self.assertEqual(sent, [])
        state = db_operations.get_db_connection().execute(
            "SELECT state FROM mail_dm_deliveries WHERE mail_unique_id = ?", (unique_id,)
        ).fetchone()[0]
        self.assertEqual(state, "cancelled")


if __name__ == "__main__":
    unittest.main()
