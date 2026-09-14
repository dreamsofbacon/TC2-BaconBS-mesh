"""Offline mail relay: delivered means the recipient's radio took it.

MeshCore users advert rarely, so waiting to hear from them before relaying
left mail sitting for days -- one MeshCore recipient on the live fleet had
mail pending for three days with zero attempts. MeshCore acknowledges direct
messages reliably, so the relay now asks: one chunk at a time, each only
after the previous one was ACKed, backing off when nobody answers.

Meshtastic ACKs are not reliable enough to depend on. There the relay still
waits to hear the recipient, sends the whole mail, counts it delivered at
once if every packet was ACKed, and otherwise sends it exactly once more the
next time the recipient is heard.
"""
import sqlite3
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import relay_ack

_CACHE_KEYS = ("config_init", "server", "radio_link")


def _install_fake_meshtastic():
    """Just enough of the meshtastic package for server to import (as in
    test_delayed_link_code)."""
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

MC_KEY = "ab" * 32
MT_NODE = "!0a1b2c3d"
MT_NUM = 0x0A1B2C3D
LONG_BODY = " ".join("word%03d" % i for i in range(90))


class _MeshCoreRadio:
    """Scripted MeshCore interface: each DM answers 'ack', 'noack' or 'error'."""
    protocol_name = "MeshCore"
    max_text_bytes = 160

    def __init__(self, contacts=(MC_KEY,), script=None):
        self.contacts = {c.lower() for c in contacts}
        self.script = list(script or [])
        self.sent = []
        self.nodes = {}
        self.bbs_nodes = []
        self.wakes = set()

    def has_contact(self, node_id):
        key = str(node_id).lower()
        return any(c.startswith(key) or key.startswith(c) for c in self.contacts)

    def take_wakes(self):
        wakes, self.wakes = self.wakes, set()
        return wakes

    def sendText(self, text=None, destinationId=None, wantAck=True, wantResponse=False):
        outcome = self.script.pop(0) if self.script else "ack"
        if outcome == "noack":
            raise relay_ack.RecipientNoAck("MeshCore recipient did not acknowledge")
        if outcome == "error":
            raise IOError("MeshCore send failed: table full")
        self.sent.append(text)
        return types.SimpleNamespace(id=len(self.sent))


class _MeshtasticRadio:
    protocol_name = "Meshtastic"
    max_text_bytes = 220

    def __init__(self, fail_on=None):
        self.sent = []
        self.nodes = {}
        self.bbs_nodes = []
        self.fail_on = fail_on
        self._next_id = 1000

    def sendText(self, text=None, destinationId=None, wantAck=True, wantResponse=False):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise IOError("serial write failed")
        self._next_id += 1
        self.sent.append((self._next_id, text))
        return types.SimpleNamespace(id=self._next_id)

    def ack(self, packet_id, sender=MT_NUM):
        relay_ack.on_routing_packet({
            "from": sender,
            "decoded": {"requestId": packet_id, "routing": {"errorReason": "NONE"}}}, None)


class _RelayCase(unittest.TestCase):
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
        relay_ack.reset()
        pace = patch("utils._pace_radio_send", lambda *a, **k: None)
        pace.start()
        self.addCleanup(pace.stop)
        receipts = patch("utils.send_mail_delivery_receipt_to_bbs_nodes")
        self.receipts = receipts.start()
        self.addCleanup(receipts.stop)

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

    @property
    def conn(self):
        return db_operations.get_db_connection()

    def queue_mail(self, node_id, network, body=LONG_BODY):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account(node_id, account_id, network)
        db_operations.set_account_mail_relay(account_id, True)
        unique_id = db_operations.add_mail("!sender", "Sender", node_id, "Subject", body, [], None)
        self.make_due()
        return unique_id

    def make_due(self):
        self.conn.execute("UPDATE mail_dm_deliveries SET not_before_epoch = 0")
        self.conn.commit()

    def row(self):
        cur = self.conn.execute(
            "SELECT state, attempts, unreachable_attempts, delivered_chunks, meshtastic_sends,"
            " not_before_epoch, last_error, first_sent_epoch FROM mail_dm_deliveries")
        keys = [d[0] for d in cur.description]
        return dict(zip(keys, cur.fetchone()))

    def deliver(self, *interfaces):
        links = [self.RadioLink(f"link{i}", iface) for i, iface in enumerate(interfaces)]
        return self.server.deliver_due_mail_dms(links)

    def chunks(self, radio):
        import utils
        unique_id = self.conn.execute("SELECT mail_unique_id FROM mail_dm_deliveries").fetchone()[0]
        entry = dict(mail_unique_id=unique_id, sender_short_name="Sender",
                     subject="Subject", content=LONG_BODY)
        return utils.message_chunks(self.server._relay_mail_text(entry), radio)


class MeshCoreDeliveryTests(_RelayCase):
    def test_every_chunk_acknowledged_is_delivered_with_a_receipt(self):
        self.queue_mail(MC_KEY, "meshcore")
        radio = _MeshCoreRadio()
        expected = self.chunks(radio)
        self.assertGreater(len(expected), 2, "the fixture must span several packets")
        self.assertEqual(self.deliver(radio), 1)
        self.assertEqual(radio.sent, expected)
        self.assertEqual(self.row()["state"], "delivered")
        self.assertEqual(self.receipts.call_count, 1)

    def test_no_ack_on_the_first_chunk_sends_nothing_more(self):
        self.queue_mail(MC_KEY, "meshcore")
        radio = _MeshCoreRadio(script=["noack"])
        before = int(time.time())
        self.assertEqual(self.deliver(radio), 0)
        self.assertEqual(radio.sent, [], "only the unanswered probe went out")
        row = self.row()
        self.assertEqual((row["state"], row["unreachable_attempts"], row["delivered_chunks"],
                          row["attempts"]), ("pending", 1, 0, 0))
        self.assertAlmostEqual(row["not_before_epoch"] - before, 600, delta=5)

    def test_the_backoff_ladder_settles_at_six_hours(self):
        self.queue_mail(MC_KEY, "meshcore")
        delays = []
        for _ in range(7):
            radio = _MeshCoreRadio(script=["noack"])
            before = int(time.time())
            self.deliver(radio)
            delays.append(round((self.row()["not_before_epoch"] - before) / 10) * 10)
            self.make_due()
        self.assertEqual(delays, [600, 1800, 3600, 10800, 21600, 21600, 21600])

    def test_a_partial_mail_resumes_where_it_stopped(self):
        self.queue_mail(MC_KEY, "meshcore")
        radio = _MeshCoreRadio(script=["ack", "ack", "noack"])
        expected = self.chunks(radio)
        self.deliver(radio)
        self.assertEqual(self.row()["delivered_chunks"], 2)
        self.make_due()
        later = _MeshCoreRadio()
        self.assertEqual(self.deliver(later), 1)
        self.assertEqual(later.sent, expected[2:], "nothing already acknowledged is resent")

    def test_a_radio_error_is_a_short_retry_not_the_ladder(self):
        self.queue_mail(MC_KEY, "meshcore")
        before = int(time.time())
        self.deliver(_MeshCoreRadio(script=["error"]))
        row = self.row()
        self.assertEqual((row["attempts"], row["unreachable_attempts"]), (1, 0))
        self.assertLessEqual(row["not_before_epoch"] - before, 305)

    def test_a_recipient_this_radio_has_no_contact_for_is_not_tried(self):
        self.queue_mail(MC_KEY, "meshcore")
        radio = _MeshCoreRadio(contacts=())
        before = int(time.time())
        self.assertEqual(self.deliver(radio), 0)
        self.assertEqual(radio.sent, [])
        row = self.row()
        self.assertEqual((row["attempts"], row["unreachable_attempts"]), (0, 0))
        self.assertAlmostEqual(row["not_before_epoch"] - before, 1800, delta=5)

    def test_the_link_that_has_the_contact_sends_it(self):
        self.queue_mail(MC_KEY, "meshcore")
        stranger, friend = _MeshCoreRadio(contacts=()), _MeshCoreRadio()
        self.assertEqual(self.deliver(stranger, friend), 1)
        self.assertEqual(stranger.sent, [])
        self.assertTrue(friend.sent)

    def test_no_advert_is_needed(self):
        """The live failure: never heard, but a contact, so it is tried."""
        self.queue_mail(MC_KEY, "meshcore")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM mesh_clients").fetchone()[0], 0)
        self.assertEqual(self.deliver(_MeshCoreRadio()), 1)

    def test_a_sign_of_life_makes_waiting_mail_due_at_once(self):
        prefix = MC_KEY[:12]
        self.queue_mail(prefix, "meshcore")
        self.deliver(_MeshCoreRadio(script=["noack"]))
        self.assertGreater(self.row()["not_before_epoch"], time.time() + 500)
        radio = _MeshCoreRadio()
        radio.wakes = {MC_KEY}    # the radio reports the full key
        self.assertEqual(self.deliver(radio), 1)


class MeshtasticDeliveryTests(_RelayCase):
    def heard(self, epoch=None):
        db_operations.upsert_mesh_clients([{
            "link_name": "link0", "node_id": MT_NODE, "node_num": MT_NUM,
            "protocol": "Meshtastic", "short_name": "RCPT", "long_name": "Recipient",
            "hw_model": "", "role": "CLIENT", "battery_level": None,
            "last_heard_epoch": epoch if epoch is not None else int(time.time())}])

    def window_passed(self, seconds_ago=400):
        self.conn.execute("UPDATE mail_dm_deliveries SET first_sent_epoch = ?, not_before_epoch = 0",
                          (int(time.time()) - seconds_ago,))
        self.conn.commit()

    def test_not_heard_recently_is_not_sent(self):
        self.queue_mail(MT_NODE, "meshtastic")
        radio = _MeshtasticRadio()
        self.assertEqual(self.deliver(radio), 0)
        self.assertEqual(radio.sent, [])

    def test_every_packet_acknowledged_is_delivered_without_a_resend(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.assertEqual(self.deliver(radio), 0)
        self.assertEqual(self.row()["meshtastic_sends"], 1)
        for packet_id, _ in radio.sent:
            radio.ack(packet_id)
        self.make_due()
        sent = len(radio.sent)
        self.assertEqual(self.deliver(radio), 1)
        self.assertEqual(len(radio.sent), sent)
        self.assertEqual(self.row()["state"], "delivered")
        self.assertEqual(self.receipts.call_count, 1)

    def test_one_missing_ack_is_not_confirmation(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.deliver(radio)
        self.assertGreater(len(radio.sent), 1)
        for packet_id, _ in radio.sent[:-1]:
            radio.ack(packet_id)
        self.make_due()
        self.deliver(radio)
        self.assertEqual(self.row()["state"], "pending")

    def test_an_implicit_ack_or_a_nak_is_not_confirmation(self):
        relay_ack.track(77, MT_NODE)
        relay_ack.on_routing_packet({"from": 0x11111111, "decoded": {
            "requestId": 77, "routing": {"errorReason": "NONE"}}}, None)   # our own radio
        relay_ack.on_routing_packet({"from": MT_NUM, "decoded": {
            "requestId": 77, "routing": {"errorReason": "MAX_RETRANSMIT"}}}, None)
        self.assertFalse(relay_ack.acked([77]))
        relay_ack.on_routing_packet({"from": MT_NUM, "decoded": {
            "requestId": 77, "routing": {}}}, None)
        self.assertTrue(relay_ack.acked([77]))

    def test_the_listener_fits_under_the_bbs_receive_topic(self):
        """pypubsub makes a subtopic listener take every argument its parent
        requires. The BBS listens on meshtastic.receive with (packet,
        interface); a listener with an optional interface made subscribing
        raise, which would have silently disabled ACK tracking on the node."""
        from pubsub import pub

        def bbs_receive(packet, interface):
            pass
        pub.subscribe(bbs_receive, "relayacktest.receive")
        pub.subscribe(relay_ack.on_routing_packet, "relayacktest.receive.routing")
        relay_ack.track(91, MT_NODE)
        pub.sendMessage("relayacktest.receive.routing", interface=object(), packet={
            "from": MT_NUM, "decoded": {"requestId": 91, "routing": {"errorReason": "NONE"}}})
        self.assertTrue(relay_ack.acked([91]))

    def test_no_resend_inside_the_ack_window(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.deliver(radio)
        sent = len(radio.sent)
        self.make_due()
        self.heard()
        self.deliver(radio)
        self.assertEqual(len(radio.sent), sent)
        self.assertEqual(self.row()["state"], "pending")

    def test_no_resend_until_heard_again_after_the_window(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.deliver(radio)
        sent = len(radio.sent)
        self.window_passed()
        self.heard(epoch=int(time.time()) - 1000)    # last heard before the send
        before = int(time.time())
        self.deliver(radio)
        self.assertEqual(len(radio.sent), sent)
        self.assertAlmostEqual(self.row()["not_before_epoch"] - before, 300, delta=5)

    def test_heard_again_resends_once_then_it_is_delivered(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.deliver(radio)
        first = [text for _, text in radio.sent]
        self.window_passed()
        self.heard()
        self.assertEqual(self.deliver(radio), 1)
        self.assertEqual([text for _, text in radio.sent], first + first)
        self.assertEqual(self.row()["state"], "delivered")
        self.make_due()
        self.heard()
        self.deliver(radio)
        self.assertEqual(len(radio.sent), 2 * len(first), "never a third send")

    def test_a_direct_message_counts_as_heard_again(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.deliver(radio)
        self.window_passed()
        self.heard(epoch=int(time.time()) - 1000)
        db_operations.wake_mail_dm_deliveries(MT_NODE)
        self.assertEqual(self.deliver(radio), 1)

    def test_a_restart_between_sends_means_one_resend(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio()
        self.deliver(radio)
        for packet_id, _ in radio.sent:
            radio.ack(packet_id)
        relay_ack.reset()                 # the ACKs were only ever in memory
        self.window_passed()
        self.heard()
        sent = len(radio.sent)
        self.assertEqual(self.deliver(radio), 1)
        self.assertEqual(len(radio.sent), 2 * sent)
        self.assertEqual(self.row()["state"], "delivered")

    def test_a_first_send_cut_short_keeps_its_retry(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.heard()
        radio = _MeshtasticRadio(fail_on=1)
        self.deliver(radio)
        self.assertEqual(len(radio.sent), 1)
        for packet_id, _ in radio.sent:
            radio.ack(packet_id)
        row = self.row()
        self.assertEqual((row["state"], row["meshtastic_sends"]), ("pending", 1))
        self.make_due()
        self.deliver(radio)
        self.assertEqual(self.row()["state"], "pending", "a partial send is never confirmed")
        radio.fail_on = None
        self.window_passed()
        self.heard()
        self.assertEqual(self.deliver(radio), 1)
        self.assertEqual(len(radio.sent), 1 + len(self.chunks(radio)))


class QueueTests(_RelayCase):
    def test_mail_older_than_seven_days_stops_being_relayed(self):
        self.queue_mail(MC_KEY, "meshcore")
        old = (datetime.now(timezone.utc) - timedelta(days=7, minutes=1)).isoformat()
        self.conn.execute("UPDATE mail_dm_deliveries SET created_at = ?", (old,))
        self.conn.commit()
        radio = _MeshCoreRadio()
        self.assertEqual(self.deliver(radio), 0)
        self.assertEqual(radio.sent, [])
        row = self.row()
        self.assertEqual(row["state"], "expired")
        self.assertIn("still in mailbox", row["last_error"])

    def test_six_days_old_is_still_relayed(self):
        self.queue_mail(MC_KEY, "meshcore")
        recent = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        self.conn.execute("UPDATE mail_dm_deliveries SET created_at = ?", (recent,))
        self.conn.commit()
        self.assertEqual(self.deliver(_MeshCoreRadio()), 1)

    def test_an_ssh_identity_is_never_queued(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("ssh:" + "f" * 12, account_id, "ssh")
        db_operations.link_node_to_account(MC_KEY, account_id, "meshcore")
        db_operations.set_account_mail_relay(account_id, True)
        db_operations.add_mail("!sender", "Sender", MC_KEY, "S", "B", [], None)
        targets = [r[0] for r in self.conn.execute("SELECT target_node_id FROM mail_dm_deliveries")]
        self.assertEqual(targets, [MC_KEY])

    def test_queued_non_radio_rows_are_cancelled(self):
        """The 21 ssh: rows pending forever on the live fleet."""
        unique_id = self.queue_mail(MC_KEY, "meshcore")
        self.conn.execute(
            "INSERT INTO mail_dm_deliveries (mail_unique_id, target_node_id, created_at)"
            " VALUES (?, 'ssh:ffffffffffff', ?)", (unique_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        self.assertEqual(db_operations.cancel_non_radio_mail_dm_deliveries(), 1)
        state = self.conn.execute(
            "SELECT state FROM mail_dm_deliveries WHERE target_node_id LIKE 'ssh:%'").fetchone()[0]
        self.assertEqual(state, "cancelled")

    def test_the_delivery_loop_cancels_a_non_radio_row_too(self):
        unique_id = self.queue_mail(MC_KEY, "meshcore")
        self.conn.execute("DELETE FROM mail_dm_deliveries")
        self.conn.execute(
            "INSERT INTO mail_dm_deliveries (mail_unique_id, target_node_id, created_at)"
            " VALUES (?, 'ssh:ffffffffffff', ?)", (unique_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        with patch("db_operations.get_mail_relay_preference", return_value=True):
            self.deliver(_MeshCoreRadio())
        self.assertEqual(self.row()["state"], "cancelled")

    def test_maintenance_runs_the_cleanup(self):
        summary = db_operations.run_db_maintenance()
        self.assertIn("mail_relay_cancelled_non_radio", summary)
        self.assertIn("mail_relay_expired", summary)

    def test_a_wake_matches_a_meshcore_key_either_way_round(self):
        self.queue_mail(MC_KEY[:12], "meshcore")
        self.conn.execute("UPDATE mail_dm_deliveries SET not_before_epoch = ?", (int(time.time()) + 9999,))
        self.conn.commit()
        self.assertEqual(db_operations.wake_mail_dm_deliveries(MC_KEY), 1)
        self.assertLessEqual(self.row()["not_before_epoch"], time.time())
        self.assertEqual(db_operations.wake_mail_dm_deliveries("cd" * 32), 0)
        self.assertEqual(db_operations.wake_mail_dm_deliveries("ab"), 0, "too short to match")

    def test_a_meshtastic_wake_is_exact(self):
        self.queue_mail(MT_NODE, "meshtastic")
        self.assertEqual(db_operations.wake_mail_dm_deliveries("!0a1b2c3"), 0)
        self.assertEqual(db_operations.wake_mail_dm_deliveries(MT_NODE), 1)

    def test_a_roster_sweep_without_a_heard_time_keeps_the_old_one(self):
        row = {"link_name": "link0", "node_id": MC_KEY, "node_num": 1, "protocol": "MeshCore",
               "short_name": "", "long_name": "", "hw_model": "", "role": "", "battery_level": None,
               "last_heard_epoch": 1_790_000_000}
        db_operations.upsert_mesh_clients([row])
        db_operations.upsert_mesh_clients([dict(row, last_heard_epoch=None)])
        self.assertEqual(self.conn.execute(
            "SELECT last_heard_epoch FROM mesh_clients").fetchone()[0], 1_790_000_000)

    def test_a_direct_message_to_the_bbs_wakes_its_sender(self):
        import inspect
        import message_processing
        source = inspect.getsource(message_processing.on_receive)
        accepted = source.index('"Accepted direct message"')
        self.assertLess(accepted, source.index("wake_mail_dm_deliveries(sender_node_id)"))

    def test_the_ack_tracker_is_subscribed_at_startup(self):
        import inspect
        source = inspect.getsource(self.server)
        self.assertIn("relay_ack.subscribe(", source)


if __name__ == "__main__":
    unittest.main()
