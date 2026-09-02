"""The client emulator's session core.

The point of the emulator is that it is NOT a reimplementation: it drives
message_processing.process_message, the same function a LoRa packet reaches.
So the tests that matter are the ones that would fail if it quietly stopped
doing that -- asserting on real menu text the handlers produce, not on
anything this module composes itself.

The other half is chunking. A reply is one logical message and several
packets, and the split is the thing an operator cannot see any other way.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import bbs_emulator
import db_operations
import utils


class _Scratch(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        for token in list(bbs_emulator._sessions):
            bbs_emulator.end_session(token)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def session(self, **kwargs):
        session = bbs_emulator.start_session(**kwargs)
        self.addCleanup(bbs_emulator.end_session, session.token)
        return session

    @staticmethod
    def text_of(chunks):
        return "".join(chunk["text"] for chunk in chunks)


class TheRealCommandPathTests(_Scratch):
    """If these pass against a stub they prove nothing, so they assert on
    text only the genuine handlers produce."""

    def test_the_main_menu_comes_back(self):
        session = self.session()
        chunks, error = session.send("?")
        self.assertIsNone(error)
        body = self.text_of(chunks)
        self.assertIn("Bacon BBS", body)
        self.assertIn("[1] Quick Commands", body)

    def test_a_menu_choice_advances_real_menu_state(self):
        """process_message stores state in utils.user_states; if the emulator
        were faking replies this would stay empty."""
        session = self.session()
        session.send("?")
        session.send("B")
        self.assertIsNotNone(session.menu_state())

    def test_state_is_keyed_to_this_session_only(self):
        session = self.session()
        session.send("?")
        self.assertIn(session.sender_id, utils.user_states)

    def test_two_sessions_do_not_share_menu_state(self):
        """Two operators, or two tabs, must not move each other's menus."""
        first, second = self.session(), self.session()
        self.assertNotEqual(first.sender_id, second.sender_id)
        first.send("?")
        first.send("B")
        second.send("?")
        self.assertNotEqual(first.menu_state(), second.menu_state())

    def test_an_exception_is_reported_rather_than_raised(self):
        """A handler blowing up should show in the transcript, not 500 the
        page and lose the session."""
        import message_processing

        session = self.session()
        original = message_processing.process_message

        def boom(*args, **kwargs):
            raise RuntimeError("handler exploded")

        message_processing.process_message = boom
        try:
            chunks, error = session.send("?")
        finally:
            message_processing.process_message = original
        self.assertIn("handler exploded", error)
        self.assertEqual(chunks, [])


class ChunkingTests(_Scratch):
    """What a radio would actually have transmitted."""

    def test_a_long_reply_is_split_at_the_configured_limit(self):
        session = self.session(max_text_bytes=64)
        chunks, _ = session.send("?")
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(chunk["bytes"], 64)

    def test_the_default_limit_is_the_meshtastic_one(self):
        session = self.session()
        self.assertEqual(session.interface.max_text_bytes, 220)
        chunks, _ = session.send("?")
        for chunk in chunks:
            self.assertLessEqual(chunk["bytes"], 220)

    def test_byte_length_is_utf8_not_characters(self):
        """A menu full of emoji splits sooner than its character count
        suggests, which is exactly the surprise this page exists to show."""
        interface = bbs_emulator.EmulatorInterface({}, max_text_bytes=220)
        interface.sendText(text="\U0001F4BE" * 3, destinationId=1)
        self.assertEqual(interface.drain()[0]["bytes"], 12)

    def test_draining_twice_does_not_repeat_chunks(self):
        session = self.session()
        session.send("?")
        session.drain()
        self.assertEqual(session.drain(), [])


class LateReplyTests(_Scratch):
    """Ask Nomad answers from a worker thread up to a minute after the
    question returns. The session outlives the request precisely so that
    answer has somewhere to land."""

    def test_a_reply_written_after_send_returned_is_still_collected(self):
        session = self.session()
        session.send("?")
        session.drain()
        # Stand in for the gateway worker thread reaching the same interface.
        utils.send_message("the slow answer", session.sender_id,
                           session.interface)
        chunks = session.drain()
        self.assertIn("the slow answer", self.text_of(chunks))

    def test_the_buffer_is_bounded(self):
        """A closed browser tab must not grow the buffer without limit."""
        interface = bbs_emulator.EmulatorInterface({})
        for n in range(bbs_emulator.MAX_BUFFERED_CHUNKS + 50):
            interface.sendText(text=str(n), destinationId=1)
        self.assertEqual(len(interface.drain()),
                         bbs_emulator.MAX_BUFFERED_CHUNKS)


class IdentityTests(_Scratch):
    def test_a_synthetic_sender_gets_its_own_network_bucket(self):
        """Without the emu: branch these classify as meshcore, which is the
        silent default for any unrecognised shape."""
        session = self.session()
        self.assertTrue(session.sender_node_id.startswith("emu:"))
        self.assertEqual(utils.home_network(session.sender_node_id), "emulator")

    def test_a_synthetic_sender_is_not_marked_as_a_real_node(self):
        self.assertFalse(self.session().acting_as_real)

    def test_synthetic_senders_are_distinct(self):
        ids = {self.session().sender_id for _ in range(3)}
        self.assertEqual(len(ids), 3)

    def test_acting_as_a_node_keeps_its_real_id_for_writes(self):
        """The whole risk and the whole point: writes attribute to them."""
        session = self.session(node_id="!1bbecf78")
        self.assertEqual(session.sender_node_id, "!1bbecf78")
        self.assertTrue(session.acting_as_real)

    def test_acting_as_a_roster_node_reuses_its_real_node_number(self):
        db_operations.thread_local.connection.execute(
            "INSERT INTO mesh_clients (link_name, node_id, node_num, "
            "short_name, long_name, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("primary", "!1bbecf78", 464464760, "zrk", "Zorak",
             "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
        session = self.session(node_id="!1bbecf78")
        self.assertEqual(session.sender_id, 464464760)
        self.assertEqual(session.label, "zrk")

    def test_the_roster_seeds_the_interface_node_table(self):
        """Handlers resolve short names through interface.nodes, and the web
        admin has no radio to ask."""
        db_operations.thread_local.connection.execute(
            "INSERT INTO mesh_clients (link_name, node_id, node_num, "
            "short_name, long_name, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("primary", "!abcd1234", 111, "abc", "A Node",
             "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
        session = self.session()
        self.assertEqual(
            session.interface.nodes["!abcd1234"]["user"]["shortName"], "abc")


class InterfaceContractTests(_Scratch):
    """Each of these is an attribute the real command path reads."""

    def test_sync_fanout_is_suppressed(self):
        """A bulletin post fans out to bbs_nodes; those frames would land in
        the transcript. The row still reaches peers via server.py's own
        reconcile cycle."""
        self.assertEqual(self.session().interface.bbs_nodes, [])

    def test_it_is_marked_low_latency(self):
        """Otherwise send_message sleeps two seconds between chunks, which is
        correct on air and unusable in a browser."""
        self.assertTrue(self.session().interface.is_low_latency)
        self.assertEqual(
            utils.get_user_message_pause_seconds(self.session().interface), 0)

    def test_send_text_returns_something_with_an_id(self):
        """utils.send_message logs d.id; returning None breaks every reply."""
        interface = bbs_emulator.EmulatorInterface({})
        self.assertIsNotNone(
            interface.sendText(text="hi", destinationId=1).id)

    def test_a_node_number_resolves_back_to_its_id(self):
        session = self.session()
        self.assertEqual(
            utils.get_node_id_from_num(session.sender_id, session.interface),
            session.sender_node_id)


class LifecycleTests(_Scratch):
    def test_closing_clears_menu_state(self):
        session = bbs_emulator.start_session()
        session.send("?")
        sender_id = session.sender_id
        bbs_emulator.end_session(session.token)
        self.assertNotIn(sender_id, utils.user_states)

    def test_closing_stops_a_trivia_session(self):
        import trivia_port

        session = bbs_emulator.start_session()
        trivia_port._sessions[session.sender_id] = {"score": 1, "moves": 1}
        bbs_emulator.end_session(session.token)
        self.assertNotIn(session.sender_id, trivia_port._sessions)

    def test_reset_clears_state_but_keeps_the_identity(self):
        session = self.session()
        session.send("?")
        session.send("B")
        before = session.sender_node_id
        self.assertTrue(bbs_emulator.reset_session(session.token))
        self.assertIsNone(session.menu_state())
        self.assertEqual(session.sender_node_id, before)
        self.assertIs(bbs_emulator.get_session(session.token), session)

    def test_an_unknown_token_resolves_to_nothing(self):
        self.assertIsNone(bbs_emulator.get_session("not-a-token"))
        self.assertFalse(bbs_emulator.end_session("not-a-token"))

    def test_idle_sessions_are_swept(self):
        session = bbs_emulator.start_session()
        session.send("?")
        session.last_used -= bbs_emulator.SESSION_IDLE_SECONDS + 1
        self.assertEqual(bbs_emulator.sweep_idle(), 1)
        self.assertIsNone(bbs_emulator.get_session(session.token))
        self.assertNotIn(session.sender_id, utils.user_states)

    def test_a_busy_session_is_not_swept(self):
        session = self.session()
        session.send("?")
        self.assertEqual(bbs_emulator.sweep_idle(), 0)
        self.assertIsNotNone(bbs_emulator.get_session(session.token))

    def test_the_oldest_session_is_evicted_at_the_cap(self):
        first = bbs_emulator.start_session()
        first.last_used -= 5
        for _ in range(bbs_emulator.MAX_SESSIONS):
            self.session()
        self.assertIsNone(bbs_emulator.get_session(first.token))
        self.assertLessEqual(bbs_emulator.active_session_count(),
                             bbs_emulator.MAX_SESSIONS)


if __name__ == "__main__":
    unittest.main()
