import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional


def is_op_log_enabled() -> bool:
    """Feature flag: set BBS_OP_LOG_ENABLED=0 to disable dual-write during rollout."""
    return os.environ.get('BBS_OP_LOG_ENABLED', '1').strip() not in ('0', 'false', 'False', 'no')


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _compute_event_id(
    origin_node_id: str,
    origin_seq: int,
    event_type: str,
    scope: str,
    target_uid: str,
    payload_json: str,
    created_at: str,
    prev_event_id: Optional[str],
) -> str:
    canonical = '|'.join(
        [
            str(origin_node_id),
            str(origin_seq),
            str(event_type),
            str(scope),
            str(target_uid),
            payload_json,
            str(created_at),
            str(prev_event_id or ''),
        ]
    )
    return hashlib.blake2b(canonical.encode('utf-8'), digest_size=16).hexdigest()


def ensure_op_log_schema(cursor) -> None:
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS op_log (
               origin_node_id TEXT NOT NULL,
               origin_seq INTEGER NOT NULL,
               event_id TEXT NOT NULL,
               event_type TEXT NOT NULL,
               scope TEXT NOT NULL,
               target_uid TEXT NOT NULL,
               payload TEXT NOT NULL,
               prev_event_id TEXT,
               created_at TEXT NOT NULL,
               content_hash TEXT NOT NULL,
               PRIMARY KEY (origin_node_id, origin_seq)
           );'''
    )
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS op_log_state (
               origin_node_id TEXT PRIMARY KEY,
               next_seq INTEGER NOT NULL
           );'''
    )
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_op_log_event_id_unique ON op_log(event_id);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_op_log_scope_target ON op_log(scope, target_uid);')


def _allocate_next_origin_seq(cursor, origin_node_id: str) -> int:
    row = cursor.execute(
        'SELECT next_seq FROM op_log_state WHERE origin_node_id = ?',
        (str(origin_node_id),),
    ).fetchone()
    if row is None:
        cursor.execute(
            'INSERT INTO op_log_state (origin_node_id, next_seq) VALUES (?, ?)',
            (str(origin_node_id), 2),
        )
        return 1

    next_seq = int(row[0])
    cursor.execute(
        'UPDATE op_log_state SET next_seq = ? WHERE origin_node_id = ?',
        (next_seq + 1, str(origin_node_id)),
    )
    return next_seq


def append_local_event(
    cursor,
    origin_node_id: str,
    event_type: str,
    scope: str,
    target_uid: str,
    payload: dict[str, Any],
    created_at: Optional[str] = None,
    prev_event_id: Optional[str] = None,
) -> dict[str, Any]:
    ensure_op_log_schema(cursor)

    origin_node = str(origin_node_id or '').strip()
    if not origin_node:
        raise ValueError('origin_node_id is required')

    sequence = _allocate_next_origin_seq(cursor, origin_node)
    created = str(created_at or _utc_now_iso())
    payload_json = _canonical_payload(payload or {})
    event_id = _compute_event_id(
        origin_node_id=origin_node,
        origin_seq=sequence,
        event_type=str(event_type),
        scope=str(scope),
        target_uid=str(target_uid),
        payload_json=payload_json,
        created_at=created,
        prev_event_id=prev_event_id,
    )

    cursor.execute(
        '''INSERT INTO op_log
           (origin_node_id, origin_seq, event_id, event_type, scope, target_uid, payload, prev_event_id, created_at, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            origin_node,
            sequence,
            event_id,
            str(event_type),
            str(scope),
            str(target_uid),
            payload_json,
            str(prev_event_id) if prev_event_id else None,
            created,
            event_id,
        ),
    )

    return {
        'origin_node_id': origin_node,
        'origin_seq': sequence,
        'event_id': event_id,
        'event_type': str(event_type),
        'scope': str(scope),
        'target_uid': str(target_uid),
        'payload': payload_json,
        'prev_event_id': str(prev_event_id) if prev_event_id else None,
        'created_at': created,
        'content_hash': event_id,
    }


def try_dual_write(
    cursor,
    origin_node_id: Optional[str],
    event_type: str,
    scope: str,
    target_uid: str,
    payload: dict[str, Any],
    created_at: Optional[str] = None,
    prev_event_id: Optional[str] = None,
) -> None:
    """Non-fatal dual-write: append an op_log event alongside an existing materialized-table write.

    If ``origin_node_id`` is None (node identity not yet resolved at startup) or
    the feature flag is off, this is a no-op.  Any exception is caught and
    logged at WARNING level so it never disrupts the primary write path.
    """
    if not is_op_log_enabled():
        return
    if not origin_node_id:
        return
    try:
        append_local_event(
            cursor,
            origin_node_id=str(origin_node_id),
            event_type=event_type,
            scope=scope,
            target_uid=target_uid,
            payload=payload,
            created_at=created_at,
            prev_event_id=prev_event_id,
        )
    except Exception as exc:
        logging.warning('op_log dual-write failed (%s %s %s): %s', event_type, scope, target_uid, exc)
