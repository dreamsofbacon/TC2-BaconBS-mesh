"""Tests for Phase-2 op-log HAVE / WANT / EVENT discovery protocol (op_sync.py)."""

import sqlite3
import sys
import types
import os
from unittest.mock import MagicMock, patch

# Stub meshtastic before importing any BBS module
if "meshtastic" not in sys.modules:
    meshtastic_stub = types.ModuleType("meshtastic")
    setattr(meshtastic_stub, "BROADCAST_NUM", 0)
    sys.modules["meshtastic"] = meshtastic_stub

import db_operations
import op_log
import op_sync


# ── Helpers ───────────────────────────────────────────────────────────────────

def _setup_db():
    """Return an open in-memory connection wired into db_operations."""
    conn = sqlite3.connect(":memory:")
    db_operations.thread_local.connection = conn
    db_operations.set_local_node_id("!local_node")
    db_operations.initialize_database()
    os.environ["BBS_OP_LOG_ENABLED"] = "1"
    return conn


def _teardown_db():
    os.environ.pop("BBS_OP_LOG_ENABLED", None)
    conn = getattr(db_operations.thread_local, "connection", None)
    if conn is not None:
        conn.close()
        del db_operations.thread_local.connection


def _fake_interface():
    iface = MagicMock()
    iface.bbs_nodes = ["!peer_node"]
    iface.nodes = {}
    return iface


# ── build_have_frame ──────────────────────────────────────────────────────────

class TestBuildHaveFrame:
    def setup_method(self):
        self.conn = _setup_db()

    def teardown_method(self):
        _teardown_db()

    def test_returns_none_when_op_log_empty(self):
        frame = op_sync.build_have_frame("!local_node")
        assert frame is None

    def test_returns_have_frame_after_bulletin_created(self):
        db_operations.add_bulletin(
            "General", "Tester", "Hello", "Body text",
            bbs_nodes=[], interface=None,
        )
        frame = op_sync.build_have_frame("!local_node")
        assert frame is not None
        assert frame.startswith("HAVE|!local_node|")
        assert "bulletins:" in frame

    def test_frame_fits_within_220_bytes(self):
        for i in range(10):
            db_operations.add_bulletin("G", "T", f"Subject {i}", "Body", bbs_nodes=[], interface=None)
        db_operations.add_mail("!a", "Alice", "!b", "Subj", "Body", bbs_nodes=[], interface=None)
        frame = op_sync.build_have_frame("!local_node")
        assert frame is not None
        assert len(frame.encode("utf-8")) <= 220

    def test_none_local_node_returns_none(self):
        assert op_sync.build_have_frame("") is None
        assert op_sync.build_have_frame(None) is None


# ── handle_have ───────────────────────────────────────────────────────────────

class TestHandleHave:
    def setup_method(self):
        self.conn = _setup_db()

    def teardown_method(self):
        _teardown_db()

    def test_sends_want_when_peer_has_more(self):
        iface = _fake_interface()
        sent = []

        def fake_send(msg, dest, interface, pause_seconds=None):
            sent.append(msg)

        with patch("utils._send_one_sync", fake_send):
            with patch("utils.get_hash_repair_pause_seconds", return_value=0):
                # Peer advertises 3 bulletins; we have seen 0 from them
                parts = ["HAVE", "!peer_node", "bulletins:3"]
                op_sync.handle_have(parts, "!peer_node", "!local_node", iface)

        assert any(m.startswith("WANT|bulletins|!peer_node|1") for m in sent)

    def test_no_want_when_watermark_already_current(self):
        iface = _fake_interface()
        # Pre-seed the peer head to 3 so we are already current
        c = self.conn.cursor()
        op_log.update_peer_received_head(c, "!peer_node", "bulletins", 3)
        self.conn.commit()

        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            parts = ["HAVE", "!peer_node", "bulletins:3"]
            op_sync.handle_have(parts, "!peer_node", "!local_node", iface)

        assert not any(m.startswith("WANT|") for m in sent)

    def test_want_uses_next_seq_after_current_head(self):
        iface = _fake_interface()
        c = self.conn.cursor()
        op_log.update_peer_received_head(c, "!peer_node", "bulletins", 5)
        self.conn.commit()

        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            with patch("utils.get_hash_repair_pause_seconds", return_value=0):
                parts = ["HAVE", "!peer_node", "bulletins:8"]
                op_sync.handle_have(parts, "!peer_node", "!local_node", iface)

        # We have head=5, peer has 8; should WANT from seq 6
        assert any(m == "WANT|bulletins|!peer_node|6" for m in sent)


# ── handle_want ───────────────────────────────────────────────────────────────

class TestHandleWant:
    def setup_method(self):
        self.conn = _setup_db()

    def teardown_method(self):
        _teardown_db()

    def test_sends_event_frames_for_local_origin(self):
        uid = db_operations.add_bulletin(
            "General", "Tester", "Hello", "Body",
            bbs_nodes=[], interface=None,
        )
        iface = _fake_interface()
        sent = []

        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            with patch("utils.get_hash_repair_pause_seconds", return_value=0):
                parts = ["WANT", "bulletins", "!local_node", "1"]
                op_sync.handle_want(parts, "!peer_node", "!local_node", iface)

        event_frames = [m for m in sent if m.startswith("EVENT|")]
        assert len(event_frames) == 1
        assert f"|upsert|{uid}" in event_frames[0]

    def test_ignores_want_for_foreign_origin(self):
        iface = _fake_interface()
        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            parts = ["WANT", "bulletins", "!other_node", "1"]
            op_sync.handle_want(parts, "!peer_node", "!local_node", iface)
        assert not sent

    def test_sends_no_events_when_from_seq_exceeds_head(self):
        db_operations.add_bulletin("G", "T", "S", "B", bbs_nodes=[], interface=None)
        iface = _fake_interface()
        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            parts = ["WANT", "bulletins", "!local_node", "999"]
            op_sync.handle_want(parts, "!peer_node", "!local_node", iface)
        assert not sent


# ── handle_event ──────────────────────────────────────────────────────────────

class TestHandleEvent:
    def setup_method(self):
        self.conn = _setup_db()

    def teardown_method(self):
        _teardown_db()

    def test_issues_hashmiss_for_unknown_uid(self):
        iface = _fake_interface()
        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            with patch("utils.get_hash_repair_pause_seconds", return_value=0):
                parts = ["EVENT", "bulletins", "!peer_node", "1", "upsert", "unknown-uid-1234"]
                op_sync.handle_event(parts, "!peer_node", iface)

        assert any(m == "HASHMISS|bulletins|unknown-uid-1234" for m in sent)

    def test_no_hashmiss_for_already_present_uid(self):
        uid = db_operations.add_bulletin(
            "General", "Tester", "Hello", "Body",
            bbs_nodes=[], interface=None,
        )
        iface = _fake_interface()
        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            parts = ["EVENT", "bulletins", "!peer_node", "1", "upsert", uid]
            op_sync.handle_event(parts, "!peer_node", iface)

        assert not any(m.startswith("HASHMISS|") for m in sent)

    def test_advances_peer_head_watermark(self):
        iface = _fake_interface()
        with patch("utils._send_one_sync", lambda *a, **kw: None):
            parts = ["EVENT", "bulletins", "!peer_node", "7", "upsert", "some-uid"]
            op_sync.handle_event(parts, "!peer_node", iface)

        c = self.conn.cursor()
        head = op_log.get_peer_received_head(c, "!peer_node", "bulletins")
        assert head == 7

    def test_delete_event_does_not_issue_hashmiss(self):
        iface = _fake_interface()
        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            parts = ["EVENT", "bulletins", "!peer_node", "2", "delete", "some-uid"]
            op_sync.handle_event(parts, "!peer_node", iface)

        assert not any(m.startswith("HASHMISS|") for m in sent)

    def test_malformed_frame_ignored_gracefully(self):
        iface = _fake_interface()
        # Too few parts — should not raise
        op_sync.handle_event(["EVENT", "bulletins"], "!peer_node", iface)
        op_sync.handle_event(["EVENT", "bulletins", "!p", "bad_seq", "upsert", "uid"], "!peer_node", iface)
