"""Drive the real BBS command path from something that is not a radio.

Every reply the BBS produces leaves through exactly one call --
``interface.sendText(...)`` inside ``utils.send_message`` -- because handlers
never return their reply text, they send it as a side effect. That single
choke point is what makes this module possible: an object with a ``sendText``
method that appends to a buffer captures everything the BBS would have said,
and not one line of command-handling code has to know it is being emulated.

So this is not a simulation of the BBS. ``EmulatorSession.send`` calls the
same ``message_processing.process_message`` a LoRa packet reaches, with the
same menu-state dict, the same chunking, and the same database. What comes
back is what a radio would have received, in the packets it would have
arrived in.

Deliberately free of Flask, and of any import of the web admin: the web page
is one transport for this, and an SSH front end would be another. See
docs/SSH-ACCESS.md -- ``EmulatorSession`` is the seam that design builds on,
which is why the session lifecycle lives here rather than in a route handler.
"""

import logging
import secrets
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import Optional

# Synthetic senders need node numbers that cannot collide with a real node's.
# Meshtastic derives its num from the low four bytes of the device MAC, so
# nothing prevents a collision in principle -- but these numbers only ever key
# utils.user_states inside the web admin's own process, and the web admin is a
# separate service from the BBS server, so a collision could at worst confuse
# two emulator sessions with each other. Starting high keeps even that from
# happening in practice.
_SYNTHETIC_NUM_BASE = 0xE0000000

# 'emu:' joins '!' (meshtastic) and 'mqtt:' as a recognised id shape. Without
# the matching branch in utils.home_network these would classify as meshcore,
# which is the silent default for any unrecognised shape.
SYNTHETIC_PREFIX = "emu:"

SESSION_IDLE_SECONDS = 30 * 60
MAX_SESSIONS = 24

# One reply can be many chunks and Ask Nomad answers arrive minutes late, so
# the buffer holds more than a single exchange -- but it is bounded, because
# a session whose browser tab was closed goes on receiving background replies
# with nothing draining them.
MAX_BUFFERED_CHUNKS = 400

DEFAULT_MAX_TEXT_BYTES = 220


class EmulatorInterface:
    """What the command handlers see instead of a radio.

    Every attribute here is one the real command path actually touches; none
    is speculative. The two that change behaviour rather than just satisfying
    an attribute lookup are ``is_low_latency`` (skips the two-second
    inter-message gap ``_pace_radio_send`` enforces to keep DMs from
    colliding on a multi-hop mesh -- correct on air, unbearable in a browser)
    and ``max_text_bytes``, which is left adjustable so an operator can watch
    a menu split at a limit other than the Meshtastic default.
    """

    protocol_name = "emulator"
    is_low_latency = True

    def __init__(self, nodes, allowed_nodes=None,
                 max_text_bytes=DEFAULT_MAX_TEXT_BYTES):
        self.nodes = dict(nodes or {})
        # Empty on purpose. A handler that posts a bulletin also fans the new
        # row out to sync peers, and those frames would go straight into this
        # buffer and confuse the transcript. Nothing is lost by dropping them:
        # the row is in the shared database, and server.py's ordinary
        # reconcile cycle carries it to the other nodes the same as any row
        # written by a radio.
        self.bbs_nodes = []
        self.allowed_nodes = list(allowed_nodes or [])
        self.max_text_bytes = int(max_text_bytes)
        # Set by utils.request_session_end when the user picks [0] Exit. A
        # radio has no session to close, so only a connected front end reads
        # this; its presence is what tells request_session_end that hanging
        # up is even possible here.
        self.session_ended = False
        self._buffer = deque(maxlen=MAX_BUFFERED_CHUNKS)
        self._lock = threading.Lock()
        self._seq = 0

    # -- the choke point -------------------------------------------------

    def sendText(self, text=None, destinationId=None, wantAck=False,
                 wantResponse=False, **kwargs):
        """Capture one outbound chunk. Signature matches meshtastic's."""
        body = text if text is not None else ""
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._buffer.append({
                "seq": seq,
                "text": body,
                "to": destinationId,
                # The number that decides whether this needed to be two
                # packets, which is most of the point of showing chunks.
                "bytes": len(body.encode("utf-8")),
                "at": time.time(),
            })
        # send_message logs d.id, so this must have one.
        return SimpleNamespace(id=seq)

    def node_id_from_num(self, node_num):
        for node_id, node in self.nodes.items():
            if node.get("num") == node_num:
                return node_id
        return None

    def drain(self):
        """Take everything captured since the last drain."""
        with self._lock:
            out = list(self._buffer)
            self._buffer.clear()
        return out


class EmulatorSession:
    """One person typing at the BBS.

    Lives longer than a single HTTP request on purpose. Ask Nomad hands the
    question to a worker thread and answers through the same interface up to
    a minute later; a per-request interface would drop that answer on the
    floor and the page would show a slow ack followed by silence.
    """

    def __init__(self, token, sender_id, sender_node_id, interface,
                 label, acting_as_real):
        self.token = token
        self.sender_id = sender_id
        self.sender_node_id = sender_node_id
        self.interface = interface
        self.label = label
        # True when driving a real node's identity: writes attribute to that
        # node, mail is genuinely from them, and Zork autosaves over their
        # save. The web layer requires an explicit confirmation for this.
        self.acting_as_real = acting_as_real
        self.created_at = time.time()
        self.last_used = self.created_at
        # process_message mutates shared module state keyed by sender_id, so
        # two overlapping sends from one session would interleave menu steps.
        self._lock = threading.Lock()

    def send(self, text):
        """Run one user message through the real command path.

        Returns (chunks, error). Chunks produced before a failure are kept:
        a handler that sent a prompt and then raised has still told the user
        something, and hiding that makes the traceback harder to place.
        """
        from message_processing import process_message

        self.last_used = time.time()
        error = None
        with self._lock:
            try:
                process_message(
                    self.sender_id, text, self.interface,
                    is_sync_message=False,
                    sender_node_id=self.sender_node_id,
                )
            except Exception as exc:
                logging.exception(
                    "Emulator session %s raised handling %r", self.token[:8],
                    text)
                error = f"{type(exc).__name__}: {exc}"
        return self.drain(), error

    def drain(self):
        self.last_used = time.time()
        return self.interface.drain()

    def menu_state(self):
        """The state dict that decides how the next message is read.

        Worth surfacing: inside a game session 'N' means north, and outside
        one it means a menu item. Without this the transcript looks wrong.
        """
        from utils import get_user_state
        return get_user_state(self.sender_id)

    def close(self):
        """Drop menu state and stop anything holding a process or a thread."""
        try:
            from utils import user_states
            user_states.pop(self.sender_id, None)
        except Exception:
            logging.exception("Emulator could not clear menu state")

        try:
            import zork_port
            # Iterating the public game registry rather than the private
            # session dict; stop_zork_session no-ops for a game that was
            # never started, and this is what kills the dfrotz subprocess.
            for game_id in zork_port.GAMES:
                zork_port.stop_zork_session(self.sender_id, game_id)
        except Exception:
            logging.exception("Emulator could not stop a Zork session")

        try:
            import trivia_port
            # Popped rather than ended through command(user_id, 'x'), which
            # would record a score for a session that existed to test the
            # menus.
            trivia_port._sessions.pop(self.sender_id, None)
        except Exception:
            logging.exception("Emulator could not stop a Trivia session")


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_sessions: dict = {}
_synthetic_counter = 0


def _roster_nodes():
    """Seed interface.nodes from the stored roster.

    The web admin has no radio to ask, so the mesh_clients table is the only
    record of who exists. Handlers use this to resolve short names; a sender
    missing from it still works, it just has no name to show.
    """
    nodes = {}
    try:
        from db_operations import get_mesh_clients
        for client in get_mesh_clients():
            node_id = str(client.get("node_id") or "").strip()
            if not node_id or node_id in nodes:
                continue
            try:
                num = int(client.get("node_num") or 0)
            except (TypeError, ValueError):
                num = 0
            nodes[node_id] = {
                "num": num,
                "user": {
                    "id": node_id,
                    "shortName": client.get("short_name") or "",
                    "longName": client.get("long_name") or "",
                },
            }
    except Exception:
        logging.exception("Emulator could not read the mesh client roster")
    return nodes


def roster_choices():
    """Nodes an operator can act as, most recently seen first."""
    choices = []
    seen = set()
    try:
        from db_operations import get_mesh_clients
        for client in get_mesh_clients():
            node_id = str(client.get("node_id") or "").strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            choices.append({
                "node_id": node_id,
                "short_name": client.get("short_name") or "",
                "long_name": client.get("long_name") or "",
                "last_seen": client.get("last_seen") or "",
            })
    except Exception:
        logging.exception("Emulator could not list roster choices")
    return choices


def _allowed_nodes():
    """The urgent-board posting allow list, so that ACL behaves truthfully."""
    try:
        import configparser
        import os
        from app_paths import resolve_app_path
        config = configparser.ConfigParser()
        config.read(resolve_app_path(os.getenv("BBS_CONFIG_PATH"), "config.ini"))
        raw = config.get("allow_list", "allowed_nodes", fallback="")
        return [item.strip() for item in raw.split(",") if item.strip()]
    except Exception:
        logging.exception("Emulator could not read the allow list")
        return []


def sweep_idle(now=None):
    """Close sessions nobody is using. Each holds menu state and maybe a
    dfrotz process, so leaving them is a real leak rather than untidiness."""
    now = now if now is not None else time.time()
    with _registry_lock:
        stale = [token for token, session in _sessions.items()
                 if now - session.last_used > SESSION_IDLE_SECONDS]
        expired = [_sessions.pop(token) for token in stale]
    for session in expired:
        session.close()
    return len(expired)


def start_session(node_id=None, short_name=None,
                  max_text_bytes=DEFAULT_MAX_TEXT_BYTES):
    """Open a session, either as a synthetic tester or as a real node.

    Passing ``node_id`` means acting as that node for real: its writes are
    attributed to it in the shared database. The caller is responsible for
    having confirmed that with whoever asked.
    """
    global _synthetic_counter

    sweep_idle()

    nodes = _roster_nodes()
    acting_as_real = bool(str(node_id or "").strip())

    if acting_as_real:
        sender_node_id = str(node_id).strip()
        existing = nodes.get(sender_node_id)
        if existing and existing.get("num"):
            sender_id = int(existing["num"])
        else:
            # A node in no roster, or one whose num was never recorded. It
            # can still be driven; it just gets a synthetic number for menu
            # state, while writes keep its real id.
            with _registry_lock:
                _synthetic_counter += 1
                sender_id = _SYNTHETIC_NUM_BASE + _synthetic_counter
            nodes.setdefault(sender_node_id, {
                "num": sender_id,
                "user": {"id": sender_node_id, "shortName": "", "longName": ""},
            })
            nodes[sender_node_id]["num"] = sender_id
        label = ((existing or {}).get("user", {}).get("shortName")
                 or sender_node_id)
    else:
        with _registry_lock:
            _synthetic_counter += 1
            counter = _synthetic_counter
        sender_id = _SYNTHETIC_NUM_BASE + counter
        sender_node_id = f"{SYNTHETIC_PREFIX}{counter}"
        label = (str(short_name or "").strip() or f"emu{counter}")[:12]
        nodes[sender_node_id] = {
            "num": sender_id,
            "user": {
                "id": sender_node_id,
                "shortName": label,
                "longName": f"Emulator {label}",
            },
        }

    interface = EmulatorInterface(
        nodes, allowed_nodes=_allowed_nodes(), max_text_bytes=max_text_bytes)
    token = secrets.token_urlsafe(24)
    session = EmulatorSession(
        token, sender_id, sender_node_id, interface, label, acting_as_real)

    with _registry_lock:
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions.values(), key=lambda s: s.last_used)
            _sessions.pop(oldest.token, None)
            evicted = oldest
        else:
            evicted = None
        _sessions[token] = session
    if evicted is not None:
        evicted.close()

    return session


def start_ssh_session(account_id, alias, max_text_bytes=8192):
    """Open a session whose identity is derived only from an account id."""
    from db_operations import get_account_sender_num

    clean_account_id = str(account_id or '').strip()
    if len(clean_account_id) != 32 or any(
            char not in '0123456789abcdef'
            for char in clean_account_id.casefold()):
        raise ValueError("Invalid SSH account id")

    sweep_idle()
    # Stable for the life of the account. This used to be a per-connection
    # counter, and since user_profiles, game_scores and zork_saves are keyed
    # by this number rather than the node id, a player's save was written
    # under a number nothing would ever present again -- it synced to every
    # peer and could never be resumed by the person who made it.
    sender_id = get_account_sender_num(clean_account_id)
    if sender_id is None:
        raise ValueError(f"No sender number for account {clean_account_id}")
    sender_node_id = f"ssh:{clean_account_id}"
    label = str(alias or '').strip()[:20] or "ssh-user"
    nodes = _roster_nodes()
    nodes[sender_node_id] = {
        "num": sender_id,
        "user": {
            "id": sender_node_id,
            "shortName": label,
            "longName": label,
        },
    }
    interface = EmulatorInterface(
        nodes, allowed_nodes=[], max_text_bytes=max_text_bytes)
    token = secrets.token_urlsafe(24)
    session = EmulatorSession(
        token, sender_id, sender_node_id, interface, label, False)
    with _registry_lock:
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions.values(), key=lambda item: item.last_used)
            _sessions.pop(oldest.token, None)
            evicted = oldest
        else:
            evicted = None
        _sessions[token] = session
    if evicted is not None:
        evicted.close()
    return session


def get_session(token) -> Optional[EmulatorSession]:
    with _registry_lock:
        return _sessions.get(str(token or ""))


def end_session(token) -> bool:
    with _registry_lock:
        session = _sessions.pop(str(token or ""), None)
    if session is None:
        return False
    session.close()
    return True


def reset_session(token) -> bool:
    """Put a session back at the front door without changing identity.

    Clears menu state and game sessions but keeps the same sender, so an
    operator can restart a flow without losing who they are acting as.
    """
    session = get_session(token)
    if session is None:
        return False
    session.close()
    session.last_used = time.time()
    return True


def active_session_count() -> int:
    with _registry_lock:
        return len(_sessions)
