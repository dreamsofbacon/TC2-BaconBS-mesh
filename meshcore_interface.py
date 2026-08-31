"""Synchronous compatibility interface for MeshCore companion radios.

The BBS was originally written against Meshtastic's synchronous ``sendText``
API and pypubsub receive events.  The official MeshCore Python library is
asyncio-native.  This module bridges the two models so the rest of the BBS can
remain transport-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

from meshcore import EventType, MeshCore
from pubsub import pub


# MeshCore's BaseChatMesh limits direct-message text to 10 AES blocks.
MESHCORE_MAX_TEXT_BYTES = 160


def _clean_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text[1:] if text.startswith("!") else text


def _node_num(public_key: str) -> int:
    """Return a stable Meshtastic-shaped numeric ID for BBS session state."""
    key = _clean_key(public_key)
    try:
        return int(key[:8], 16)
    except (TypeError, ValueError):
        # FNV-1a fallback for an unexpected non-hex identifier.
        value = 2166136261
        for byte in key.encode("utf-8"):
            value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
        return value


# MeshCore reports a contact's advertisement type as a raw byte
# (meshcore/reader.py reads it with dbuf.read(1)[0]), so it arrives as an
# int and was being stored verbatim -- a bare "1" sitting in the roster's
# Role column next to Meshtastic's readable names. Unknown codes are shown
# as "type <n>" rather than guessed at, so a firmware addition reads as
# unrecognized instead of being silently mislabelled as something else.
_ADV_TYPE_NAMES = {
    1: "Companion",
    2: "Repeater",
    3: "Room Server",
    4: "Sensor",
}


def _adv_type_name(value, default: str = "Companion") -> str:
    """Human-readable name for a MeshCore advertisement type."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return _ADV_TYPE_NAMES.get(value, f"type {value}")
    text = str(value).strip()
    if text.isdigit():
        return _ADV_TYPE_NAMES.get(int(text), f"type {text}")
    return text


class MeshCoreInterface:
    """Expose the subset of Meshtastic's interface used by Bacon BBS."""

    protocol_name = "MeshCore"
    max_text_bytes = MESHCORE_MAX_TEXT_BYTES
    send_timeout_seconds = 30.0

    def __init__(
        self,
        transport: str,
        *,
        port: Optional[str] = None,
        baudrate: int = 115200,
        hostname: Optional[str] = None,
        tcp_port: int = 5000,
        ble_address: Optional[str] = None,
        ble_pin: Optional[str] = None,
        channel_index: int = 0,
        receive_topic: str = "meshtastic.receive",
        connect_timeout: float = 30.0,
    ) -> None:
        self.transport = transport
        self.port = port
        self.hostname = hostname
        self.tcp_port = int(tcp_port)
        self.ble_address = ble_address
        self.channel_index = int(channel_index)
        self.receive_topic = receive_topic
        self.bbs_nodes: list[str] = []
        self.allowed_nodes: list[str] = []
        self.subscriber_nodes: list[str] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self.myInfo = SimpleNamespace(my_node_num=0)

        self._meshcore: Optional[MeshCore] = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._receive_started = False
        self._closed = False
        self._num_to_key: dict[int, str] = {}
        self._incoming: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()

        self._loop = asyncio.new_event_loop()
        self._rxThread = threading.Thread(
            target=self._run_loop,
            name=f"meshcore-{transport}-loop",
            daemon=True,
        )
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_incoming,
            name="meshcore-bbs-dispatch",
            daemon=True,
        )
        self._rxThread.start()
        self._dispatch_thread.start()

        future = asyncio.run_coroutine_threadsafe(
            self._connect(
                port=port,
                baudrate=baudrate,
                hostname=hostname,
                tcp_port=tcp_port,
                ble_address=ble_address,
                ble_pin=ble_pin,
            ),
            self._loop,
        )
        try:
            future.result(timeout=connect_timeout)
        except Exception:
            self.close()
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(
        self,
        *,
        port: Optional[str],
        baudrate: int,
        hostname: Optional[str],
        tcp_port: int,
        ble_address: Optional[str],
        ble_pin: Optional[str],
    ) -> None:
        if self.transport == "serial":
            if not port:
                raise ValueError("A serial port is required for MeshCore serial")
            meshcore = await MeshCore.create_serial(
                port,
                baudrate,
                default_timeout=5,
                auto_reconnect=True,
                max_reconnect_attempts=8,
            )
        elif self.transport == "tcp":
            if not hostname:
                raise ValueError("A hostname is required for MeshCore TCP")
            meshcore = await MeshCore.create_tcp(
                hostname,
                int(tcp_port),
                default_timeout=5,
                auto_reconnect=True,
                max_reconnect_attempts=8,
            )
        elif self.transport == "ble":
            meshcore = await MeshCore.create_ble(
                ble_address,
                pin=ble_pin,
                default_timeout=5,
                auto_reconnect=True,
                max_reconnect_attempts=8,
            )
        else:
            raise ValueError(f"Unsupported MeshCore transport: {self.transport}")

        if meshcore is None:
            raise ConnectionError("MeshCore companion radio did not respond")

        self._meshcore = meshcore
        self._send_lock = asyncio.Lock()
        meshcore.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_message)
        meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_message)
        meshcore.subscribe(EventType.NEW_CONTACT, self._on_contact_update)
        await meshcore.ensure_contacts()
        self._refresh_nodes()

        local_key = _clean_key(meshcore.self_info.get("public_key"))
        self.myInfo.my_node_num = _node_num(local_key)
        if local_key:
            self._num_to_key[self.myInfo.my_node_num] = local_key

    async def _on_contact_update(self, event) -> None:
        if self._meshcore is not None:
            contact = event.payload or {}
            public_key = _clean_key(contact.get("public_key"))
            if public_key:
                # meshcore_py keeps NEW_CONTACT entries in a pending map until
                # the next full refresh. Make the freshly advertised contact
                # immediately addressable by the BBS.
                self._meshcore.contacts[public_key] = contact
            self._refresh_nodes()

    def _refresh_nodes(self) -> None:
        if self._meshcore is None:
            return
        nodes: dict[str, dict[str, Any]] = {}
        num_to_key: dict[int, str] = {}
        for key, contact in self._meshcore.contacts.items():
            public_key = _clean_key(contact.get("public_key") or key)
            if not public_key:
                continue
            number = _node_num(public_key)
            name = str(contact.get("adv_name") or public_key[:12])
            nodes[public_key] = {
                "num": number,
                "user": {
                    "id": public_key,
                    "shortName": name[:4],
                    "longName": name,
                    "hwModel": "MeshCore",
                    "role": _adv_type_name(contact.get("type")),
                },
                "position": {
                    "latitude": contact.get("adv_lat"),
                    "longitude": contact.get("adv_lon"),
                },
            }
            num_to_key[number] = public_key

        local = self._meshcore.self_info
        local_key = _clean_key(local.get("public_key"))
        if local_key:
            number = _node_num(local_key)
            name = str(local.get("name") or local_key[:12])
            nodes[local_key] = {
                "num": number,
                "user": {
                    "id": local_key,
                    "shortName": name[:4],
                    "longName": name,
                    "hwModel": "MeshCore",
                    "role": _adv_type_name(local.get("adv_type")),
                },
            }
            num_to_key[number] = local_key

        self.nodes = nodes
        self._num_to_key = num_to_key

    def _resolve_received_key(self, prefix: str) -> str:
        clean_prefix = _clean_key(prefix)
        if self._meshcore is not None:
            contact = self._meshcore.get_contact_by_key_prefix(clean_prefix)
            if contact:
                full = _clean_key(contact.get("public_key"))
                if full:
                    return full
        return clean_prefix

    def _configured_alias(self, public_key: str) -> str:
        clean_key = _clean_key(public_key)
        configured = self.bbs_nodes + self.allowed_nodes + self.subscriber_nodes
        for candidate in configured:
            clean_candidate = _clean_key(candidate)
            if clean_candidate and (
                clean_key.startswith(clean_candidate)
                or clean_candidate.startswith(clean_key)
            ):
                return candidate.strip()
        return clean_key

    async def _on_contact_message(self, event) -> None:
        payload = event.payload or {}
        if int(payload.get("txt_type", 0)) != 0:
            return
        public_key = self._resolve_received_key(payload.get("pubkey_prefix", ""))
        sender_id = self._configured_alias(public_key)
        sender_num = _node_num(public_key)
        if public_key and public_key not in self.nodes:
            name = public_key[:12]
            self.nodes[public_key] = {
                "num": sender_num,
                "user": {
                    "id": public_key,
                    "shortName": name[:4],
                    "longName": name,
                    "hwModel": "MeshCore",
                    "role": "contact",
                },
            }
            self._num_to_key[sender_num] = public_key
        if sender_id != public_key and public_key in self.nodes:
            self.nodes[sender_id] = self.nodes[public_key]

        text = str(payload.get("text", ""))
        packet = {
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": text.encode("utf-8"),
            },
            "from": sender_num,
            "fromId": sender_id,
            "to": self.myInfo.my_node_num,
        }
        self._incoming.put(packet)

    async def _on_channel_message(self, event) -> None:
        payload = event.payload or {}
        if int(payload.get("txt_type", 0)) != 0:
            return
        channel_index = int(payload.get("channel_idx", 0))
        text = str(payload.get("text", ""))
        packet = {
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": text.encode("utf-8"),
            },
            "to": 0,
            "channel_index": channel_index,
            "channel_name": "Public" if channel_index == 0 else f"Channel {channel_index}",
            "sender_timestamp": payload.get("sender_timestamp"),
            "message_hash": payload.get("txt_hash"),
            "public_chatter_only": True,
        }
        self._incoming.put(packet)

    def _dispatch_incoming(self) -> None:
        while True:
            packet = self._incoming.get()
            try:
                if packet is None:
                    return
                pub.sendMessage(self.receive_topic, packet=packet, interface=self)
            except Exception:
                logging.exception("MeshCore receive dispatch failed")
            finally:
                self._incoming.task_done()

    def start_receive(self) -> None:
        """Enable queued-message fetching after the BBS subscriber is ready."""
        if self._receive_started or self._meshcore is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self._meshcore.start_auto_message_fetching(), self._loop
        )
        future.result(timeout=20)
        self._receive_started = True

    @property
    def is_connected(self) -> bool:
        return bool(self._meshcore and self._meshcore.is_connected and not self._closed)

    def _resolve_destination(self, destination: Any) -> str:
        if isinstance(destination, int):
            key = self._num_to_key.get(destination)
            if not key:
                raise ValueError(f"Unknown MeshCore numeric destination: {destination}")
            return key

        clean = _clean_key(destination)
        if self._meshcore is not None:
            contact = self._meshcore.get_contact_by_key_prefix(clean)
            if contact:
                return _clean_key(contact.get("public_key"))
        if len(clean) >= 12:
            return clean
        raise ValueError(
            "MeshCore destinations must be a contact public key or unique 12+ hex prefix"
        )

    async def _send(self, destination: str, text: str, want_ack: bool):
        if self._meshcore is None or self._send_lock is None:
            raise ConnectionError("MeshCore interface is not connected")
        async with self._send_lock:
            if want_ack:
                return await self._meshcore.commands.send_msg_with_retry(
                    destination,
                    text,
                    max_attempts=1,
                    max_flood_attempts=1,
                    timeout=12,
                )
            return await self._meshcore.commands.send_msg(destination, text)

    async def _send_channel(self, text: str):
        if self._meshcore is None or self._send_lock is None:
            raise ConnectionError("MeshCore interface is not connected")
        async with self._send_lock:
            return await self._meshcore.commands.send_chan_msg(
                self.channel_index, text
            )

    def sendText(
        self,
        text: str,
        destinationId: Any,
        wantAck: bool = True,
        wantResponse: bool = False,
    ) -> SimpleNamespace:
        del wantResponse
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_text_bytes:
            raise ValueError(
                f"MeshCore text exceeds {self.max_text_bytes} bytes: {len(encoded)}"
            )
        is_broadcast = destinationId in (0, 255, "0", "255")
        if is_broadcast:
            future = asyncio.run_coroutine_threadsafe(
                self._send_channel(text), self._loop
            )
        else:
            destination = self._resolve_destination(destinationId)
            future = asyncio.run_coroutine_threadsafe(
                self._send(destination, text, wantAck), self._loop
            )
        result = future.result(timeout=self.send_timeout_seconds)
        if result is None or result.type == EventType.ERROR:
            detail = getattr(result, "payload", None)
            raise IOError(f"MeshCore send failed: {detail}")
        expected_ack = result.payload.get("expected_ack", b"")
        send_id = (
            expected_ack.hex()
            if isinstance(expected_ack, (bytes, bytearray))
            else str(expected_ack or int(time.time()))
        )
        return SimpleNamespace(id=send_id)

    def getMyNodeInfo(self) -> dict[str, Any]:
        if self._meshcore is None:
            return {}
        info = self._meshcore.self_info
        public_key = _clean_key(info.get("public_key"))
        name = str(info.get("name") or public_key[:12])
        return {
            "num": self.myInfo.my_node_num,
            "user": {
                # MeshCore's on-air addressing uses a 6-byte public-key prefix.
                # Keeping the BBS identity at the same 12 hex characters also
                # prevents source metadata from consuming most of a 160-byte frame.
                "id": public_key[:12],
                "publicKey": public_key,
                "shortName": name[:4],
                "longName": name,
                "hwModel": "MeshCore",
                "role": _adv_type_name(info.get("adv_type")),
            },
        }

    def node_id_from_num(self, node_num: int) -> Optional[str]:
        public_key = self._num_to_key.get(node_num)
        return self._configured_alias(public_key) if public_key else None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        meshcore = self._meshcore
        if meshcore is not None and self._loop.is_running():
            async def _shutdown() -> None:
                if self._receive_started:
                    await meshcore.stop_auto_message_fetching()
                await meshcore.disconnect()

            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=10)
            except Exception:
                logging.debug("MeshCore shutdown did not complete cleanly", exc_info=True)
        self._incoming.put(None)
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if threading.current_thread() is not self._rxThread:
            self._rxThread.join(timeout=3)
        if threading.current_thread() is not self._dispatch_thread:
            self._dispatch_thread.join(timeout=3)
