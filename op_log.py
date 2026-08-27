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
    # Tracks the highest seq we have *received* from each peer per scope via EVENT frames.
    # Separate from op_log_state (which only allocates seqs for locally-created events).
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS op_log_peer_head (
               peer_node_id TEXT NOT NULL,
               scope TEXT NOT NULL,
               max_received_seq INTEGER NOT NULL,
               PRIMARY KEY (peer_node_id, scope)
           );'''
    )
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_op_log_event_id_unique ON op_log(event_id);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_op_log_scope_target ON op_log(scope, target_uid);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_op_log_origin_scope_seq ON op_log(origin_node_id, scope, origin_seq);')


def _allocate_next_origin_seq(cursor, origin_node_id: str) -> int:
    cursor.execute(
        '''INSERT INTO op_log_state (origin_node_id, next_seq)
           VALUES (
               ?,
               COALESCE((
                   SELECT MAX(origin_seq) + 2
                   FROM op_log
                   WHERE origin_node_id = ?
               ), 2)
           )
           ON CONFLICT(origin_node_id) DO UPDATE SET
               next_seq = MAX(
                   op_log_state.next_seq,
                   COALESCE((
                       SELECT MAX(origin_seq) + 1
                       FROM op_log
                       WHERE origin_node_id = excluded.origin_node_id
                   ), 1)
               ) + 1''',
        (str(origin_node_id), str(origin_node_id)),
    )
    row = cursor.execute(
        'SELECT next_seq FROM op_log_state WHERE origin_node_id = ?',
        (str(origin_node_id),),
    ).fetchone()
    return int(row[0]) - 1


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


# ── Query helpers used by op_sync.py ──────────────────────────────────────────

def get_local_op_log_head(cursor, local_node_id: str, scope: str) -> int:
    """Return the highest origin_seq this node has in op_log for the given scope.
    Returns 0 if no events exist yet.
    """
    row = cursor.execute(
        'SELECT MAX(origin_seq) FROM op_log WHERE origin_node_id = ? AND scope = ?',
        (str(local_node_id), str(scope)),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_peer_received_head(cursor, peer_node_id: str, scope: str) -> int:
    """Return the highest seq we have acknowledged receiving from peer for scope.
    Returns 0 if we have not seen any events from this peer for this scope.
    """
    row = cursor.execute(
        'SELECT max_received_seq FROM op_log_peer_head WHERE peer_node_id = ? AND scope = ?',
        (str(peer_node_id), str(scope)),
    ).fetchone()
    return int(row[0]) if row else 0


def update_peer_received_head(cursor, peer_node_id: str, scope: str, received_seq: int) -> None:
    """Advance the peer head watermark, but never go backwards."""
    cursor.execute(
        '''INSERT INTO op_log_peer_head (peer_node_id, scope, max_received_seq)
           VALUES (?, ?, ?)
           ON CONFLICT(peer_node_id, scope) DO UPDATE SET
               max_received_seq = MAX(max_received_seq, excluded.max_received_seq)''',
        (str(peer_node_id), str(scope), int(received_seq)),
    )


def get_op_log_events(
    cursor,
    origin_node_id: str,
    scope: str,
    from_seq: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return up to *limit* op_log rows for (origin_node_id, scope) starting at from_seq."""
    rows = cursor.execute(
        '''SELECT origin_seq, event_id, event_type, target_uid
           FROM op_log
           WHERE origin_node_id = ? AND scope = ? AND origin_seq >= ?
           ORDER BY origin_seq
           LIMIT ?''',
        (str(origin_node_id), str(scope), int(from_seq), int(limit)),
    ).fetchall()
    return [
        {
            'origin_seq': int(r[0]),
            'event_id': str(r[1]),
            'event_type': str(r[2]),
            'target_uid': str(r[3]),
        }
        for r in rows
    ]


# ── Startup backfill ───────────────────────────────────────────────────────────

# Mapping of (scope, materialized_table, uid_column, source_column).
_BACKFILL_SCOPES = [
    ('bulletins',        'bulletins',        'unique_id', 'source_node_id'),
    ('mail',             'mail',             'unique_id', 'source_node_id'),
    ('channel_comments', 'channel_comments', 'unique_id', 'source_node_id'),
]


def backfill_op_log(cursor, local_node_id: str) -> int:
    """Idempotent backfill: create op_log upsert events for locally-originated
    records that predate Phase 2 (or were missed for any reason).

    Called once at startup after the local node ID is established.  Safe to
    call multiple times — the unique constraint on (origin_node_id, scope,
    target_uid) prevents duplicate entries.

    Returns the number of new entries created.
    """
    if not is_op_log_enabled():
        return 0
    if not local_node_id:
        return 0

    total = 0
    for scope, table, uid_col, src_col in _BACKFILL_SCOPES:
        try:
            # Include records where source_node_id matches local OR is NULL.
            # NULL means the record predates Phase 1 source tracking; on a given
            # node those are almost certainly locally-originated records (records
            # synced from a peer in Phase-1+ have the peer's node_id set).
            rows = cursor.execute(
                f'''SELECT t.{uid_col}
                    FROM {table} t
                    WHERE (t.{src_col} = ? OR t.{src_col} IS NULL)
                      AND NOT EXISTS (
                          SELECT 1 FROM op_log o
                          WHERE o.scope = ? AND o.target_uid = t.{uid_col}
                            AND o.origin_node_id = ?
                      )''',
                (local_node_id, scope, local_node_id),
            ).fetchall()
            for (uid,) in rows:
                append_local_event(
                    cursor,
                    origin_node_id=local_node_id,
                    event_type='upsert',
                    scope=scope,
                    target_uid=str(uid),
                    payload={'backfill': True},
                )
                total += 1
        except Exception as exc:
            logging.warning('op_log backfill error for %s: %s', scope, exc)

    if total:
        logging.info('op_log backfill: created %d entries for %s', total, local_node_id)
    else:
        logging.debug('op_log backfill: nothing new for %s', local_node_id)
    return total
