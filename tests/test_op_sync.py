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


class _FakeLowLatencyInterface:
    """Mirrors tests/test_mqtt_turbo_pacing.py's fake -- a low-latency
    transport like MQTT, which utils._effective_turbo() detects via this
    single attribute."""
    is_low_latency = True


class _FakeNormalInterface:
    pass  # no is_low_latency attribute -- must behave like plain LoRa


class _FakeLowBudgetInterface:
    """A transport with a tiny per-message byte budget, to exercise
    build_have_frame's oversized-frame trim path deterministically."""
    max_text_bytes = 32


class _FakeHighBudgetInterface:
    """An MQTT-sized byte budget -- large enough that build_have_frame
    should never need to trim scopes."""
    max_text_bytes = 32768


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

    def _seed_all_three_scopes(self):
        c = self.conn.cursor()
        for scope in ("bulletins", "mail", "channel_comments"):
            op_log.append_local_event(
                c, origin_node_id="!local_node", event_type="upsert",
                scope=scope, target_uid=f"uid-{scope}", payload={},
            )
        self.conn.commit()

    def test_trims_to_two_scopes_when_frame_exceeds_transport_budget(self):
        self._seed_all_three_scopes()
        frame = op_sync.build_have_frame("!local_node", interface=_FakeLowBudgetInterface())
        assert frame is not None
        assert "bulletins:" in frame
        assert "mail:" in frame
        assert "channel_comments:" not in frame  # trimmed by the tiny budget

    def test_does_not_trim_when_transport_budget_is_large(self):
        self._seed_all_three_scopes()
        frame = op_sync.build_have_frame("!local_node", interface=_FakeHighBudgetInterface())
        assert frame is not None
        assert "bulletins:" in frame
        assert "mail:" in frame
        assert "channel_comments:" in frame  # MQTT-sized budget needs no trim


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

    def _seed_bulletin_events(self, count):
        c = self.conn.cursor()
        for i in range(count):
            op_log.append_local_event(
                c, origin_node_id="!local_node", event_type="upsert",
                scope="bulletins", target_uid=f"uid-{i}", payload={},
            )
        self.conn.commit()

    def test_caps_events_per_want_at_the_normal_reconcile_limit(self):
        # 25 events available, but a plain (non-low-latency) interface must
        # still cap at the normal reconcile-per-pass limit (20), unchanged
        # from the old flat _MAX_EVENTS_PER_WANT constant. Force the global
        # sync_turbo flag off so this is deterministic regardless of the
        # machine's own config.ini (mirrors test_mqtt_turbo_pacing.py).
        self._seed_bulletin_events(25)
        sent = []
        with patch("utils._is_sync_turbo_enabled", return_value=False):
            with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
                with patch("utils.get_hash_repair_pause_seconds", return_value=0):
                    parts = ["WANT", "bulletins", "!local_node", "1"]
                    op_sync.handle_want(parts, "!peer_node", "!local_node", _FakeNormalInterface())
        event_frames = [m for m in sent if m.startswith("EVENT|")]
        assert len(event_frames) == 20

    def test_caps_events_per_want_at_the_turbo_reconcile_limit_for_mqtt(self):
        # Same 25 events, but a low-latency (e.g. MQTT) interface should use
        # the turbo reconcile-per-pass limit (100), so all 25 go out in one
        # WANT response instead of being throttled to 20.
        self._seed_bulletin_events(25)
        sent = []
        with patch("utils._send_one_sync", lambda *a, **kw: sent.append(a[0])):
            with patch("utils.get_hash_repair_pause_seconds", return_value=0):
                parts = ["WANT", "bulletins", "!local_node", "1"]
                op_sync.handle_want(parts, "!peer_node", "!local_node", _FakeLowLatencyInterface())
        event_frames = [m for m in sent if m.startswith("EVENT|")]
        assert len(event_frames) == 25


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


# ── backfill_op_log ────────────────────────────────────────────────────────────

class TestBackfillOpLog:
    def setup_method(self):
        self.conn = _setup_db()

    def teardown_method(self):
        _teardown_db()

    def test_backfill_creates_entries_for_local_records(self):
        # Create records normally (dual-write fires because source == local)
        # Then wipe op_log to simulate pre-Phase-2 state
        db_operations.add_bulletin("General", "Tester", "S1", "B1", bbs_nodes=[], interface=None)
        db_operations.add_bulletin("General", "Tester", "S2", "B2", bbs_nodes=[], interface=None)
        db_operations.add_mail("!local_node", "Alice", "!other", "Sub", "Body", bbs_nodes=[], interface=None)
        # Wipe op_log to simulate pre-Phase-2 state
        self.conn.execute("DELETE FROM op_log")
        self.conn.commit()
        assert self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0] == 0

        count = db_operations.run_op_log_backfill()
        assert count == 3
        assert self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0] == 3

    def test_backfill_is_idempotent(self):
        db_operations.add_bulletin("General", "T", "S", "B", bbs_nodes=[], interface=None)
        count1 = db_operations.run_op_log_backfill()
        count2 = db_operations.run_op_log_backfill()
        # Second call should find nothing new
        assert count2 == 0

    def test_backfill_skips_remote_origin_records(self):
        # Directly insert a bulletin with a foreign source_node_id (simulating a replicated record)
        import uuid
        uid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO bulletins (unique_id, board, sender_short_name, subject, content, date, source_node_id)"
            " VALUES (?, 'G', 'Remote', 'Sub', 'Body', '2026-01-01', '!remote_node')",
            (uid,),
        )
        self.conn.commit()

        count = db_operations.run_op_log_backfill()
        assert count == 0  # remote-origin record must not be backfilled

    def test_backfill_returns_zero_when_op_log_disabled(self):
        db_operations.add_bulletin("General", "T", "S", "B", bbs_nodes=[], interface=None)
        self.conn.execute("DELETE FROM op_log")
        self.conn.commit()
        os.environ["BBS_OP_LOG_ENABLED"] = "0"
        try:
            count = db_operations.run_op_log_backfill()
        finally:
            os.environ["BBS_OP_LOG_ENABLED"] = "1"
        assert count == 0
