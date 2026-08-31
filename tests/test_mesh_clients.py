"""Tests for the durable mesh client roster (mesh_clients table).

interface.nodes is transient -- rebuilt from scratch on every reconnect --
so this is the only place "who's in range" survives a restart. Covers both
the db_operations upsert/query layer and server.persist_mesh_clients, which
sweeps every active link's live node roster into it on the same cadence as
the existing diagnostics snapshot / MQTT status publish.
"""
import sqlite3
import sys
import types
import unittest

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations


class MeshClientsDbTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _row(self, **overrides):
        row = {
            "link_name": "primary",
            "node_id": "!04059140",
            "node_num": 67437888,
            "protocol": "Meshtastic",
            "short_name": "ABCD",
            "long_name": "A Test Node",
            "hw_model": "TBEAM",
            "role": "CLIENT",
            "battery_level": 87,
            "last_heard_epoch": 1755000000,
        }
        row.update(overrides)
        return row

    def test_upsert_then_get_round_trips_all_fields(self):
        db_operations.upsert_mesh_clients([self._row()])

        clients = db_operations.get_mesh_clients()
        self.assertEqual(len(clients), 1)
        c = clients[0]
        self.assertEqual(c["link_name"], "primary")
        self.assertEqual(c["node_id"], "!04059140")
        self.assertEqual(c["node_num"], "67437888")  # stored/returned as text
        self.assertEqual(c["protocol"], "Meshtastic")
        self.assertEqual(c["short_name"], "ABCD")
        self.assertEqual(c["long_name"], "A Test Node")
        self.assertEqual(c["hw_model"], "TBEAM")
        self.assertEqual(c["role"], "CLIENT")
        self.assertEqual(c["battery_level"], 87)
        self.assertEqual(c["last_heard_epoch"], 1755000000)
        self.assertTrue(c["first_seen"])
        self.assertTrue(c["last_seen"])
        self.assertEqual(c["first_seen"], c["last_seen"])  # first sighting

    def test_upsert_same_node_updates_fields_but_preserves_first_seen(self):
        db_operations.upsert_mesh_clients([self._row(short_name="OLD")])
        first_seen = db_operations.get_mesh_clients()[0]["first_seen"]

        # Force a distinguishable later timestamp for the second sighting.
        import time
        time.sleep(1.1)
        db_operations.upsert_mesh_clients([self._row(short_name="NEW", battery_level=50)])

        clients = db_operations.get_mesh_clients()
        self.assertEqual(len(clients), 1)  # still one row, not a duplicate
        c = clients[0]
        self.assertEqual(c["short_name"], "NEW")
        self.assertEqual(c["battery_level"], 50)
        self.assertEqual(c["first_seen"], first_seen)  # unchanged
        self.assertGreaterEqual(c["last_seen"], first_seen)

    def test_same_node_id_different_links_are_independent_rows(self):
        """The same physical node reachable on two links (e.g. a dual-radio
        bridge node) gets its own row per link, not one shared row."""
        db_operations.upsert_mesh_clients([
            self._row(link_name="primary", short_name="ON-PRI"),
            self._row(link_name="secondary", short_name="ON-SEC"),
        ])

        all_clients = db_operations.get_mesh_clients()
        self.assertEqual(len(all_clients), 2)

        primary_only = db_operations.get_mesh_clients(link_name="primary")
        self.assertEqual(len(primary_only), 1)
        self.assertEqual(primary_only[0]["short_name"], "ON-PRI")

    def test_get_mesh_clients_orders_most_recently_seen_first(self):
        db_operations.upsert_mesh_clients([self._row(node_id="!aaa", short_name="AAA")])
        import time
        time.sleep(1.1)
        db_operations.upsert_mesh_clients([self._row(node_id="!bbb", short_name="BBB")])

        clients = db_operations.get_mesh_clients()
        self.assertEqual([c["node_id"] for c in clients], ["!bbb", "!aaa"])

    def test_upsert_empty_list_is_a_noop(self):
        db_operations.upsert_mesh_clients([])
        self.assertEqual(db_operations.get_mesh_clients(), [])

    def test_upsert_tolerates_missing_optional_fields(self):
        """MeshCore/MQTT nodes never populate battery_level/last_heard_epoch
        -- must store cleanly as NULL, not raise or coerce to a wrong type."""
        db_operations.upsert_mesh_clients([{
            "link_name": "mqtt1",
            "node_id": "mqtt:baconbs/city-a-b:node-b",
            "node_num": 12345,
            "protocol": "MQTT:mqtt1",
            "short_name": "node",
            "long_name": "node-b",
            "hw_model": "MQTT",
            "role": "bridge",
            "battery_level": None,
            "last_heard_epoch": None,
        }])
        c = db_operations.get_mesh_clients()[0]
        self.assertIsNone(c["battery_level"])
        self.assertIsNone(c["last_heard_epoch"])

    def test_synced_clients_are_namespaced_and_preserve_remote_times(self):
        row = self._row(
            first_seen="2026-08-30 10:00:00",
            last_seen="2026-08-30 11:00:00",
        )
        db_operations.upsert_synced_mesh_clients("mqtt1", "forge", [row])

        client = db_operations.get_mesh_clients()[0]
        self.assertEqual(client["link_name"], "remote:mqtt1:forge:primary")
        self.assertEqual(client["first_seen"], "2026-08-30 10:00:00")
        self.assertEqual(client["last_seen"], "2026-08-30 11:00:00")

    def test_older_synced_snapshot_does_not_overwrite_newer_one(self):
        newer = self._row(short_name="NEW", first_seen="2026-08-30 10:00:00",
                          last_seen="2026-08-30 12:00:00")
        older = self._row(short_name="OLD", first_seen="2026-08-30 09:00:00",
                          last_seen="2026-08-30 11:00:00")
        db_operations.upsert_synced_mesh_clients("mqtt1", "forge", [newer])
        db_operations.upsert_synced_mesh_clients("mqtt1", "forge", [older])

        client = db_operations.get_mesh_clients()[0]
        self.assertEqual(client["short_name"], "NEW")
        self.assertEqual(client["last_seen"], "2026-08-30 12:00:00")


_CACHE_KEYS = ("config_init", "server", "radio_link")


class _MeshInterfaceError(Exception):
    pass


def _install_fake_meshtastic_package():
    def _stub(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    mesh = _stub("meshtastic")
    mesh.BROADCAST_NUM = 0
    mesh.mesh_interface = _stub("meshtastic.mesh_interface")
    mesh.stream_interface = _stub("meshtastic.stream_interface")
    mesh.serial_interface = _stub("meshtastic.serial_interface")
    mesh.tcp_interface = _stub("meshtastic.tcp_interface")
    mesh.stream_interface.StreamInterface = object
    mesh.mesh_interface.MeshInterface = types.SimpleNamespace(MeshInterfaceError=_MeshInterfaceError)
    return mesh


class _FreshServerCase(unittest.TestCase):
    """Gives each test its own freshly-imported server/radio_link modules --
    see tests/test_radio_link_watchdog.py for why this dance is needed
    (config_init.py/server.py get cached in sys.modules bound to whichever
    fake meshtastic package was active at first import)."""

    def setUp(self):
        self._saved = {key: sys.modules.pop(key, None) for key in _CACHE_KEYS}
        self._saved_meshtastic = {
            name: mod for name, mod in sys.modules.items()
            if name == "meshtastic" or name.startswith("meshtastic.")
        }
        for name in list(self._saved_meshtastic):
            del sys.modules[name]

        _install_fake_meshtastic_package()
        import server as _server
        from radio_link import RadioLink as _RadioLink
        self.server = _server
        self.RadioLink = _RadioLink

        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

        for name in list(sys.modules):
            if name == "meshtastic" or name.startswith("meshtastic.") or name in _CACHE_KEYS:
                del sys.modules[name]
        sys.modules.update(self._saved_meshtastic)
        for key, mod in self._saved.items():
            if mod is not None:
                sys.modules[key] = mod


class _FakeInterface:
    """Minimal stand-in shaped like the common subset of Meshtastic's native
    interface / MeshCoreInterface / MqttInterface: .nodes dict, .myInfo,
    .protocol_name."""

    def __init__(self, nodes, my_node_num, protocol_name="Meshtastic"):
        self.nodes = nodes
        self.myInfo = types.SimpleNamespace(my_node_num=my_node_num)
        self.protocol_name = protocol_name


class PersistMeshClientsTests(_FreshServerCase):
    def test_persists_nodes_excluding_self(self):
        nodes = {
            "!self0000": {"num": 1, "user": {"id": "!self0000", "shortName": "ME", "longName": "Myself"}},
            "!peer1111": {
                "num": 2,
                "user": {"id": "!peer1111", "shortName": "PEER", "longName": "A Peer", "hwModel": "TBEAM", "role": "CLIENT"},
                "lastHeard": 1755000000,
                "deviceMetrics": {"batteryLevel": 42},
            },
        }
        link = self.RadioLink("primary", _FakeInterface(nodes, my_node_num=1))

        self.server.persist_mesh_clients([link])

        clients = db_operations.get_mesh_clients()
        self.assertEqual(len(clients), 1)  # self excluded
        c = clients[0]
        self.assertEqual(c["node_id"], "!peer1111")
        self.assertEqual(c["link_name"], "primary")
        self.assertEqual(c["short_name"], "PEER")
        self.assertEqual(c["hw_model"], "TBEAM")
        self.assertEqual(c["battery_level"], 42)
        self.assertEqual(c["last_heard_epoch"], 1755000000)

    def test_multiple_links_persist_independently(self):
        primary = self.RadioLink(
            "primary",
            _FakeInterface(
                {"!aaa": {"num": 10, "user": {"id": "!aaa", "shortName": "AAA", "longName": "Node A"}}},
                my_node_num=1,
                protocol_name="Meshtastic",
            ),
        )
        secondary = self.RadioLink(
            "secondary",
            _FakeInterface(
                {"deadbeef01": {"num": 20, "user": {"id": "deadbeef01", "shortName": "BBB", "longName": "Node B"}}},
                my_node_num=2,
                protocol_name="MeshCore",
            ),
        )

        self.server.persist_mesh_clients([primary, secondary])

        clients = db_operations.get_mesh_clients()
        self.assertEqual(len(clients), 2)
        by_link = {c["link_name"]: c for c in clients}
        self.assertEqual(by_link["primary"]["protocol"], "Meshtastic")
        self.assertEqual(by_link["secondary"]["protocol"], "MeshCore")

    def test_link_with_no_nodes_attribute_does_not_crash(self):
        class _BareInterface:
            pass

        link = self.RadioLink("primary", _BareInterface())
        self.server.persist_mesh_clients([link])  # must not raise
        self.assertEqual(db_operations.get_mesh_clients(), [])

    def test_link_with_none_interface_does_not_crash(self):
        # A link that never connected at startup (config_init._open_interface
        # gave up and returned None -- see the radio-independence fix).
        link = self.RadioLink("primary", None)
        self.server.persist_mesh_clients([link])  # must not raise
        self.assertEqual(db_operations.get_mesh_clients(), [])

    def test_malformed_node_entry_is_skipped_not_fatal(self):
        nodes = {
            "!good": {"num": 5, "user": {"id": "!good", "shortName": "GOOD"}},
            "!bad": "not-a-dict",
        }
        link = self.RadioLink("primary", _FakeInterface(nodes, my_node_num=1))
        self.server.persist_mesh_clients([link])  # must not raise
        clients = db_operations.get_mesh_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["node_id"], "!good")


if __name__ == "__main__":
    unittest.main()


class MeshClientsRecencyQueryTests(unittest.TestCase):
    """Database-level recency filter used by MQTT publishing."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _insert(self, node_id, hours_ago):
        from datetime import datetime, timedelta
        ts = (datetime.now() - timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO mesh_clients (link_name, node_id, protocol, first_seen, last_seen) "
            "VALUES (?, ?, 'Meshtastic', ?, ?)", ("primary", node_id, ts, ts))
        conn.commit()

    def test_filters_to_the_requested_window(self):
        self._insert("!recent", 1)
        self._insert("!stale", 100)
        ids = [c["node_id"] for c in db_operations.get_mesh_clients(seen_within_seconds=24*3600)]
        self.assertEqual(ids, ["!recent"])

    def test_no_window_returns_everything(self):
        self._insert("!recent", 1)
        self._insert("!stale", 100)
        self.assertEqual(len(db_operations.get_mesh_clients()), 2)

    def test_window_combines_with_link_filter(self):
        self._insert("!recent", 1)
        self.assertEqual(
            len(db_operations.get_mesh_clients(link_name="primary", seen_within_seconds=24*3600)), 1)
        self.assertEqual(
            len(db_operations.get_mesh_clients(link_name="other", seen_within_seconds=24*3600)), 0)


class ClientNameFlatteningTests(unittest.TestCase):
    """Node-supplied names are free text, and at least one real device
    reports a long name spread over three lines. Stored raw it renders as a
    tall ragged cell that drags the whole table row's height with it."""

    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _store(self, **fields):
        row = {
            "link_name": "primary", "node_id": "!aaa11111", "node_num": 1,
            "protocol": "Meshtastic", "short_name": "AAA", "long_name": "A",
            "hw_model": "HELTEC_V3", "role": "CLIENT",
            "battery_level": None, "last_heard_epoch": None,
        }
        row.update(fields)
        db_operations.upsert_mesh_clients([row])
        return db_operations.get_mesh_clients()[0]

    def test_newlines_in_long_name_are_collapsed(self):
        stored = self._store(long_name="USM Auriga Solar\nUSM Auriga\nUSM Auriga")
        self.assertEqual(stored["long_name"], "USM Auriga Solar USM Auriga USM Auriga")

    def test_short_name_is_flattened_too(self):
        self.assertEqual(self._store(short_name="A\nB")["short_name"], "A B")

    def test_surrounding_and_repeated_whitespace_collapses(self):
        self.assertEqual(self._store(long_name="  Bacon   BBS \t ")["long_name"], "Bacon BBS")

    def test_ordinary_names_are_untouched(self):
        self.assertEqual(self._store(long_name="Bacon BBS")["long_name"], "Bacon BBS")

    def test_missing_name_stays_empty(self):
        self.assertEqual(self._store(long_name=None)["long_name"], "")

    def test_existing_multiline_names_are_backfilled(self):
        """The sweep only rewrites nodes still in range, so a device that
        has gone quiet keeps whatever was stored before names were
        flattened -- which is the case that actually had one."""
        conn = db_operations.thread_local.connection
        conn.execute(
            "INSERT INTO mesh_clients (link_name, node_id, protocol, short_name,"
            " long_name, hw_model, role, first_seen, last_seen)"
            " VALUES ('primary', '!stale', 'Meshtastic', 'A'||CHAR(10)||'B',"
            " 'USM Auriga Solar'||CHAR(10)||'USM Auriga', 'HW', '', 'x', 'y')"
        )
        conn.commit()
        db_operations.initialize_database()
        row = conn.execute(
            "SELECT short_name, long_name FROM mesh_clients WHERE node_id='!stale'"
        ).fetchone()
        self.assertEqual(row, ("A B", "USM Auriga Solar USM Auriga"))


class NodeRoleResolutionTests(unittest.TestCase):
    """The Role column mixed readable names with a bare '1' and a lot of
    blanks, because two transports were storing whatever they received.

    Meshtastic sends role as a protobuf enum and MessageToDict omits any
    field at its default, so role 0 (CLIENT) -- the most common one --
    arrived as no key at all. MeshCore reads its advert type as a raw byte
    (meshcore/reader.py: dbuf.read(1)[0]), so it arrived as an int.
    """

    def test_meshtastic_omitted_role_means_client(self):
        self.assertEqual(db_operations.resolve_node_role({"id": "!a"}, "Meshtastic"), "CLIENT")

    def test_no_user_packet_is_unknown_rather_than_client(self):
        """An absent user dict means we never heard the node's User packet;
        filling in CLIENT there would be inventing a fact."""
        self.assertEqual(db_operations.resolve_node_role({}, "Meshtastic"), "")

    def test_numeric_meshtastic_role_resolves_to_its_name(self):
        self.assertEqual(db_operations.resolve_node_role({"id": "!a", "role": 1}, "Meshtastic"),
                         "CLIENT_MUTE")
        self.assertEqual(db_operations.resolve_node_role({"id": "!a", "role": "12"}, "Meshtastic"),
                         "CLIENT_BASE")

    def test_named_roles_pass_through(self):
        self.assertEqual(db_operations.resolve_node_role({"id": "!a", "role": "ROUTER_LATE"},
                                                   "Meshtastic"), "ROUTER_LATE")

    def test_unknown_role_number_is_reported_not_guessed(self):
        """A firmware addition should read as unrecognized rather than be
        silently mislabelled as an existing role."""
        self.assertEqual(db_operations.resolve_node_role({"id": "!a", "role": 99}, "Meshtastic"),
                         "role 99")

    def test_role_table_comes_from_the_protobuf(self):
        self.assertEqual(db_operations.MESHTASTIC_ROLE_NAMES[0], "CLIENT")
        self.assertEqual(db_operations.MESHTASTIC_ROLE_NAMES[11], "ROUTER_LATE")

    def test_meshcore_blank_role_is_not_forced_to_client(self):
        """Only Meshtastic omits a role to mean the default."""
        self.assertEqual(db_operations.resolve_node_role({"id": "key"}, "MeshCore"), "")

    def test_meshcore_advert_type_byte_becomes_a_name(self):
        import meshcore_interface
        self.assertEqual(meshcore_interface._adv_type_name(1), "Companion")
        self.assertEqual(meshcore_interface._adv_type_name(2), "Repeater")
        self.assertEqual(meshcore_interface._adv_type_name("3"), "Room Server")

    def test_unknown_advert_type_is_reported_not_guessed(self):
        import meshcore_interface
        self.assertEqual(meshcore_interface._adv_type_name(7), "type 7")

    def test_existing_numeric_roles_are_backfilled(self):
        """Stale rows are never re-swept, so the sweep alone would leave a
        node that has gone quiet showing '1' forever."""
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        try:
            db_operations.initialize_database()
            conn = db_operations.thread_local.connection
            for protocol, node_id, role in (
                ("MeshCore", "key1", "1"),
                ("Meshtastic", "!aaa", "1"),
                ("Meshtastic", "!bbb", "ROUTER"),
                ("Meshtastic", "!ccc", ""),
            ):
                conn.execute(
                    "INSERT INTO mesh_clients (link_name, node_id, protocol, short_name,"
                    " long_name, hw_model, role, first_seen, last_seen)"
                    " VALUES ('L', ?, ?, '', '', '', ?, 'x', 'y')",
                    (node_id, protocol, role),
                )
            conn.commit()
            db_operations.initialize_database()
            stored = dict(conn.execute("SELECT node_id, role FROM mesh_clients"))
            self.assertEqual(stored["key1"], "Companion")
            self.assertEqual(stored["!aaa"], "CLIENT_MUTE")
            self.assertEqual(stored["!bbb"], "ROUTER")
            # Blank is genuinely ambiguous, so the backfill must not guess.
            self.assertEqual(stored["!ccc"], "")
        finally:
            db_operations.thread_local.connection.close()
            del db_operations.thread_local.connection
