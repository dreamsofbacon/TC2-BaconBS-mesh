import unittest

from utils import _send_sync_with_cont, _split_into_chunks


class _MeshCoreSizedInterface:
    max_text_bytes = 160

    def __init__(self):
        self.sent = []

    def sendText(self, text, destinationId, wantAck, wantResponse):
        del destinationId, wantAck, wantResponse
        self.sent.append(text)


class TransportPacketSizingTests(unittest.TestCase):
    def test_sync_frames_honor_active_transport_byte_limit(self):
        interface = _MeshCoreSizedInterface()
        _send_sync_with_cont(
            header="BULLETIN|General|CALL|Subject|",
            footer="|uid-1",
            content="payload " * 100,
            unique_id="uid-1",
            cont_prefix="BULLETINCONT|uid-1|",
            meta_prefix="BULLETINMETA|uid-1|",
            bbs_nodes=["peer"],
            interface=interface,
            pause_seconds=0,
        )
        self.assertGreater(len(interface.sent), 1)
        self.assertTrue(
            all(len(frame.encode("utf-8")) <= 160 for frame in interface.sent)
        )

    def test_user_reply_chunking_counts_utf8_bytes(self):
        chunks = _split_into_chunks("🙂" * 100, max_len=160)
        self.assertEqual("".join(chunks), "🙂" * 100)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 160 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
