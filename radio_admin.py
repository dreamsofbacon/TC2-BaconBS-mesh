"""Local radio administration. Only the running BBS dispatches operations.

A separate SQLite mailbox gives web/server processes atomic, durable claims.
Claimed operations are never replayed, even after a crash or uncertain timeout.
No changes to the shared BBS database or peer protocol are needed.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from app_paths import resolve_app_path


@contextmanager
def mailbox():
    path = resolve_app_path(os.getenv('BBS_RADIO_ADMIN_PATH'), 'radio_admin.db')
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL)''')
        conn.execute("CREATE INDEX IF NOT EXISTS operations_pending ON operations(status, created)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def operation(operation_id):
    with mailbox() as conn:
        row = conn.execute('SELECT id, status, detail, created FROM operations WHERE id=?',
                           (operation_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result['status'] in ('queued', 'dispatching') and time.time() - result['created'] > 90:
        result.update(status='unknown' if result['status'] == 'dispatching' else 'expired',
                      detail='No timely result. This operation will not be retried automatically.')
    return result


def submit(payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected a radio operation.')
    operation_id = str(payload.get('id', ''))
    try:
        uuid.UUID(operation_id)
    except (ValueError, AttributeError):
        raise ValueError('A unique operation ID is required.')
    if payload.get('action') not in ('send', 'identity', 'remove_contact', 'refresh'):
        raise ValueError('Unsupported operation.')
    if payload.get('radio') not in ('primary', 'secondary'):
        raise ValueError('Select a local radio.')
    encoded = json.dumps(payload, sort_keys=True)
    if len(encoded.encode('utf-8')) > 4096:
        raise ValueError('Operation is too large.')
    with mailbox() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT payload FROM operations WHERE id=?', (operation_id,)).fetchone()
        if old and old['payload'] != encoded:
            raise ValueError('This operation ID was already used for a different request.')
        if not old:
            if conn.execute("SELECT count(*) FROM operations WHERE status='queued' AND created>?",
                            (time.time() - 60,)).fetchone()[0] >= 20:
                raise ValueError('Radio queue is full. Wait for pending operations.')
            conn.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                         (operation_id, encoded, time.time(), 'queued', 'Waiting for the local BBS.'))
    return operation(operation_id)


def channel_label(network, name, index):
    name = str(name or '').strip()
    if not name:
        return 'Channel %s (name unknown)' % index
    if network == 'meshcore' and name.casefold() in ('public', '#public'):
        return '#Public'
    return name


def describe(link, configured_network=None):
    iface = link.interface
    network = str(getattr(iface, 'protocol_name', configured_network or 'unknown')).lower()
    connected = iface is not None and bool(getattr(link, 'enabled', True))
    connected = connected and not (link.reconnecting or link.reconnect_needed.is_set())
    check = getattr(iface, 'is_connected', None)
    if isinstance(check, bool):
        connected = connected and check
    event = getattr(iface, 'isConnected', None)
    if hasattr(event, 'is_set'):
        connected = connected and event.is_set()
    state = 'connected' if connected else ('reconnecting' if link.reconnecting else 'unavailable')
    if iface is not None and not getattr(iface, '_bbs_admin_generation', None):
        iface._bbs_admin_generation = uuid.uuid4().hex
    names = dict(getattr(iface, 'channel_names', {}) or {})
    channel_config = {}
    # Read cached protobuf config; do not ask the hardware from a web request.
    local = getattr(iface, 'localNode', None)
    for channel in getattr(local, 'channels', None) or []:
        if getattr(channel, 'role', 0):
            names[channel.index] = channel.settings.name or names.get(channel.index, "")
            channel_config[channel.index] = str(channel.settings)
    channels = []
    for index, name in sorted(names.items(), key=lambda pair: int(pair[0])):
        # Token binds the selection to this connection and its current channel
        # config, including key changes without exposing a channel secret.
        token = hashlib.sha256(('%s:%s:%s:%s' % (
            getattr(iface, '_bbs_admin_generation', ''), index, name, channel_config.get(index, getattr(iface, 'channel_fingerprints', {}).get(index, '')))).encode()).hexdigest()
        channels.append(dict(index=int(index), name=str(name),
                             label=channel_label(network, name, index), token=token))
    nodes = []
    for key, node in list((getattr(iface, 'nodes', {}) or {}).items()):
        user = node.get('user') or {}
        nodes.append(dict(id=str(key), name=str(user.get('longName') or user.get('shortName') or key)))
    info = {}
    try:
        info = iface.getMyNodeInfo().get('user', {}) if iface else {}
    except Exception:
        pass
    meshcore = network == 'meshcore'
    return dict(id=link.name, token=getattr(iface, '_bbs_admin_generation', ''), network=network, state=state, channels=channels,
                contacts=nodes, identity=info.get('longName', ''),
                max_bytes=int(getattr(iface, 'max_text_bytes', 228)),
                capabilities=dict(send=callable(getattr(iface, 'sendText', None)),
                    identity=callable(getattr(iface, 'admin_operation', None)) if meshcore else
                             callable(getattr(local, 'setOwner', None)),
                    remove_contact=meshcore and callable(getattr(iface, 'admin_operation', None)),
                    refresh=meshcore and callable(getattr(iface, 'admin_operation', None))))


def read_radios(config):
    path = resolve_app_path(os.getenv('BBS_RUNTIME_DIAG_PATH'), 'runtime_diagnostics.json')
    try:
        with open(path, encoding='utf-8') as source:
            snapshot = json.load(source)
        fresh = time.time() - os.path.getmtime(path) < 75
    except (OSError, ValueError):
        snapshot, fresh = {}, False
    runtime = {r['id']: r for r in snapshot.get('radio_admin', []) if isinstance(r, dict) and 'id' in r}
    radios = []
    for name, section in [('primary', 'interface'), ('secondary', 'interface2')]:
        kind = config.get(section, 'type', fallback='serial' if name == 'primary' else '').lower()
        if kind in ('', 'none') or (name == 'secondary' and not config.getboolean(section, 'enabled', fallback=True)):
            continue
        network = 'meshcore' if kind.startswith('meshcore') else 'meshtastic'
        radio = dict(runtime.get(name) or dict(id=name, network=network, channels=[], contacts=[],
                                               identity='', capabilities={}, max_bytes=160 if network == 'meshcore' else 228))
        if not fresh or name not in runtime:
            radio['state'] = 'configured'
        radios.append(radio)
    return radios


def execute(link, payload):
    """Validate against the current owner immediately before any side effect."""
    from utils import interface_send_lock, _pace_radio_send, get_user_message_pause_seconds, get_max_text_bytes
    iface = link.interface
    lock = interface_send_lock(iface)
    if not lock.acquire(timeout=10):
        raise ValueError('Radio is busy. Nothing was dispatched.')
    try:
        current = describe(link)
        if current['state'] != 'connected' or link.interface is not iface:
            raise ValueError('Selected radio is unavailable. Nothing was dispatched.')
        if payload.get('radio_token') != current['token']:
            raise ValueError('Radio selection expired. Refresh and select the radio again.')
        action = payload['action']
        if not current['capabilities'].get(action):
            raise ValueError('This radio does not support that operation.')
        if action == 'send':
            channel = next((c for c in current['channels'] if c['index'] == payload.get('channel')
                            and c['token'] == payload.get('channel_token')), None)
            if channel is None:
                raise ValueError('Channel selection expired or is unavailable. Select it again.')
            text = payload.get('text')
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Enter a message.')
            if len(text.encode('utf-8')) > get_max_text_bytes(iface):
                raise ValueError('Message exceeds this network’s UTF-8 byte limit.')
            _pace_radio_send(iface, get_user_message_pause_seconds(iface))
            if link.interface is not iface or describe(link)['state'] != 'connected':
                raise ValueError('Radio disconnected before dispatch.')
            iface.sendText(text=text, destinationId=0 if current['network'] == 'meshcore' else '^all',
                           channelIndex=channel['index'], wantAck=False, wantResponse=False)
            return 'submitted', 'Submitted to the radio. Public-channel delivery is not confirmed.'
        if action == 'identity':
            name = payload.get('name')
            if not isinstance(name, str) or not name.strip() or len(name.encode('utf-8')) > 24:
                raise ValueError('Use a non-empty name of at most 24 UTF-8 bytes.')
            if current['network'] == 'meshcore':
                iface.admin_operation(action, name=name.strip())
                return 'confirmed', 'Radio accepted the name change.'
            user = iface.getMyNodeInfo().get('user', {})
            iface.localNode.setOwner(long_name=name.strip(), is_licensed=bool(user.get('isLicensed', False)))
            return 'submitted', 'Name change submitted. Check the radio identity after it refreshes.'
        if action == 'remove_contact':
            if payload.get('contact') not in {n['id'] for n in current['contacts']}:
                raise ValueError('Contact is no longer on this radio.')
            iface.admin_operation(action, contact=payload['contact'])
        else:
            iface.admin_operation(action)
        return 'confirmed', 'Radio accepted the operation.'
    finally:
        lock.release()


class RadioAdminWorker:
    """One bounded worker per local slot; a stalled radio cannot block another."""
    def __init__(self):
        self.workers = {}
        self.next_tick = 0

    def tick(self, links):
        if time.monotonic() < self.next_tick:
            return
        self.next_tick = time.monotonic() + 1
        for link in links:
            if link.name not in ('primary', 'secondary'):
                continue
            thread = self.workers.get(link.name)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=self.process_one, args=(link,), daemon=True)
                self.workers[link.name] = thread
                thread.start()

    def process_one(self, link):
        with mailbox() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE operations SET status='expired', detail='Expired before dispatch.' WHERE status='queued' AND created<?",
                         (time.time() - 60,))
            rows = conn.execute("SELECT * FROM operations WHERE status='queued' ORDER BY created").fetchall()
            row = next((r for r in rows if json.loads(r['payload'])['radio'] == link.name), None)
            if row is None:
                return
            conn.execute("UPDATE operations SET status='dispatching', detail='Dispatch started; do not repeat.' WHERE id=?", (row['id'],))
        try:
            status, detail = execute(link, json.loads(row['payload']))
        except ValueError as exc:
            status, detail = 'failed', str(exc)
        except Exception as exc:
            status, detail = 'unknown', 'Radio result uncertain (%s). Not retried.' % type(exc).__name__
        with mailbox() as conn:
            conn.execute('UPDATE operations SET status=?, detail=? WHERE id=?', (status, detail, row['id']))
