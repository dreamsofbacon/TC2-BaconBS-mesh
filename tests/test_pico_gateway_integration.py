"""End-to-end interop: real gateway handlers <-> real Pico client, no hardware.

Wires the actual server-side sync handlers (message_processing.process_message
against an in-memory DB + op_log) to the actual Pico SyncClient (pico_node/),
passing frame strings between them as a fake radio would. This proves the two
independently-built halves actually speak the same protocol — the seam where
bugs hide and the thing we can't otherwise check until hardware exists.

Flow exercised (Option B, op_log subscriber pull):
  Pico --WANT--> gateway --EVENT--> Pico --HASHMISS--> gateway --BULLETIN--> Pico
and the bulletin lands, fully reassembled, in the Pico's cache.
"""

import os
import sys
import sqlite3
import tempfile
import shutil
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pico_node"))

import db_operations
import message_processing
import store as pico_store
import syncclient

GW = "!0408b778"
PICO = "!04059140"


class _GatewayIface:
    """Captures frames the gateway 'transmits' as (dest, text)."""
    def __init__(self):
        self.sent = []
        self.bbs_nodes = [GW]
        self.subscriber_nodes = [PICO]
        self.allowed_nodes = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent.append((destinationId, text))

    def drain(self):
        out = list(self.sent)
        self.sent = []
        return out


class PicoGatewayInteropTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        db_operations.set_local_node_id(GW)
        self.gw = _GatewayIface()
        self.dir = tempfile.mkdtemp()
        self.cache = pico_store.CacheStore(self.dir)
        self.client = syncclient.SyncClient(self.cache)

    def tearDown(self):
        db_operations.set_local_node_id(None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        shutil.rmtree(self.dir, ignore_errors=True)

    def _gw_recv(self, frame):
        """Deliver a frame from the Pico to the gateway sync handler."""
        message_processing.process_message(
            1, frame, self.gw, is_sync_message=True, sender_node_id=PICO)

    def _pico_recv_all(self, frames):
        """Feed gateway frames into the Pico; return frames the Pico wants to send."""
        out = []
        for _dest, text in frames:
            for reply in self.client.handle_frame(text):
                out.append(reply)
        return out

    def _seed_bulletin(self, uid, subject, content):
        # source_node_id == local makes it a locally-originated record, so an
        # op_log EVENT is recorded (what the WANT pull serves).
        db_operations.add_bulletin(
            "General", "CALL", subject, content, [], self.gw,
            unique_id=uid, date="2026-06-08 14:30",
            source_node_id=GW, source_timestamp="2026-06-08T14:30:05")

    def test_single_bulletin_syncs_into_pico_cache(self):
        self._seed_bulletin("uidX", "Hello", "the full bulletin body")

        # 1) Pico asks for the gateway's bulletins from seq 1.
        self._gw_recv("WANT|bulletins|%s|1" % GW)
        events = self.gw.drain()
        self.assertTrue(any(t.startswith("EVENT|bulletins|%s|" % GW) for _, t in events),
                        "gateway should answer WANT with EVENT frame(s)")

        # 2) Pico processes EVENTs -> emits HASHMISS for the record it lacks.
        hashmisses = self._pico_recv_all(events)
        self.assertIn("HASHMISS|bulletins|uidX", hashmisses)

        # 3) Gateway answers HASHMISS with the actual BULLETIN record frame(s).
        for hm in hashmisses:
            self._gw_recv(hm)
        records = self.gw.drain()
        self.assertTrue(any(t.startswith("BULLETIN|") for _, t in records))

        # 4) Pico parses the record into its cache.
        self._pico_recv_all(records)
        rows = self.cache.get("bulletins")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unique_id"], "uidX")
        self.assertEqual(rows[0]["subject"], "Hello")
        self.assertEqual(rows[0]["content"], "the full bulletin body")

    def test_multipacket_bulletin_reassembles_over_the_wire(self):
        big = "X" * 400  # forces BULLETIN + META + CONT frames
        self._seed_bulletin("uidBig", "Big", big)
        self._gw_recv("WANT|bulletins|%s|1" % GW)
        hashmisses = self._pico_recv_all(self.gw.drain())
        for hm in hashmisses:
            self._gw_recv(hm)
        self._pico_recv_all(self.gw.drain())
        rows = self.cache.get("bulletins")
        self.assertEqual(rows[0]["content"], big)  # fully reassembled

    def test_mail_syncs_into_pico_cache(self):
        db_operations.add_mail(
            "!sender", "BOB", "!recip", "Subj", "mail body here", [], self.gw,
            unique_id="m1", date="2026-06-08 14:30",
            source_node_id=GW, source_timestamp="2026-06-08T14:30:05")
        self._gw_recv("WANT|mail|%s|1" % GW)
        hashmisses = self._pico_recv_all(self.gw.drain())
        self.assertIn("HASHMISS|mail|m1", hashmisses)
        for hm in hashmisses:
            self._gw_recv(hm)
        self._pico_recv_all(self.gw.drain())
        rows = self.cache.get("mail")
        self.assertEqual(rows[0]["unique_id"], "m1")
        self.assertEqual(rows[0]["content"], "mail body here")

    def test_already_cached_record_no_hashmiss(self):
        self._seed_bulletin("dup", "Hi", "body")
        # Pre-seed the Pico cache with the same uid.
        self.cache.upsert("bulletins", {"unique_id": "dup", "date": "2026-06-08 14:30",
                                        "content": "body", "subject": "Hi"})
        self._gw_recv("WANT|bulletins|%s|1" % GW)
        hashmisses = self._pico_recv_all(self.gw.drain())
        self.assertEqual(hashmisses, [])  # nothing to fetch; watermark just advances
        self.assertEqual(self.cache.get_watermark(GW, "bulletins"), 1)


if __name__ == "__main__":
    unittest.main()
