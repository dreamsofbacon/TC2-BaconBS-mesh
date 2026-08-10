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

This project is an MQTT *client* only -- it connects to a broker the
operator already runs (e.g. self-hosted Mosquitto). No broker code lives
here.
"""

from __future__ import annotations

import json
import logging
import queue
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
    """Expose the subset of Meshtastic's interface used by BaconBS, over MQTT."""

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
        self.topic_prefix = topic_prefix.strip().strip("/")
        self.local_id = _clean_label(local_id)
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

        self._self_node_id = _mqtt_node_id(self.topic_prefix, self.local_id)
        self.myInfo = SimpleNamespace(my_node_num=_node_num(self._self_node_id))
        self._num_to_label[self.myInfo.my_node_num] = self.local_id

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
        if tls:
            self._client.tls_set()
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

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            client.subscribe(self._topic, qos=1)
            self._connect_error = None
        else:
            self._connect_error = ConnectionError(str(reason_code))
        self._connected_event.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        del client, userdata, flags, reason_code, properties

    def _on_message(self, client, userdata, msg) -> None:
        del client, userdata
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logging.debug(
                "MQTT[%s]: ignoring malformed payload on %s", self.link_name, msg.topic
            )
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
