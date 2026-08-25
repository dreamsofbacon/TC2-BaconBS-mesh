"""Synchronous MQTT transport for bridging BBS nodes over the internet.

MQTT sync exists for one purpose: letting a node reach another BBS node it
cannot hear over RF at all (separate LoRa mesh islands, e.g. different
cities) by relaying the same sync traffic over an internet-connected MQTT
broker instead. It participates in the existing five-phase sync protocol
completely unchanged -- this module only has to look like a radio interface.

Topic design: each configured link publishes/subscribes on exactly one
topic, ``{topic_prefix}/bbs``. Every subscriber on that topic sees every
message and filters locally by the packet's ``to`` field, exactly like a
real LoRa mesh (an inherently broadcast RF medium) -- so no new receive-side
filtering logic is needed anywhere else in the BBS. A ``topic_prefix``
scopes one bridge *relationship*; bridging three sites off one shared broker
means three separate ``[mqttN]`` links (three prefixes), not one link with
everyone crosstalking on a single topic.

Status telemetry (separate from the sync topic above, see publish_status):
server.py's main loop periodically publishes this node's whole link-status
tree (every radio AND every configured MQTT link, not just this one) to
``{topic_prefix}/{local_id}/status`` -- one retained, compact single-line
JSON message with everything -- plus one retained sub-topic per link at
``{topic_prefix}/{local_id}/status/links/<name>`` for a subscriber that only
wants one piece. Scoped under ``local_id`` (not just ``topic_prefix``)
because multiple nodes can share one broker+prefix; each only ever
publishes its own tree. ``retain=True`` so a client connecting later still
gets the last known state immediately, without waiting for the next cycle.

This project is an MQTT *client* only -- it connects to a broker the
operator already runs (e.g. self-hosted Mosquitto). No broker code lives
here.

TLS: ``tls = true`` alone uses the system CA store (the right default for a
broker with a publicly-trusted certificate). For a self-hosted broker with
a private CA, point ``tls_ca_certs`` at that CA. For brokers requiring
client-certificate (mutual TLS) auth, add ``tls_certfile``/``tls_keyfile``
(plus ``tls_keyfile_password`` if the key is encrypted). Configuring any of
those implies TLS even without ``tls = true``, so certs can never be
silently ignored on a plaintext connection.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
import os
import queue
import re
import threading
from types import SimpleNamespace
from typing import Any, Optional

import paho.mqtt.client as mqtt
from pubsub import pub


# MQTT has none of LoRa's payload-size or half-duplex constraints, so this
# is set high enough that the existing chunking machinery
# (utils._split_into_chunks) essentially never has to fragment MQTT
# traffic -- this alone satisfies the "turbo mode" chunking requirement,
# no new chunking code needed.
MQTT_MAX_TEXT_BYTES = 32768

_BROADCAST_LABELS = ("", "*", "0", "255")


def _clean_label(value: Any) -> str:
    return str(value or "").strip()


def sanitize_topic_segment(value: Any, allow_slash: bool = False) -> str:
    """Normalize a value used as an MQTT topic segment.

    Whitespace becomes '-' (runs collapse to one). Spaces are technically
    legal in MQTT topics but break shell/CLI tooling that doesn't quote,
    and trip up broker ACL patterns -- a node published under
    'baconbbs/Burlington NNE/status' is needlessly painful to work with.

    '+' and '#' are MQTT WILDCARDS and '/' is the level separator, so
    leaving them in a segment silently changes the topic's shape rather
    than naming it. Prefixes legitimately contain '/', hence allow_slash.
    """
    text = _clean_label(value)
    if not text:
        return ""
    text = re.sub(r"\s+", "-", text)
    text = text.replace("+", "-").replace("#", "-")
    if allow_slash:
        text = re.sub(r"/{2,}", "/", text).strip("/")
    else:
        text = text.replace("/", "-")
    return text


def _resolve_cert_path(value: Any, setting_name: str, link_name: str) -> Optional[str]:
    """Validate a configured TLS file path up front.

    Without this, a typo'd or unreadable cert path surfaces later as an
    opaque ssl/OSError from deep inside paho at connect time, which then
    just looks like "the broker is down" in the retry loop. Failing here
    instead names the exact setting and path at fault.
    """
    path = _clean_label(value)
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise ValueError(
            f"MQTT[{link_name}]: {setting_name} file not found: {expanded}"
        )
    if not os.access(expanded, os.R_OK):
        raise ValueError(
            f"MQTT[{link_name}]: {setting_name} file is not readable "
            f"(check permissions): {expanded}"
        )
    return expanded


def _mqtt_node_id(topic_prefix: str, label: str) -> str:
    """Stable string id for a node reached over this MQTT link.

    Deliberately prefixed so it can never collide in *shape* with a
    Meshtastic ``!xxxxxxxx`` id or a bare-hex MeshCore public key --
    ``utils.home_network()`` pattern-matches on this prefix to classify it
    into its own bucket rather than falling through to MeshCore's default.
    """
    return f"mqtt:{topic_prefix}:{label}"


def _fnv1a32(text: str) -> int:
    value = 2166136261
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def _node_num(node_id: str) -> int:
    """Full-width hash over the COMPLETE string id.

    meshcore_interface._node_num synthesizes a numeric id by truncating a
    public key to its first 8 hex characters -- a real, known collision
    surface between MeshCore and Meshtastic numeric ids in dual-radio
    bridge mode. This deliberately hashes the entire id string instead of
    truncating a prefix, avoiding that specific "throw away information"
    mistake. It doesn't eliminate 32-bit collision risk in the abstract,
    but these ids only need to be locally unique per node (session state,
    profile keys), not globally coordinated, matching how they're used
    elsewhere in the codebase today.
    """
    return _fnv1a32(node_id)


class MqttInterface:
    """Expose the subset of Meshtastic's interface used by Bacon BBS, over MQTT."""

    protocol_name = "MQTT"
    max_text_bytes = MQTT_MAX_TEXT_BYTES
    # Read by utils.py's per-interface pacing override: an MQTT link always
    # gets turbo-equivalent pacing, independent of the global [sync]
    # sync_turbo flag, so a mixed LoRa+MQTT bridge node can run its radio at
    # normal pacing and its MQTT link fast at the same time.
    is_low_latency = True
    send_timeout_seconds = 15.0

    def __init__(
        self,
        *,
        host: str,
        topic_prefix: str,
        local_id: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls: bool = False,
        tls_ca_certs: Optional[str] = None,
        tls_certfile: Optional[str] = None,
        tls_keyfile: Optional[str] = None,
        tls_keyfile_password: Optional[str] = None,
        tls_insecure: bool = False,
        publish_kinds: Optional[dict] = None,
        publish_prefix: Optional[str] = None,
        publish_clients_max_age_hours: int = 24,
        client_id: Optional[str] = None,
        link_name: str = "mqtt",
        keepalive: int = 60,
        receive_topic: str = "meshtastic.receive",
        connect_timeout: float = 30.0,
    ) -> None:
        if not host:
            raise ValueError("MQTT interface requires a broker host")
        if not topic_prefix or not topic_prefix.strip():
            raise ValueError("MQTT interface requires a topic_prefix")
        if not _clean_label(local_id):
            raise ValueError("MQTT interface requires a local_id")

        self.host = host
        self.port = int(port)
        # Normalized rather than rejected: a hand-edited config.ini with a
        # space in it should still work, just on a sane topic. Logged so
        # the effective value is never a silent surprise.
        self.topic_prefix = sanitize_topic_segment(topic_prefix, allow_slash=True)
        self.local_id = sanitize_topic_segment(local_id)
        if self.topic_prefix != _clean_label(topic_prefix).strip("/"):
            logging.info(
                "MQTT[%s]: topic_prefix normalized to %r for topic safety.",
                link_name, self.topic_prefix,
            )
        if self.local_id != _clean_label(local_id):
            logging.info(
                "MQTT[%s]: local_id normalized to %r for topic safety "
                "(this is also the node's id on this link, so peers must use the "
                "normalized form).",
                link_name, self.local_id,
            )
        if not self.topic_prefix:
            raise ValueError("MQTT interface requires a topic_prefix")
        if not self.local_id:
            raise ValueError("MQTT interface requires a local_id")
        self.link_name = link_name
        self.receive_topic = receive_topic
        # Unique per configured link (not a shared "MQTT" bucket) so
        # RadioLink.network_key can disambiguate N simultaneous MQTT links.
        self.protocol_name = f"MQTT:{link_name}"

        self.bbs_nodes: list[str] = []
        self.allowed_nodes: list[str] = []
        self.subscriber_nodes: list[str] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self._num_to_label: dict[int, str] = {}

        # Other BBS nodes noticed on this topic, keyed by their label. Filled
        # from the retained status messages every node already publishes (see
        # publish_status), so pairing does not require anyone to type a peer
        # address by hand -- every way of mistyping one fails silently.
        # Retained means the broker replays them the moment we subscribe, so
        # this is populated on connect rather than after a peer next speaks.
        self.peers_seen: dict[str, dict[str, Any]] = {}
        self._peers_lock = threading.Lock()

        self._self_node_id = _mqtt_node_id(self.topic_prefix, self.local_id)
        # Public alias: this node's identity ON THIS LINK. A node has one
        # identity per link, so get_local_node_id()'s single radio id
        # cannot answer "is this me?" for an MQTT peer.
        self.self_node_id = self._self_node_id
        self.myInfo = SimpleNamespace(my_node_num=_node_num(self._self_node_id))
        self._num_to_label[self.myInfo.my_node_num] = self.local_id

        # What this broker gets beyond sync traffic, and where published
        # data lives. publish_prefix intentionally does NOT affect the sync
        # topic below -- topic_prefix identifies the bridge relationship and
        # both ends must agree on it, whereas published data is one-way
        # telemetry that an operator may want slotted elsewhere.
        self.publish_kinds = dict(publish_kinds or {'status': True})
        self.publish_clients_max_age_hours = int(publish_clients_max_age_hours or 0)
        # Per-node client topics published last cycle. Retained messages
        # persist on the broker forever, so a node that ages out of the
        # window must be explicitly cleared -- otherwise "publish only
        # recent nodes" would shrink nothing that a subscriber actually
        # sees.
        self._published_client_topics: set = set()
        self.publish_prefix = (
            sanitize_topic_segment(publish_prefix, allow_slash=True) or self.topic_prefix
        )

        self._topic = f"{self.topic_prefix}/bbs"
        self._closed = False
        self._connected_event = threading.Event()
        self._connect_error: Optional[BaseException] = None
        self._incoming: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or f"baconbs-{link_name}-{self.local_id}",
        )
        if username:
            self._client.username_pw_set(username, password)

        ca_certs = _resolve_cert_path(tls_ca_certs, "tls_ca_certs", link_name)
        certfile = _resolve_cert_path(tls_certfile, "tls_certfile", link_name)
        keyfile = _resolve_cert_path(tls_keyfile, "tls_keyfile", link_name)
        key_password = _clean_label(tls_keyfile_password) or None

        if keyfile and not certfile:
            raise ValueError(
                f"MQTT[{link_name}]: tls_keyfile is set without tls_certfile -- "
                "client-certificate auth needs both (the cert and its private key)."
            )

        # Any TLS-specific option implies TLS even if `tls` itself wasn't set.
        # Silently connecting in PLAINTEXT to a broker the operator clearly
        # meant to secure (they configured certs!) is a security footgun, not
        # a convenience -- so turn it on and say so, rather than ignoring them.
        cert_options_present = any([ca_certs, certfile, keyfile])
        use_tls = bool(tls) or cert_options_present
        if use_tls and not tls:
            logging.info(
                "MQTT[%s]: enabling TLS because certificate options are configured "
                "(set 'tls = true' explicitly to silence this).",
                link_name,
            )

        if use_tls:
            # ca_certs=None keeps paho's default: verify against the system
            # CA store, exactly as before this option existed. A private/
            # self-signed broker CA goes here instead.
            self._client.tls_set(
                ca_certs=ca_certs,
                certfile=certfile,
                keyfile=keyfile,
                keyfile_password=key_password,
            )
            if tls_insecure:
                # Must be called AFTER tls_set (paho requirement).
                logging.warning(
                    "MQTT[%s]: tls_insecure is enabled -- the broker's certificate "
                    "hostname is NOT verified, so this connection can be "
                    "impersonated. Use only for testing against a self-signed "
                    "broker; prefer setting tls_ca_certs to that broker's CA.",
                    link_name,
                )
                self._client.tls_insecure_set(True)
        # paho owns transient reconnects internally; is_connected below is
        # the backstop server.py's existing per-link liveness/reconnect
        # check reads for the case connect() fails outright at startup --
        # deliberately not a second, competing reconnect loop.
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._dispatch_thread = threading.Thread(
            target=self._dispatch_incoming,
            name=f"mqtt-{link_name}-dispatch",
            daemon=True,
        )
        self._dispatch_thread.start()

        try:
            self._client.connect(self.host, self.port, keepalive=keepalive)
        except Exception:
            self.close()
            raise
        self._client.loop_start()

        if not self._connected_event.wait(timeout=connect_timeout):
            self.close()
            raise ConnectionError(
                f"MQTT broker {self.host}:{self.port} did not respond within "
                f"{connect_timeout}s"
            )
        if self._connect_error is not None:
            error = self._connect_error
            self.close()
            raise ConnectionError(f"MQTT connect failed: {error}") from error

    def _status_discovery_topics(self) -> list[str]:
        """Wildcard topics that carry other nodes' retained status messages.

        '+' is single-level, so this matches '{prefix}/{label}/status' and
        deliberately NOT the '{prefix}/{label}/status/links/<name>'
        sub-topics -- one message per node, not one per link.

        publish_prefix is watched as well as topic_prefix because a node
        that overrides it publishes its status there instead. A peer whose
        publish_prefix differs from ours is still undiscoverable; that is
        documented in Settings rather than guessed at.
        """
        topics = [f"{self.topic_prefix}/+/status"]
        if self.publish_prefix and self.publish_prefix != self.topic_prefix:
            topics.append(f"{self.publish_prefix}/+/status")
        return topics

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            client.subscribe(self._topic, qos=1)
            for topic in self._status_discovery_topics():
                client.subscribe(topic, qos=0)
            self._connect_error = None
        else:
            self._connect_error = ConnectionError(str(reason_code))
        self._connected_event.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        del client, userdata, flags, reason_code, properties

    def _record_peer_status(self, topic: str, data: Any) -> None:
        """Note another node publishing status on this topic.

        The label is the segment before 'status'. Our own retained status is
        echoed straight back to us on subscribe, so skipping self is the
        case that matters here -- without it every node would list itself as
        an available peer.
        """
        parts = str(topic).split("/")
        if len(parts) < 2 or parts[-1] != "status":
            return
        label = _clean_label(parts[-2])
        if not label or label == self.local_id:
            return
        updated_at = None
        if isinstance(data, dict):
            updated_at = data.get("updated_at")
        with self._peers_lock:
            self.peers_seen[label] = {
                "label": label,
                # Built with the same helper the sync layer uses, so a
                # discovered address can never drift from an accepted one.
                "node_id": _mqtt_node_id(self.topic_prefix, label),
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "updated_at": updated_at,
            }

    def discovered_peers(self) -> list[dict[str, Any]]:
        """Peers noticed on this topic, most recently seen first."""
        with self._peers_lock:
            peers = [dict(entry) for entry in self.peers_seen.values()]
        peers.sort(key=lambda entry: str(entry.get("last_seen") or ""), reverse=True)
        return peers

    def _on_message(self, client, userdata, msg) -> None:
        del client, userdata
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logging.debug(
                "MQTT[%s]: ignoring malformed payload on %s", self.link_name, msg.topic
            )
            return
        # Route by topic before parsing as a sync frame. Status payloads have
        # no 'from' field and would fall out of the parser below anyway, but
        # depending on that would make the sync path quietly sensitive to
        # anything else we ever subscribe to.
        if msg.topic != self._topic:
            self._record_peer_status(msg.topic, data)
            return
        from_label = _clean_label(data.get("from"))
        if not from_label or from_label == self.local_id:
            # No sender label, or our own publish echoed back to us.
            return
        to_label = _clean_label(data.get("to"))
        text = str(data.get("text", ""))

        sender_id = _mqtt_node_id(self.topic_prefix, from_label)
        sender_num = _node_num(sender_id)
        self._ensure_node(sender_id, sender_num, from_label)

        if to_label in _BROADCAST_LABELS:
            to_num = 0
        elif to_label == self.local_id:
            to_num = self.myInfo.my_node_num
        else:
            # Addressed to some other node sharing this topic -- still
            # dispatched (matching the "everyone sees everything, filter
            # locally" broadcast-medium design); on_receive already
            # discriminates by the `to` field for every other transport.
            to_num = _node_num(_mqtt_node_id(self.topic_prefix, to_label))

        packet = {
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": text.encode("utf-8"),
            },
            "from": sender_num,
            "fromId": sender_id,
            "to": to_num,
        }
        self._incoming.put(packet)

    def _ensure_node(self, node_id: str, node_num: int, label: str) -> None:
        # MQTT has no discovery/heartbeat -- a node only appears in .nodes
        # after its first message is seen, mirroring
        # meshcore_interface.py's own unknown-sender handling.
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "num": node_num,
                "user": {
                    "id": node_id,
                    "shortName": label[:4],
                    "longName": label,
                    "hwModel": "MQTT",
                    "role": "bridge",
                },
            }
        self._num_to_label[node_num] = label

    def _dispatch_incoming(self) -> None:
        while True:
            packet = self._incoming.get()
            try:
                if packet is None:
                    return
                pub.sendMessage(self.receive_topic, packet=packet, interface=self)
            except Exception:
                logging.exception("MQTT[%s] receive dispatch failed", self.link_name)
            finally:
                self._incoming.task_done()

    def start_receive(self) -> None:
        """No-op: loop_start() already ran in __init__.

        Matches the callable-or-absent contract server.py already tolerates
        for other transports (``getattr(link.interface, 'start_receive', None)``).
        """
        return

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected() and not self._closed)

    def _label_for_destination(self, destination: Any) -> str:
        if isinstance(destination, int):
            label = self._num_to_label.get(destination)
            if label is None:
                label = self._resolve_configured_label(destination)
            if label is None:
                raise ValueError(f"Unknown MQTT numeric destination: {destination}")
            return label
        text = _clean_label(destination)
        prefix = f"mqtt:{self.topic_prefix}:"
        if text.startswith(prefix):
            return text[len(prefix):]
        return text

    def _resolve_configured_label(self, destination_num: int) -> Optional[str]:
        """Resolve a numeric destination against configured peer lists.

        Unlike MeshCore (which pre-fetches its full contact list from the
        companion radio at connect time), MQTT has no contact directory --
        the lazy roster only knows a peer once a message from it has been
        seen. A configured BBS peer we've never heard from yet still needs
        to be reachable, so fall back to the same bbs_nodes/allowed_nodes/
        subscriber_nodes lists the sync engine already populates.
        """
        prefix = f"mqtt:{self.topic_prefix}:"
        for candidate in self.bbs_nodes + self.allowed_nodes + self.subscriber_nodes:
            candidate_id = _clean_label(candidate)
            if not candidate_id:
                continue
            if not candidate_id.startswith(prefix):
                candidate_id = _mqtt_node_id(self.topic_prefix, candidate_id)
            if _node_num(candidate_id) == destination_num:
                label = candidate_id[len(prefix):]
                self._ensure_node(candidate_id, destination_num, label)
                return label
        return None

    def sendText(
        self,
        text: str,
        destinationId: Any,
        wantAck: bool = True,
        wantResponse: bool = False,
    ) -> SimpleNamespace:
        del wantAck, wantResponse
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_text_bytes:
            raise ValueError(
                f"MQTT text exceeds {self.max_text_bytes} bytes: {len(encoded)}"
            )
        is_broadcast = destinationId in (0, 255, "0", "255", None)
        to_label = "*" if is_broadcast else self._label_for_destination(destinationId)
        payload = json.dumps({"from": self.local_id, "to": to_label, "text": text})
        info = self._client.publish(self._topic, payload, qos=1, retain=False)
        try:
            info.wait_for_publish(timeout=self.send_timeout_seconds)
        except Exception as exc:
            raise IOError(f"MQTT publish failed: {exc}") from exc
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise IOError(f"MQTT publish failed: rc={info.rc}")
        return SimpleNamespace(id=str(info.mid))

    def apply_publish_settings(
        self, publish_kinds: dict, publish_prefix: Optional[str] = None,
        max_age_hours: Optional[int] = None,
    ) -> None:
        """Update what this broker receives, WITHOUT reconnecting.

        Publish selection is pure output routing -- it has no effect on the
        MQTT session itself -- so dropping a healthy connection to change
        it would be gratuitous. Called by server.reload_links_from_config
        when config.ini changes, which is what makes the Settings toggles
        take effect on a running node instead of silently waiting for the
        next restart.
        """
        self.publish_kinds = dict(publish_kinds or {})
        self.publish_prefix = (
            sanitize_topic_segment(publish_prefix, allow_slash=True) or self.topic_prefix
        )
        if max_age_hours is not None:
            self.publish_clients_max_age_hours = int(max_age_hours or 0)

    def publishes(self, kind: str) -> bool:
        """True if this broker is configured to receive ``kind``.

        server.py checks this across all links before doing the work to
        build a payload, so a category nobody subscribes to costs nothing
        (some require database queries).
        """
        return bool(self.publish_kinds.get(kind, False))

    def _publish_json(self, topic: str, payload: Any, retain: bool = True) -> bool:
        """One published telemetry message. Never raises.

        Published data is a side effect of the periodic diagnostics cycle;
        a broker refusing it (ACL) or a dropped connection must never
        disturb that cycle or the sync engine sharing this connection.
        """
        if self._closed:
            return False
        try:
            self._client.publish(
                topic, json.dumps(payload, separators=(",", ":")), qos=0, retain=retain,
            )
            return True
        except Exception:
            logging.debug(
                "MQTT[%s] publish failed for %s", self.link_name, topic, exc_info=True
            )
            return False

    @property
    def _publish_base(self) -> str:
        return f"{self.publish_prefix}/{self.local_id}"

    def publish_status(self, status: dict[str, Any]) -> None:
        """Node/link health -- one master message plus a sub-topic per link.
        ``status`` looks like ``{"updated_at": ..., "links": {name: {...}}}``,
        built by server.py from the same per-link data the Settings >
        Diagnostics page and the nav-bar badges use, so all three agree.
        """
        if not self.publishes("status"):
            return
        base = f"{self._publish_base}/status"
        if not self._publish_json(base, status):
            return
        links = status.get("links")
        if not isinstance(links, dict):
            return
        for name, link_status in links.items():
            self._publish_json(f"{base}/links/{name}", link_status)

    def publish_clients(self, clients: list) -> None:
        """Mesh devices seen in range: a summary plus one topic per node.

        Filtered to this broker's ``publish_clients_max_age_hours`` window
        (0 = no limit). The roster accumulates every node ever heard, which
        on a busy mesh is hundreds of entries -- far more than a bridge
        needs and expensive on a metered link.

        Per-node topics are keyed by link and node id so a subscriber can
        watch one device without parsing the whole roster. Topics for nodes
        that have since aged out are cleared with an empty retained payload
        (the MQTT convention for deleting a retained message), otherwise
        they would linger on the broker indefinitely and the filtering
        would reduce nothing a subscriber actually sees.
        """
        if not self.publishes("clients"):
            return
        clients = self._filter_recent(clients)
        base = f"{self._publish_base}/clients"
        self._publish_json(base, {"count": len(clients), "clients": clients})

        current: set = set()
        for client in clients:
            node_id = str(client.get("node_id", "")).strip()
            link_name = str(client.get("link_name", "")).strip()
            if not node_id:
                continue
            # '/' and '+' are MQTT topic separators/wildcards -- a node id
            # containing one would silently fan out into extra topic levels.
            safe_id = node_id.replace("/", "_").replace("+", "_").replace("#", "_")
            topic = f"{base}/{link_name}/{safe_id}"
            current.add(topic)
            self._publish_json(topic, client)

        for stale in self._published_client_topics - current:
            try:
                self._client.publish(stale, "", qos=0, retain=True)
            except Exception:
                logging.debug(
                    "MQTT[%s] could not clear stale client topic %s",
                    self.link_name, stale, exc_info=True,
                )
        self._published_client_topics = current

    def _filter_recent(self, clients: list) -> list:
        """Keep only clients seen within this broker's window.

        server.py queries the database with the widest window any broker
        needs, so each link narrows that shared result to its own -- the
        alternative, one query per broker, would repeat the same work.
        """
        max_age = self.publish_clients_max_age_hours
        if not max_age or max_age <= 0:
            return list(clients)
        cutoff = datetime.now() - timedelta(hours=max_age)
        epoch_cutoff = cutoff.timestamp()
        kept = []
        for client in clients:
            # Prefer when the radio actually HEARD the node; last_seen only
            # says it was still listed during our sweep, which is true for
            # nearly every node. See db_operations.get_mesh_clients.
            heard = client.get("last_heard_epoch")
            if isinstance(heard, (int, float)):
                if heard >= epoch_cutoff:
                    kept.append(client)
                continue
            raw = str(client.get("last_seen") or "").strip()
            if not raw:
                # No timestamp to judge by -- keep it rather than silently
                # dropping a node, same as the unparseable case below.
                kept.append(client)
                continue
            try:
                seen = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                # Unparseable timestamp: keep it rather than silently
                # dropping a node over a formatting quirk.
                kept.append(client)
                continue
            if seen >= cutoff:
                kept.append(client)
        return kept

    def publish_telemetry(self, telemetry: dict[str, Any]) -> None:
        """Aggregate node stats (hardware models, roles, battery) -- the
        data behind Node Statistics and Wall of Shame, as topics suitable
        for graphing or a Home Assistant sensor."""
        if not self.publishes("telemetry"):
            return
        base = f"{self._publish_base}/telemetry"
        self._publish_json(base, telemetry)
        for key in ("hardware_models", "roles"):
            section = telemetry.get(key)
            if isinstance(section, dict):
                for name, count in section.items():
                    safe = str(name).replace("/", "_").replace("+", "_").replace("#", "_")
                    self._publish_json(f"{base}/{key}/{safe}", count)

    def publish_sync_stats(self, stats: dict[str, Any]) -> None:
        """Sync progress, peer convergence, record counts, database size --
        for monitoring whether nodes are actually catching up."""
        if not self.publishes("sync_stats"):
            return
        self._publish_json(f"{self._publish_base}/sync", stats)

    def publish_activity(self, event: dict[str, Any]) -> None:
        """One BBS activity event (new bulletin / mail / channel comment).

        Deliberately NOT retained: an event describes something that
        happened at a moment, so replaying the last one to every new
        subscriber would be misleading.
        """
        if not self.publishes("activity"):
            return
        kind = str(event.get("kind", "event"))
        self._publish_json(f"{self._publish_base}/activity/{kind}", event, retain=False)

    def getMyNodeInfo(self) -> dict[str, Any]:
        return {
            "num": self.myInfo.my_node_num,
            "user": {
                "id": self._self_node_id,
                "shortName": self.local_id[:4],
                "longName": self.local_id,
                "hwModel": "MQTT",
                "role": "bridge",
            },
        }

    def node_id_from_num(self, node_num: int) -> Optional[str]:
        label = self._num_to_label.get(node_num)
        if label is None:
            return None
        return _mqtt_node_id(self.topic_prefix, label)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.loop_stop()
        except Exception:
            logging.debug("MQTT[%s] loop_stop failed", self.link_name, exc_info=True)
        try:
            self._client.disconnect()
        except Exception:
            logging.debug("MQTT[%s] disconnect failed", self.link_name, exc_info=True)
        self._incoming.put(None)
        if threading.current_thread() is not self._dispatch_thread:
            self._dispatch_thread.join(timeout=3)
