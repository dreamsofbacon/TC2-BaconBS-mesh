"""Phase 2: HAVE / WANT / EVENT op-log discovery protocol.

Frame formats (all pipe-delimited; HAVE trims itself to the active
transport's byte budget -- 220 bytes on LoRa, far higher on a low-latency
transport like MQTT -- via utils.get_max_text_bytes(interface)):

  HAVE|{local_node_id}|{scope}:{max_seq}[|{scope}:{max_seq}...]
      Broadcast periodically by each node.  Advertises the highest op_log
      sequence number this node has *created* for each scope.  Receivers
      compare against their peer-head watermark and request gaps via WANT.

  WANT|{scope}|{origin_node_id}|{from_seq}
      Unicast to the node that sent a HAVE.  Asks for EVENT frames covering
      [from_seq, ...) for events the origin created for that scope.

  EVENT|{scope}|{origin_node_id}|{seq}|{event_type}|{target_uid}
      Unicast response to WANT.  Describes one op_log entry.
      On receipt the node updates its peer-head watermark and, for upsert
      events whose target_uid is not yet in the local materialized table,
      issues a HASHMISS to fetch the full record via the existing protocol.

Design invariant: all three handlers are non-fatal.  Any exception is caught
and logged so a bad frame never crashes the receiver loop.
"""

import logging
from typing import Optional

import db_operations
import op_log

# Scopes tracked by HAVE/WANT/EVENT.  Must match the op_log dual-write scopes
# added in Phase 1 and the materialized table names used in handle_event.
_SUPPORTED_SCOPES = ('bulletins', 'mail', 'channel_comments', 'public_chatter')

# Map op-log scope name → materialized SQLite table name.
_SCOPE_TO_TABLE = {
    'bulletins': 'bulletins',
    'mail': 'mail',
    'channel_comments': 'channel_comments',
    'public_chatter': 'public_chatter',
}

# ── HAVE ──────────────────────────────────────────────────────────────────────

def build_have_frame(local_node_id: str, peer_id: Optional[str] = None, interface=None) -> Optional[str]:
    """Build a HAVE announcement from the local op_log.

    Returns None when the op_log is empty (nothing to advertise yet).
    When ``peer_id`` is provided and that peer supports the 'scc' wire cap,
    scope names are encoded as single chars to save bytes.

    The oversized-frame trim uses the active transport's byte budget
    (``utils.get_max_text_bytes(interface)``) rather than a fixed LoRa-sized
    cap, so a low-latency transport like MQTT (32KB budget) doesn't discard
    scope data it has plenty of room to carry.
    """
    if not local_node_id:
        return None
    try:
        from utils import peers_all_support, encode_scope, get_max_text_bytes
        use_codes = peers_all_support([peer_id], 'scc') if peer_id else False
        conn = db_operations.get_db_connection()
        c = conn.cursor()
        scope_parts = []
        for scope in _SUPPORTED_SCOPES:
            max_seq = op_log.get_local_op_log_head(c, local_node_id, scope)
            if max_seq > 0:
                scope_parts.append(f'{encode_scope(scope, use_codes)}:{max_seq}')
        if not scope_parts:
            return None
        frame = 'HAVE|' + local_node_id + '|' + '|'.join(scope_parts)
        if len(frame.encode('utf-8')) > get_max_text_bytes(interface):
            # Trim to first two scopes if somehow oversized
            frame = 'HAVE|' + local_node_id + '|' + '|'.join(scope_parts[:2])
        return frame
    except Exception as exc:
        logging.warning('op_sync.build_have_frame failed: %s', exc)
        return None


def handle_have(parts: list[str], sender_node_id: str, local_node_id: str, interface) -> None:
    """Process a HAVE frame received from *sender_node_id*.

    For each scope where the peer has more events than we've acknowledged, send
    a WANT requesting the missing range.
    """
    # parts: ['HAVE', origin_node_id, 'scope:max_seq', ...]
    if len(parts) < 3:
        return
    origin_node_id = parts[1]
    if not origin_node_id:
        return
    try:
        conn = db_operations.get_db_connection()
        c = conn.cursor()
        from utils import _send_one_sync, get_hash_repair_pause_seconds, decode_scope, encode_scope, peers_all_support
        use_codes = peers_all_support([sender_node_id], 'scc')
        for field in parts[2:]:
            if ':' not in field:
                continue
            colon = field.rfind(':')
            scope = decode_scope(field[:colon])
            seq_str = field[colon + 1:]
            if scope not in _SUPPORTED_SCOPES:
                continue
            try:
                their_max_seq = int(seq_str)
            except ValueError:
                continue
            our_head = op_log.get_peer_received_head(c, origin_node_id, scope)
            if their_max_seq > our_head:
                want_from = our_head + 1
                want_frame = f'WANT|{encode_scope(scope, use_codes)}|{origin_node_id}|{want_from}'
                logging.debug(
                    'op_sync HAVE: peer=%s scope=%s their_max=%d our_head=%d → WANT from %d',
                    origin_node_id, scope, their_max_seq, our_head, want_from,
                )
                _send_one_sync(
                    want_frame, sender_node_id, interface,
                    pause_seconds=get_hash_repair_pause_seconds(interface),
                )
    except Exception as exc:
        logging.warning('op_sync.handle_have failed (from %s): %s', sender_node_id, exc)


# ── WANT ──────────────────────────────────────────────────────────────────────

def handle_want(parts: list[str], sender_node_id: str, local_node_id: str, interface) -> None:
    """Process a WANT frame received from *sender_node_id*.

    Look up op_log events and send EVENT frames for the requested range.
    """
    # parts: ['WANT', scope, origin_node_id, from_seq]
    if len(parts) != 4:
        return
    from utils import decode_scope
    scope = decode_scope(parts[1])
    origin_node_id = parts[2]
    from_seq_str = parts[3]
    if scope not in _SUPPORTED_SCOPES:
        return
    # We only have events *we* originated; ignore WANTs for other origins.
    if origin_node_id != local_node_id:
        logging.debug(
            'op_sync WANT: requested origin %s is not us (%s); ignoring',
            origin_node_id, local_node_id,
        )
        return
    try:
        from_seq = int(from_seq_str)
    except ValueError:
        logging.warning('op_sync WANT: bad from_seq %r; ignoring', from_seq_str)
        return
    try:
        from utils import get_reconcile_max_per_pass
        conn = db_operations.get_db_connection()
        c = conn.cursor()
        # Bound the number of EVENT frames sent per WANT with the same
        # turbo-aware cap used elsewhere for records-per-reconcile-pass (20
        # normal / 100 for a low-latency transport like MQTT) rather than a
        # flat constant that never scales up off LoRa.
        max_events = get_reconcile_max_per_pass(interface)
        events = op_log.get_op_log_events(c, local_node_id, scope, from_seq, limit=max_events)
        if not events:
            logging.debug('op_sync WANT: no events for scope=%s from_seq=%d', scope, from_seq)
            return
        from utils import _send_one_sync, get_hash_repair_pause_seconds, encode_scope, peers_all_support
        use_codes = peers_all_support([sender_node_id], 'scc')
        pause = get_hash_repair_pause_seconds(interface)
        logging.info(
            'op_sync WANT from %s: scope=%s from_seq=%d \u2192 sending %d EVENT frame(s)',
            sender_node_id, scope, from_seq, len(events),
        )
        for ev in events:
            frame = (
                f"EVENT|{encode_scope(scope, use_codes)}|{local_node_id}|{ev['origin_seq']}"
                f"|{ev['event_type']}|{ev['target_uid']}"
            )
            _send_one_sync(frame, sender_node_id, interface, pause_seconds=pause)
    except Exception as exc:
        logging.warning('op_sync.handle_want failed (from %s): %s', sender_node_id, exc)


# ── EVENT ─────────────────────────────────────────────────────────────────────

def handle_event(parts: list[str], sender_node_id: str, interface) -> None:
    """Process an EVENT frame received from *sender_node_id*.

    Advances the peer-head watermark for the origin.  For upsert events whose
    target_uid is not yet in the local materialized table, issues a HASHMISS to
    fetch the full record via the existing protocol.
    """
    # parts: ['EVENT', scope, origin_node_id, seq, event_type, target_uid]
    if len(parts) != 6:
        logging.warning('op_sync EVENT: malformed frame (expected 6 parts): %s', '|'.join(parts))
        return
    from utils import decode_scope
    scope = decode_scope(parts[1])
    origin_node_id = parts[2]
    seq_str = parts[3]
    event_type = parts[4]
    target_uid = parts[5]

    if scope not in _SUPPORTED_SCOPES:
        return
    if not target_uid:
        return
    try:
        seq = int(seq_str)
    except ValueError:
        logging.warning('op_sync EVENT: bad seq %r; ignoring', seq_str)
        return

    try:
        conn = db_operations.get_db_connection()
        c = conn.cursor()

        # Advance peer-head watermark (monotone: never go backwards)
        op_log.update_peer_received_head(c, origin_node_id, scope, seq)
        conn.commit()

        if event_type == 'upsert':
            table = _SCOPE_TO_TABLE[scope]
            row = conn.execute(
                f'SELECT 1 FROM {table} WHERE unique_id = ?',  # noqa: S608 – table is whitelisted above
                (str(target_uid),),
            ).fetchone()
            if not row:
                from utils import _send_one_sync, get_hash_repair_pause_seconds
                logging.info(
                    'op_sync EVENT: upsert %s/%s (seq %d from %s) not in local table → HASHMISS',
                    scope, target_uid, seq, origin_node_id,
                )
                _send_one_sync(
                    f'HASHMISS|{scope}|{target_uid}',
                    sender_node_id,
                    interface,
                    pause_seconds=get_hash_repair_pause_seconds(interface),
                )
            else:
                logging.debug(
                    'op_sync EVENT: upsert %s/%s already present; watermark advanced to %d',
                    scope, target_uid, seq,
                )
        elif event_type == 'delete':
            # Deletes are already propagated via DELETE_BULLETIN/DELETE_MAIL/DELETE_CHANNELCOMMENT
            # frames in the existing protocol.  Phase 2 records the watermark only.
            logging.debug(
                'op_sync EVENT: delete %s/%s from %s; watermark advanced to %d',
                scope, target_uid, origin_node_id, seq,
            )
    except Exception as exc:
        logging.warning('op_sync.handle_event failed (from %s): %s', sender_node_id, exc)
