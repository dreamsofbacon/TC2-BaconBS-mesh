import sqlite3
import sys
import types
import os

# Stub meshtastic so db_operations can be imported without the hardware library
if "meshtastic" not in sys.modules:
    meshtastic_stub = types.ModuleType("meshtastic")
    setattr(meshtastic_stub, "BROADCAST_NUM", 0)
    sys.modules["meshtastic"] = meshtastic_stub

import db_operations
import op_log


def _new_mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    return conn


# ── Pure op_log unit tests ──────────────────────────────────────────────────

def test_ensure_op_log_schema_creates_required_tables():
    conn = _new_mem_conn()
    try:
        cursor = conn.cursor()
        op_log.ensure_op_log_schema(cursor)
        conn.commit()

        tables = {
            row[0]
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        assert 'op_log' in tables
        assert 'op_log_state' in tables
    finally:
        conn.close()


def test_append_local_event_allocates_per_origin_sequences():
    conn = _new_mem_conn()
    try:
        cursor = conn.cursor()
        first = op_log.append_local_event(
            cursor,
            origin_node_id='!A',
            event_type='upsert',
            scope='bulletins',
            target_uid='uid-1',
            payload={'k': 'v'},
            created_at='2026-05-16T21:00:00Z',
        )
        second = op_log.append_local_event(
            cursor,
            origin_node_id='!A',
            event_type='upsert',
            scope='bulletins',
            target_uid='uid-2',
            payload={'k': 'v2'},
            created_at='2026-05-16T21:00:01Z',
        )
        other_origin = op_log.append_local_event(
            cursor,
            origin_node_id='!B',
            event_type='upsert',
            scope='bulletins',
            target_uid='uid-3',
            payload={'k': 'v3'},
            created_at='2026-05-16T21:00:02Z',
        )

        assert first['origin_seq'] == 1
        assert second['origin_seq'] == 2
        assert other_origin['origin_seq'] == 1
    finally:
        conn.close()


def test_append_local_event_repairs_stale_sequence_state():
    conn = _new_mem_conn()
    try:
        cursor = conn.cursor()
        first = op_log.append_local_event(
            cursor,
            origin_node_id='!A',
            event_type='upsert',
            scope='bulletins',
            target_uid='uid-1',
            payload={'k': 'v'},
            created_at='2026-05-16T21:00:00Z',
        )
        cursor.execute(
            'UPDATE op_log_state SET next_seq = ? WHERE origin_node_id = ?',
            (first['origin_seq'], '!A'),
        )

        second = op_log.append_local_event(
            cursor,
            origin_node_id='!A',
            event_type='delete',
            scope='bulletins',
            target_uid='uid-1',
            payload={},
            created_at='2026-05-16T21:00:01Z',
        )

        assert second['origin_seq'] == 2
        assert cursor.execute(
            'SELECT next_seq FROM op_log_state WHERE origin_node_id = ?',
            ('!A',),
        ).fetchone()[0] == 3
    finally:
        conn.close()


def test_append_local_event_is_deterministic_for_same_inputs_and_sequence():
    conn1 = _new_mem_conn()
    conn2 = _new_mem_conn()
    try:
        event1 = op_log.append_local_event(
            conn1.cursor(),
            origin_node_id='!X',
            event_type='delete',
            scope='mail',
            target_uid='mail-1',
            payload={'reason': 'moderation', 'hard': True},
            created_at='2026-05-16T21:05:00Z',
            prev_event_id='prev-123',
        )
        event2 = op_log.append_local_event(
            conn2.cursor(),
            origin_node_id='!X',
            event_type='delete',
            scope='mail',
            target_uid='mail-1',
            payload={'hard': True, 'reason': 'moderation'},
            created_at='2026-05-16T21:05:00Z',
            prev_event_id='prev-123',
        )

        assert event1['event_id'] == event2['event_id']
        assert event1['content_hash'] == event2['content_hash']
    finally:
        conn1.close()
        conn2.close()


# ── Dual-write integration tests ────────────────────────────────────────────

class _TestDB:
    """Helper: sets up an in-memory DB for db_operations just like the existing tests."""
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        db_operations.thread_local.connection = self.conn
        db_operations.set_local_node_id('!test_node')
        db_operations.initialize_database()
        os.environ['BBS_OP_LOG_ENABLED'] = '1'

    def tearDown(self):
        os.environ.pop('BBS_OP_LOG_ENABLED', None)
        conn = getattr(db_operations.thread_local, 'connection', None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def op_log_rows(self, scope: str | None = None):
        q = 'SELECT origin_node_id, origin_seq, event_type, scope, target_uid FROM op_log'
        params = ()
        if scope:
            q += ' WHERE scope = ?'
            params = (scope,)
        return self.conn.execute(q, params).fetchall()


class TestBulletinDualWrite(_TestDB):
    def setup_method(self):
        self.setUp()

    def teardown_method(self):
        self.tearDown()

    def test_add_bulletin_writes_upsert_event(self):
        uid = db_operations.add_bulletin(
            'General', 'Tester', 'Hello', 'World body',
            bbs_nodes=[], interface=None,
        )
        rows = self.op_log_rows('bulletins')
        assert len(rows) == 1
        assert rows[0][2] == 'upsert'
        assert rows[0][4] == uid

    def test_delete_bulletin_writes_delete_event(self):
        uid = db_operations.add_bulletin(
            'General', 'Tester', 'Hello', 'World body',
            bbs_nodes=[], interface=None,
        )
        db_operations.delete_bulletin(uid, bbs_nodes=[], interface=None)
        rows = self.op_log_rows('bulletins')
        event_types = {r[2] for r in rows}
        assert 'delete' in event_types

    def test_synced_delete_bulletin_does_not_write_local_delete_event(self):
        uid = db_operations.add_bulletin(
            'General', 'Tester', 'Hello', 'World body',
            bbs_nodes=[], interface=None,
        )
        before = len(self.op_log_rows('bulletins'))
        db_operations.delete_bulletin(
            uid, bbs_nodes=[], interface=None, sync_received=True)
        assert len(self.op_log_rows('bulletins')) == before

    def test_dual_write_disabled_by_env_produces_no_op_log_rows(self):
        os.environ['BBS_OP_LOG_ENABLED'] = '0'
        db_operations.add_bulletin(
            'General', 'Tester', 'No log', 'Content',
            bbs_nodes=[], interface=None,
        )
        assert self.op_log_rows('bulletins') == []


class TestMailDualWrite(_TestDB):
    def setup_method(self):
        self.setUp()

    def teardown_method(self):
        self.tearDown()

    def test_add_mail_writes_upsert_event(self):
        uid = db_operations.add_mail(
            '!abc', 'Alice', '!def', 'Subj', 'Body',
            bbs_nodes=[], interface=None,
        )
        rows = self.op_log_rows('mail')
        assert len(rows) == 1
        assert rows[0][2] == 'upsert'
        assert rows[0][4] == uid

    def test_delete_mail_writes_delete_event(self):
        uid = db_operations.add_mail(
            '!abc', 'Alice', '!def', 'Subj', 'Body',
            bbs_nodes=[], interface=None,
        )
        db_operations.delete_mail(uid, recipient_id=None, bbs_nodes=[], interface=None)
        rows = self.op_log_rows('mail')
        event_types = {r[2] for r in rows}
        assert 'delete' in event_types

    def test_synced_delete_mail_does_not_write_local_delete_event(self):
        uid = db_operations.add_mail(
            '!abc', 'Alice', '!def', 'Subj', 'Body',
            bbs_nodes=[], interface=None,
        )
        before = len(self.op_log_rows('mail'))
        db_operations.delete_mail(
            uid, recipient_id=None, bbs_nodes=[], interface=None,
            sync_received=True,
        )
        assert len(self.op_log_rows('mail')) == before


class TestChannelCommentDualWrite(_TestDB):
    def setup_method(self):
        self.setUp()

    def teardown_method(self):
        self.tearDown()

    def test_add_channel_comment_writes_upsert_event(self):
        db_operations.add_channel('TestChan', 'meshtastic://test', bbs_nodes=[], interface=None)
        channel_id = db_operations.get_channel_id_by_name_url('TestChan', 'meshtastic://test')
        uid = db_operations.add_channel_comment(channel_id, 'Tester', 'A comment')
        rows = self.op_log_rows('channel_comments')
        assert len(rows) == 1
        assert rows[0][2] == 'upsert'
        assert rows[0][4] == uid

    def test_delete_channel_comment_writes_delete_event(self):
        db_operations.add_channel('TestChan', 'meshtastic://test', bbs_nodes=[], interface=None)
        channel_id = db_operations.get_channel_id_by_name_url('TestChan', 'meshtastic://test')
        uid = db_operations.add_channel_comment(channel_id, 'Tester', 'A comment')
        db_operations.delete_channel_comment(uid, bbs_nodes=[], interface=None)
        rows = self.op_log_rows('channel_comments')
        event_types = {r[2] for r in rows}
        assert 'delete' in event_types

    def test_synced_delete_channel_comment_does_not_write_local_delete_event(self):
        db_operations.add_channel('TestChan', 'meshtastic://test', bbs_nodes=[], interface=None)
        channel_id = db_operations.get_channel_id_by_name_url('TestChan', 'meshtastic://test')
        uid = db_operations.add_channel_comment(channel_id, 'Tester', 'A comment')
        before = len(self.op_log_rows('channel_comments'))
        db_operations.delete_channel_comment(
            uid, bbs_nodes=[], interface=None, sync_received=True)
        assert len(self.op_log_rows('channel_comments')) == before

