import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import message_processing
from utils import send_sync_message


class _DummySendResult:
    def __init__(self, message_id: int):
        self.id = message_id


class _DummyInterface:
    def __init__(self):
        self.sent_texts = []
        self._next_id = 1

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append(text)
        result = _DummySendResult(self._next_id)
        self._next_id += 1
        return result


class SyncChunkingTests(unittest.TestCase):
    def setUp(self):
        message_processing._sync_chunk_buffers.clear()

    def test_long_sync_message_is_chunked_and_reassembled(self):
        interface = _DummyInterface()
        original = "MAIL|sender|short|recipient|subject|" + ("A" * 900) + "|uid-123"

        send_sync_message(original, destination=12345, interface=interface, raw_chunk_size=60, pause_seconds=0)

        self.assertGreater(len(interface.sent_texts), 1)
        self.assertTrue(all(msg.startswith("SYNCCHUNK|") for msg in interface.sent_texts))

        reconstructed = None
        for framed in interface.sent_texts:
            maybe_payload = message_processing._consume_sync_chunk("!peerNode", framed)
            if maybe_payload is not None:
                reconstructed = maybe_payload

        self.assertEqual(reconstructed, original)

    def test_short_sync_message_uses_single_legacy_frame(self):
        interface = _DummyInterface()
        original = "DELETE_MAIL|abc-123"

        send_sync_message(original, destination=12345, interface=interface, pause_seconds=0)

        self.assertEqual(interface.sent_texts, [original])


if __name__ == "__main__":
    unittest.main()
