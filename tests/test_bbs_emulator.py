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
import unittest.mock

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


class RosterTelemetryTests(_Scratch):
    """Settings -> View Stats reads hwModel/role/lastHeard off a real
    Meshtastic interface's own node shape. Over SSH and the web admin,
    interface.nodes IS _roster_nodes()'s synthetic build from mesh_clients
    -- and until now it never carried any of the three, so every node
    showed "Unknown" hardware and role and zero activity in every recent
    window, even though mesh_clients holds real values for all three.
    """

    def _client(self, node_id, **overrides):
        row = {
            "link_name": "primary", "node_id": node_id, "node_num": 12345,
            "protocol": "meshtastic", "short_name": "abc", "long_name": "ABC Node",
            "hw_model": "", "role": "", "battery_level": None,
            "last_heard_epoch": None,
        }
        row.update(overrides)
        db_operations.upsert_mesh_clients([row])

    def test_hw_model_and_role_are_carried_through(self):
        self._client("!abc", hw_model="TBEAM", role="CLIENT")
        node = bbs_emulator._roster_nodes()["!abc"]
        self.assertEqual(node["user"]["hwModel"], "TBEAM")
        self.assertEqual(node["user"]["role"], "CLIENT")

    def test_a_client_with_no_recorded_hardware_still_falls_back_to_unknown(self):
        """Not "", not 0 -- an absent key, so handlers' own .get(..., 'Unknown')
        default still fires instead of a falsy value masquerading as one."""
        self._client("!abc")
        node = bbs_emulator._roster_nodes()["!abc"]
        self.assertNotIn("hwModel", node["user"])
        self.assertNotIn("role", node["user"])

    def test_meshtastic_last_heard_epoch_is_carried_through(self):
        import time
        now = int(time.time())
        self._client("!abc", last_heard_epoch=now)
        node = bbs_emulator._roster_nodes()["!abc"]
        self.assertEqual(node["lastHeard"], now)

    def test_meshcore_nodes_fall_back_to_last_seen(self):
        """Only Meshtastic reports last_heard_epoch; MeshCore/MQTT nodes are
        tracked only through the roster-presence sweep (last_seen). Reading
        ONLY last_heard_epoch would show these nodes as never active."""
        self._client("!mc", protocol="meshcore", last_heard_epoch=None)
        node = bbs_emulator._roster_nodes()["!mc"]
        self.assertIsNotNone(node.get("lastHeard"))
        self.assertGreater(node["lastHeard"], 0)

    def test_a_client_with_no_activity_recorded_at_all_has_no_lastheard_key(self):
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO mesh_clients (link_name, node_id, node_num, protocol,"
            " short_name, long_name, hw_model, role, battery_level,"
            " last_heard_epoch, first_seen, last_seen)"
            " VALUES ('primary', '!nolast', 1, 'meshtastic', 'x', 'X', '', '',"
            " NULL, NULL, '2026-01-01 00:00:00', '')")
        conn.commit()
        node = bbs_emulator._roster_nodes()["!nolast"]
        self.assertNotIn("lastHeard", node)

    def test_stats_reports_real_hardware_and_roles_over_a_session(self):
        """End to end: driving the actual Settings -> View Stats flow a
        beta tester would use, not the roster builder in isolation."""
        self._client("!abc", hw_model="TBEAM", role="CLIENT")
        self._client("!def", hw_model="HELTEC_V3", role="ROUTER")
        session = self.session()
        session.send("!S")
        # View Stats is [7] on the merged Settings & Profile screen.
        chunks, error = session.send("7")
        self.assertIsNone(error)
        body = self.text_of(chunks)
        chunks, error = session.send("2")
        self.assertIsNone(error)
        body += self.text_of(chunks)
        self.assertIn("TBEAM", body)
        self.assertIn("HELTEC_V3", body)
        self.assertNotIn("Unknown: 2", body)

    def test_stats_counts_recent_meshtastic_activity(self):
        import time
        now = int(time.time())
        self._client("!abc", last_heard_epoch=now)
        session = self.session()
        session.send("!S")
        chunks, error = session.send("7")
        self.assertIsNone(error)
        body = self.text_of(chunks)
        chunks, error = session.send("1")
        self.assertIsNone(error)
        body += self.text_of(chunks)
        self.assertIn("Last hour: 1", body)


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


class SyntheticIdentityTests(_Scratch):
    """A new emulator session must be a new person.

    The session counter is module state that restarted at 1 on every restart
    of the web admin, so each new session was handed an id an earlier one had
    already used. process_message treats "no profile row" as first contact,
    so a genuinely new tester silently inherited an old identity: no welcome,
    someone else's short name and message count, and -- found on the live
    node -- someone else's bio.
    """

    def setUp(self):
        super().setUp()
        bbs_emulator._synthetic_counter = 0
        bbs_emulator._synthetic_counter_seeded = False
        self.addCleanup(self._reset_counter)

    def _reset_counter(self):
        bbs_emulator._synthetic_counter = 0
        bbs_emulator._synthetic_counter_seeded = False

    def seed_old_sessions(self, count, bio_on=None):
        for n in range(1, count + 1):
            db_operations.auto_upsert_user_profile(
                bbs_emulator._SYNTHETIC_NUM_BASE + n, f"old{n}", f"old{n}")
        if bio_on:
            db_operations.update_user_bio(
                bbs_emulator._SYNTHETIC_NUM_BASE + bio_on, "an earlier tester's bio")

    def test_a_new_session_does_not_reuse_an_old_id(self):
        self.seed_old_sessions(4)
        session = self.session()
        self.assertGreater(session.sender_id, bbs_emulator._SYNTHETIC_NUM_BASE + 4)

    def test_a_new_session_does_not_inherit_a_stranger_s_bio(self):
        self.seed_old_sessions(4, bio_on=3)
        session = self.session()
        session.send("")
        profile = db_operations.get_user_profile(session.sender_id)
        self.assertEqual(profile[6], "")

    def test_a_new_session_is_greeted(self):
        """First contact is decided by whether a profile row exists, so a
        reused id silently skipped the welcome."""
        self.seed_old_sessions(4)
        session = self.session()
        chunks, error = session.send("")
        self.assertIsNone(error)
        self.assertIn("Send ? any time for the menu", self.text_of(chunks))

    def test_two_sessions_in_a_row_are_different_people(self):
        first = self.session()
        second = self.session()
        self.assertNotEqual(first.sender_id, second.sender_id)

    def test_an_unreadable_database_still_opens_a_session(self):
        """Reusing an id is bad; refusing to open the emulator at all is
        worse, so a failed seed must not raise."""
        with unittest.mock.patch.object(
                db_operations, "get_db_connection", side_effect=RuntimeError("down")):
            bbs_emulator._seed_synthetic_counter()
        self.assertTrue(self.session().token)
