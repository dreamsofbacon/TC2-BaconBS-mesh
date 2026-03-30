"""
Tests for the BULLETINCONT / MAILCONT long-content sync protocol.

The sender splits large payloads across independent packets (BULLETIN + continuation
BULLETINCONT packets) so each packet is individually valid and packet loss only causes
partial content truncation — never an all-or-nothing failure.
"""
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

from utils import _send_sync_with_cont, _MESHTASTIC_MAX_BYTES


class _DummyInterface:
    def __init__(self):
        self.sent_texts = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        self.sent_texts.append(text)


class LongContentSyncTests(unittest.TestCase):
    def _run_bulletin_sync(self, board, sender, subject, content, unique_id):
        interface = _DummyInterface()
        header = f"BULLETIN|{board}|{sender}|{subject}|"
        footer = f"|{unique_id}"
        _send_sync_with_cont(
            header, footer, content, unique_id,
            cont_prefix=f"BULLETINCONT|{unique_id}|",
            bbs_nodes=["!peer1"],
            interface=interface,
            pause_seconds=0,
        )
        return interface.sent_texts

    def test_short_bulletin_fits_single_packet(self):
        msgs = self._run_bulletin_sync("General", "CALL", "Hi", "short content", "uid-001")
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("BULLETIN|"), msgs[0])
        self.assertIn("short content", msgs[0])

    def test_long_bulletin_first_packet_is_always_valid(self):
        content = "A" * 600
        msgs = self._run_bulletin_sync("General", "CALL", "Long post", content, "uid-002")
        self.assertGreater(len(msgs), 1)
        # First packet must be a valid BULLETIN| message
        first = msgs[0]
        self.assertTrue(first.startswith("BULLETIN|"), first[:30])
        parts = first.split("|", 5)
        self.assertEqual(len(parts), 6)  # BULLETIN, board, sender, subject, content, uuid
        self.assertEqual(parts[5], "uid-002")

    def test_long_bulletin_first_packet_fits_meshtastic_limit(self):
        content = "B" * 600
        msgs = self._run_bulletin_sync("General", "CALL", "Long post", content, "uid-003")
        for msg in msgs:
            self.assertLessEqual(len(msg.encode('utf-8')), _MESHTASTIC_MAX_BYTES, msg[:40])

    def test_long_bulletin_cont_packets_carry_rest_of_content(self):
        content = "X" * 600
        unique_id = "uid-004"
        msgs = self._run_bulletin_sync("General", "CALL", "Subject", content, unique_id)
        # Reconstruct content from all packets
        first_parts = msgs[0].split("|", 5)
        reconstructed = first_parts[4]  # content field of first BULLETIN
        for cont_msg in msgs[1:]:
            self.assertTrue(cont_msg.startswith(f"BULLETINCONT|{unique_id}|"), cont_msg[:40])
            reconstructed += cont_msg.split("|", 2)[2]
        self.assertEqual(reconstructed, content)

    def test_single_lost_cont_only_truncates_not_destroys(self):
        """Even if all continuation packets are lost, the first packet is valid and stored."""
        content = "Y" * 600
        unique_id = "uid-005"
        msgs = self._run_bulletin_sync("General", "BCTN", "Subject", content, unique_id)
        # Simulate losing all BULLETINCONT packets — only first arrives
        surviving = [msgs[0]]
        first_parts = surviving[0].split("|", 5)
        self.assertEqual(len(first_parts), 6)
        # Content is non-empty (partial but valid)
        self.assertGreater(len(first_parts[4]), 0)


if __name__ == "__main__":
    unittest.main()
