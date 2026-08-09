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

    def test_two_differently_capped_interfaces_in_one_process_never_cross_contaminate(self):
        """Dual-radio bridge mode runs a 220-byte Meshtastic link and a
        160-byte MeshCore link in the SAME process. Chunking must be read
        fresh from each interface on every call, not memoized/cached after
        the first call -- interleave sends on both to catch a latent
        module-level-caching bug that per-transport unit tests (which only
        ever exercise one interface per process) can't see."""
        meshtastic_iface = _MeshCoreSizedInterface()
        meshtastic_iface.max_text_bytes = 220
        meshcore_iface = _MeshCoreSizedInterface()
        meshcore_iface.max_text_bytes = 160

        # Interleave: meshcore first, then meshtastic, then meshcore again --
        # a naive "cache the limit on first use" bug would leak the first
        # call's limit into later calls on the OTHER interface.
        for interface in (meshcore_iface, meshtastic_iface, meshcore_iface):
            interface.sent.clear()
            _send_sync_with_cont(
                header="BULLETIN|General|CALL|Subject|",
                footer="|uid-interleave",
                content="payload " * 100,
                unique_id="uid-interleave",
                cont_prefix="BULLETINCONT|uid-interleave|",
                meta_prefix="BULLETINMETA|uid-interleave|",
                bbs_nodes=["peer"],
                interface=interface,
                pause_seconds=0,
            )
            cap = interface.max_text_bytes
            self.assertTrue(
                all(len(frame.encode("utf-8")) <= cap for frame in interface.sent),
                f"frame exceeded this interface's own {cap}-byte cap",
            )


if __name__ == "__main__":
    unittest.main()
