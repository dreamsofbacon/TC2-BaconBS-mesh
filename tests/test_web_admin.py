import configparser
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import db_operations
from web_admin import create_app


class FakeInterface:
    def __init__(self):
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = {"!abcd1234": {"num": 1234}}

    def getMyNodeInfo(self):
        return {
            "num": 1234,
            "user": {
                "id": "!abcd1234",
                "shortName": "BBS",
                "longName": "Bacon BBS",
            },
        }


class WebAdminSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.ini"
        self.db_path = self.root / "bulletins.db"
        self.runtime_diag_path = self.root / "runtime_diagnostics.json"
        self.manual_trigger_path = self.root / "manual_sync.trigger"
        self.force_check_trigger_path = self.root / "force_check.trigger"
        self.resolve_zork_save_trigger_path = self.root / "resolve_zork_save.trigger"
        self.resolve_record_trigger_path = self.root / "resolve_record.trigger"

        config = configparser.ConfigParser()
        config["admin"] = {
            "username": "admin",
            "password": "oldpass",
        }
        config["boards"] = {
            "bulletin_boards": "General,Info,News,Urgent",
        }
        config["sync"] = {
            "bbs_nodes": "!oldpeer",
            "sync_interval_minutes": "5",
            "sync_zork_saves": "true",
        }
        config["allow_list"] = {
            "allowed_nodes": "!oldurgent",
        }
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config.write(config_file)

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BBS_CONFIG_PATH": str(self.config_path),
                "BBS_DB_PATH": str(self.db_path),
                "BBS_RUNTIME_DIAG_PATH": str(self.runtime_diag_path),
                "BBS_MANUAL_SYNC_TRIGGER_PATH": str(self.manual_trigger_path),
                "BBS_FORCE_CHECK_TRIGGER_PATH": str(self.force_check_trigger_path),
                "BBS_ZORK_SAVE_RESOLVE_TRIGGER_PATH": str(self.resolve_zork_save_trigger_path),
                "BBS_RECORD_RESOLVE_TRIGGER_PATH": str(self.resolve_record_trigger_path),
                "BBS_WEBGUI_SECRET": "test-secret",
                "BBS_VERSION_DISPLAY": "test-version",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        db_operations.remove_connection_log_handler()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def login(self, client, username="admin", password="oldpass"):
        csrf_token = self.get_csrf_token(client)
        return client.post(
            "/login",
            data={"username": username, "password": password, "csrf_token": csrf_token},
            follow_redirects=False,
        )

    def get_csrf_token(self, client):
        response = client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        return payload["csrf_token"]

    def post_with_csrf(self, client, path, data=None, json_data=None, follow_redirects=False):
        csrf_token = self.get_csrf_token(client)
        if json_data is not None:
            return client.post(
                path,
                json=json_data,
                headers={"X-CSRF-Token": csrf_token},
                follow_redirects=follow_redirects,
            )
        form = dict(data or {})
        form["csrf_token"] = csrf_token
        return client.post(path, data=form, follow_redirects=follow_redirects)

    def _settings_page(self):
        app = create_app()
        client = app.test_client()
        self.login(client)
        return client.get("/settings").get_data(as_text=True)

    def test_every_settings_nav_link_has_a_panel_and_vice_versa(self):
        """The sidebar is the only way to reach a section once panels are
        tabbed, so a link with no panel is a dead end and a panel with no
        link is unreachable."""
        import re
        page = self._settings_page()
        links = set(re.findall(r'data-panel-link="([\w-]+)"', page))
        panels = set(re.findall(r'data-settings-panel="([\w-]+)"', page))
        self.assertTrue(panels, "no settings panels rendered")
        self.assertEqual(links, panels)

    def test_settings_panels_keep_their_anchor_ids(self):
        """Section forms POST back to <url>#section and the sidebar reads
        the hash to restore the panel, so the ids are load-bearing."""
        page = self._settings_page()
        for section in ("links", "devices", "mqtt", "boards", "sync", "gateway",
                        "accounts", "subscribers", "storage", "admin",
                        "diagnostics", "danger"):
            with self.subTest(section=section):
                self.assertIn(f'id="{section}"', page)

    def test_settings_sections_render_without_javascript(self):
        """Single-panel mode is applied by settings-nav.js, so with
        scripting off the page must still render every section stacked."""
        page = self._settings_page()
        self.assertNotIn("is-tabbed", page)
        self.assertIn("settings-nav.js", page)

    def test_settings_page_contains_all_sections(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("Board Settings", page)
        self.assertIn("Sync Settings", page)
        self.assertIn("Admin Credentials", page)
        self.assertIn("Diagnostics", page)
        self.assertIn("test-version", page)
        self.assertIn("Peer Hash Graph", page)
        self.assertIn("Resolve Save by Best Candidate", page)

    def test_diagnostics_shows_db_size_and_mailbox_depth(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        # Seed a mailbox entry (one pending) so the depth indicator is non-zero.
        db_operations.initialize_database()
        db_operations.enqueue_api_response("rid1", "!node", "200", "hello world")

        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Database size:", page)
        self.assertIn("WAL:", page)
        self.assertIn("Total on disk:", page)
        self.assertIn("API mailbox:", page)
        self.assertIn("1 stored (1 pending delivery)", page)

    def test_admin_password_change_persists_across_app_restart(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "admin",
                "current_password": "oldpass",
                "new_username": "",
                "new_password": "newpass",
                "confirm_password": "newpass",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/logout"))

        restarted_app = create_app()
        restarted_client = restarted_app.test_client()
        login_response = self.login(restarted_client, password="newpass")
        self.assertEqual(login_response.status_code, 302)
        self.assertTrue(login_response.headers["Location"].endswith("/bulletins"))

    def test_sync_settings_update_config_and_runtime_interface(self):
        runtime_interface = FakeInterface()
        app = create_app(runtime_interface=runtime_interface)
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        save_response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "sync",
                "bbs_nodes": "!node1\n!node2\n!node1",
                "allowed_nodes": "!allow1, !allow2",
                "sync_interval_minutes": "5",
                "sync_zork_saves": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(runtime_interface.bbs_nodes, ["!node1", "!node2"])
        self.assertEqual(runtime_interface.allowed_nodes, ["!allow1", "!allow2"])

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("sync", "bbs_nodes"), "!node1,!node2")
        self.assertEqual(config.get("sync", "sync_interval_minutes"), "5")
        self.assertEqual(config.get("sync", "sync_zork_saves"), "true")
        self.assertEqual(config.get("allow_list", "allowed_nodes"), "!allow1,!allow2")

    def test_sync_speed_settings_are_saved_from_gui(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        save_response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "sync",
                "bbs_nodes": "!node1",
                "allowed_nodes": "!allow1",
                "sync_interval_minutes": "7",
                "sync_zork_saves": "1",
                "sync_turbo": "1",
                "sync_pause_seconds": "0.05",
                "hash_repair_pause_seconds": "0.01",
                "full_sync_delay_ms": "25",
            },
            follow_redirects=True,
        )
        self.assertEqual(save_response.status_code, 200)

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("sync", "sync_turbo"), "true")
        self.assertEqual(config.get("sync", "sync_pause_seconds"), "0.05")
        self.assertEqual(config.get("sync", "hash_repair_pause_seconds"), "0.01")
        self.assertEqual(config.get("sync", "full_sync_delay_ms"), "25")

    def test_manual_sync_api_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = self.post_with_csrf(client, "/api/sync/manual", json_data={})
        self.assertEqual(api_response.status_code, 200)
        self.assertTrue(self.manual_trigger_path.exists())

    def test_force_check_api_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = self.post_with_csrf(client, "/api/sync/force-check", json_data={})
        self.assertEqual(api_response.status_code, 200)
        self.assertTrue(self.force_check_trigger_path.exists())

    def test_resolve_zork_save_api_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = self.post_with_csrf(client, "/api/sync/resolve-zork-save", json_data={"user_id": "1234", "game_id": "zork1"})
        self.assertEqual(api_response.status_code, 200)
        self.assertTrue(self.resolve_zork_save_trigger_path.exists())

    def test_resolve_record_api_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = self.post_with_csrf(client, "/api/sync/resolve-record", json_data={"scope": "bulletins", "key": "uid-b"})
        self.assertEqual(api_response.status_code, 200)
        self.assertTrue(self.resolve_record_trigger_path.exists())

    def test_bulletins_list_shows_incomplete_marker_and_resolve_action(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0, expected_content_length INTEGER, content_complete INTEGER NOT NULL DEFAULT 1)")
        conn.execute(
            "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only, expected_content_length, content_complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("General", "CALL", "2026-04-06 12:00", "Short", "partial", "uid-incomplete", 0, 20, 0),
        )
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        page = client.get("/bulletins").get_data(as_text=True)
        self.assertIn("Incomplete", page)
        self.assertIn("Resolve", page)
        self.assertIn("uid-incomple", page)

    def test_bulletin_new_page_populates_board_dropdown(self):
        """Regression test: bulletin_new.html must be given the SAME context
        key the route passes ('bulletin_boards') -- it previously looped over
        an undefined 'boards' variable, which Jinja silently renders as zero
        <option> elements (no error) instead of failing loudly, so the board
        dropdown was empty on every "New Bulletin" page load."""
        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        page = client.get("/bulletins/new").get_data(as_text=True)
        for board in ("General", "Info", "News", "Urgent"):
            self.assertIn(f'value="{board}"', page)

    def test_bulletin_new_post_creates_bulletin_with_selected_board(self):
        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        post_response = self.post_with_csrf(
            client,
            "/bulletins/new",
            data={
                "board": "News",
                "sender_short_name": "CALL",
                "subject": "Test Subject",
                "content": "Test content",
            },
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 302)

        page = client.get("/bulletins").get_data(as_text=True)
        self.assertIn("Test Subject", page)

    def test_resolve_zork_save_settings_action_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        post_response = self.post_with_csrf(
            client,
            "/settings",
            data={"settings_section": "resolve_zork_save", "resolve_user_id": "1234", "resolve_game_id": "zork1"},
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertTrue(self.resolve_zork_save_trigger_path.exists())

    def test_force_check_settings_action_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        post_response = self.post_with_csrf(
            client,
            "/settings",
            data={"settings_section": "force_check"},
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 302)
        self.assertTrue(self.force_check_trigger_path.exists())

    def test_settings_post_without_csrf_is_rejected(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        post_response = client.post(
            "/settings",
            data={"settings_section": "force_check"},
            follow_redirects=False,
        )
        self.assertEqual(post_response.status_code, 403)

    def test_sync_status_api_returns_snapshot_values(self):
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "sync_in_progress": True,
                    "sync_progress_percent": 44,
                    "sync_current_phase": "syncing_mail",
                    "sync_next_run_epoch": 9999999999,
                    "sync_interval_minutes": 5,
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = client.get("/api/sync/status")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.get_json()
        self.assertEqual(payload["in_progress"], True)
        self.assertEqual(payload["progress_percent"], 44)
        self.assertEqual(payload["phase"], "syncing_mail")
        self.assertEqual(payload["sync_interval_minutes"], 5)

    def test_sync_mismatches_api_returns_scope_breakdown(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, sender_short_name TEXT, recipient TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS zork_saves (user_id TEXT NOT NULL, game_id TEXT NOT NULL DEFAULT 'zork1', save_data BLOB NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, short_name TEXT NOT NULL DEFAULT '', long_name TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, messages_sent INTEGER NOT NULL DEFAULT 0, bio TEXT NOT NULL DEFAULT '')")
        conn.execute("CREATE TABLE IF NOT EXISTS game_scores (user_id TEXT NOT NULL, game_id TEXT NOT NULL, short_name TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0, max_score INTEGER NOT NULL DEFAULT 0, moves INTEGER NOT NULL DEFAULT 0, achieved_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS peer_sync_state (peer_node_id TEXT PRIMARY KEY, bulletins INTEGER NOT NULL DEFAULT 0, mail INTEGER NOT NULL DEFAULT 0, channels INTEGER NOT NULL DEFAULT 0, zork_saves INTEGER NOT NULL DEFAULT 0, profiles INTEGER NOT NULL DEFAULT 0, game_scores INTEGER NOT NULL DEFAULT 0, bulletins_hash TEXT NOT NULL DEFAULT '', mail_hash TEXT NOT NULL DEFAULT '', channels_hash TEXT NOT NULL DEFAULT '', zork_saves_hash TEXT NOT NULL DEFAULT '', profiles_hash TEXT NOT NULL DEFAULT '', game_scores_hash TEXT NOT NULL DEFAULT '', reported_at TEXT NOT NULL)")
        reported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO peer_sync_state (peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores, bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash, reported_at) VALUES (?, 1, 0, 0, 0, 0, 0, 'bad', '', '', '', '', '', ?)", ("!oldpeer", reported_at))
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = client.get("/api/sync/mismatches")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.get_json()
        self.assertIn("summary", payload)
        self.assertIn("peers", payload)
        self.assertEqual(len(payload["peers"]), 1)
        self.assertEqual(payload["peers"][0]["peer_node_id"], "!oldpeer")
        self.assertIn("bulletins", payload["peers"][0]["mismatched_scopes"])

    def test_connection_events_api_returns_normalized_display_types(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS connection_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, sender_num TEXT, sender_node_id TEXT, sender_short_name TEXT, to_id TEXT, message_type TEXT NOT NULL, event_text TEXT NOT NULL)")
        conn.execute("INSERT INTO connection_events (event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text) VALUES ('2026-03-30 10:00:00', '1', '!peer1', 'PEER', '2', 'user', 'RX user message to 2')")
        conn.execute("INSERT INTO connection_events (event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text) VALUES ('2026-03-30 10:00:01', NULL, NULL, 'root', NULL, 'warning', 'Something is off')")
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = client.get("/api/connection-events?since_id=0")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.get_json()
        self.assertEqual(payload["events"][0]["display_type"], "rx")
        self.assertEqual(payload["events"][0]["display_label"], "RX")
        self.assertEqual(payload["events"][1]["display_type"], "warn")
        self.assertEqual(payload["events"][1]["display_label"], "WARN")

    def test_clients_page_renders_without_connection_events_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("General", "ALICE", "2026-03-30", "Hi", "Hello", "uid-1", 0),
        )
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        page_response = client.get("/clients")
        self.assertEqual(page_response.status_code, 200)
        page = page_response.get_data(as_text=True)
        self.assertIn("Client Post Counts", page)
        self.assertIn("ALICE", page)

    def test_clients_page_shows_known_mesh_clients_roster(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS mesh_clients (
                link_name TEXT NOT NULL, node_id TEXT NOT NULL, node_num TEXT,
                protocol TEXT NOT NULL DEFAULT '', short_name TEXT NOT NULL DEFAULT '',
                long_name TEXT NOT NULL DEFAULT '', hw_model TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '', battery_level INTEGER, last_heard_epoch INTEGER,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY (link_name, node_id))"""
        )
        conn.execute(
            "INSERT INTO mesh_clients (link_name, node_id, node_num, protocol, short_name, "
            "long_name, hw_model, role, battery_level, last_heard_epoch, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("primary", "!04059140", "67437888", "Meshtastic", "PEER", "A Peer Node",
             "TBEAM", "CLIENT", 76, 1755000000, "2026-03-30 12:00:00", "2026-03-30 12:05:00"),
        )
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        page_response = client.get("/clients")
        self.assertEqual(page_response.status_code, 200)
        page = page_response.get_data(as_text=True)
        self.assertIn("Known Mesh Clients", page)
        self.assertIn("PEER", page)
        self.assertIn("A Peer Node", page)
        self.assertIn("TBEAM", page)
        self.assertIn("76%", page)

    def test_clients_page_mesh_clients_empty_state(self):
        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        page_response = client.get("/clients")
        self.assertEqual(page_response.status_code, 200)
        page = page_response.get_data(as_text=True)
        self.assertIn("Known Mesh Clients", page)
        self.assertIn("No devices seen yet", page)

    def test_system_transmissions_page_counts_received_game_frames(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_transmissions (id INTEGER PRIMARY KEY AUTOINCREMENT, transmission_time TEXT NOT NULL, frame_type TEXT NOT NULL, destination_node_id TEXT, frame_size_bytes INTEGER, is_continuation INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO sync_transmissions (transmission_time, frame_type, destination_node_id, frame_size_bytes, is_continuation) VALUES (?, ?, ?, ?, ?)",
            ("2099-03-30T10:00:00Z", "SCORESYNC", "!peer1", 120, 0),
        )
        conn.commit()
        conn.close()

        db_operations.log_sync_transmission(
            "ZORKSAVE|save|user|game|2026-03-30T10:00:00Z|hash|0|1|chunk",
            "!peer1",
            180,
            direction="rx",
        )

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        page_response = client.get("/system/transmissions")
        self.assertEqual(page_response.status_code, 200)
        page = page_response.get_data(as_text=True)
        self.assertIn("Received from peers", page)
        self.assertIn("Game", page)
        self.assertIn("2 frames", page)
        self.assertIn("Live Transmission Log", page)
        self.assertIn("Recent Channel Activity", page)

    def test_sync_transmissions_api_returns_split_log_entries_with_filters(self):
        db_operations.log_sync_transmission(
            "HASHMISS|bulletins|uid-1",
            "!peer1",
            48,
            direction="tx",
        )
        db_operations.log_sync_transmission(
            "SYNCSTATE|peer|2|3|1|0|0|0|hash|hash|hash|hash|hash|hash",
            "!peer2",
            64,
            direction="rx",
        )

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = client.get("/api/sync/transmissions?frame_type=HASHMISS")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.get_json()
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["frame_type"], "HASHMISS")
        self.assertEqual(payload["entries"][0]["direction"], "tx")
        self.assertEqual(payload["entries"][0]["importance"], "important")
        self.assertIn("uid-1", payload["entries"][0]["preview"])

    def test_system_transmissions_reset_clears_history(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_transmissions (id INTEGER PRIMARY KEY AUTOINCREMENT, transmission_time TEXT NOT NULL, frame_type TEXT NOT NULL, destination_node_id TEXT, frame_size_bytes INTEGER, is_continuation INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO sync_transmissions (transmission_time, frame_type, destination_node_id, frame_size_bytes, is_continuation) VALUES (?, ?, ?, ?, ?)",
            ("2099-03-30T10:00:00Z", "SYNCSTATE", "!peer1", 64, 0),
        )
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        reset_response = self.post_with_csrf(client, "/system/transmissions/reset", data={}, follow_redirects=True)
        self.assertEqual(reset_response.status_code, 200)
        page = reset_response.get_data(as_text=True)
        self.assertIn("Transmission stats reset.", page)

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM sync_transmissions").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_settings_diagnostics_show_runtime_details(self):
        app = create_app(runtime_interface=FakeInterface())
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("App version:</strong> test-version", page)
        self.assertIn("Interface attached:</strong> Yes", page)
        self.assertIn("Local short name:</strong> BBS", page)
        self.assertIn("Local long name:</strong> Bacon BBS", page)

    def test_settings_page_renders_peer_hash_graph(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, sender_short_name TEXT, recipient TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS zork_saves (user_id TEXT NOT NULL, game_id TEXT NOT NULL DEFAULT 'zork1', save_data BLOB NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, short_name TEXT NOT NULL DEFAULT '', long_name TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, messages_sent INTEGER NOT NULL DEFAULT 0, bio TEXT NOT NULL DEFAULT '')")
        conn.execute("CREATE TABLE IF NOT EXISTS game_scores (user_id TEXT NOT NULL, game_id TEXT NOT NULL, short_name TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0, max_score INTEGER NOT NULL DEFAULT 0, moves INTEGER NOT NULL DEFAULT 0, achieved_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS peer_sync_state (peer_node_id TEXT PRIMARY KEY, bulletins INTEGER NOT NULL DEFAULT 0, mail INTEGER NOT NULL DEFAULT 0, channels INTEGER NOT NULL DEFAULT 0, zork_saves INTEGER NOT NULL DEFAULT 0, profiles INTEGER NOT NULL DEFAULT 0, game_scores INTEGER NOT NULL DEFAULT 0, bulletins_hash TEXT NOT NULL DEFAULT '', mail_hash TEXT NOT NULL DEFAULT '', channels_hash TEXT NOT NULL DEFAULT '', zork_saves_hash TEXT NOT NULL DEFAULT '', profiles_hash TEXT NOT NULL DEFAULT '', game_scores_hash TEXT NOT NULL DEFAULT '', reported_at TEXT NOT NULL)")
        conn.execute("INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only) VALUES ('General', 'CALL', '2026-03-30', 'Subj', 'Body', 'uid-b', 0)")
        conn.execute("INSERT INTO peer_sync_state (peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores, bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash, reported_at) VALUES ('!oldpeer', 3, 0, 0, 0, 0, 0, 'bad', '', '', '', '', '', '2026-03-30')")
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("Peer Hash Graph", page)
        self.assertIn("!oldpeer", page)
        self.assertIn("hash differs", page)

    def test_settings_page_renders_zork_save_tombstones_and_resolver_status(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, sender_short_name TEXT, recipient TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS zork_saves (user_id TEXT NOT NULL, game_id TEXT NOT NULL DEFAULT 'zork1', save_data BLOB NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS game_scores (user_id TEXT NOT NULL, game_id TEXT NOT NULL, short_name TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0, max_score INTEGER NOT NULL DEFAULT 0, moves INTEGER NOT NULL DEFAULT 0, achieved_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS connection_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, sender_num TEXT, sender_node_id TEXT, sender_short_name TEXT, to_id TEXT, message_type TEXT NOT NULL, event_text TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS deleted_sync_tombstones (tombstone_key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)")
        conn.execute("INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at) VALUES ('zork_saves:1234:zork1', '2026-03-30 12:05:00')")
        conn.commit()
        conn.close()

        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "candidate_resolution": {
                        "active": [{"key": "1234:zork1", "status": "collecting", "responses": 1, "expected": 2}],
                        "recent": [{"key": "1234:zork1", "result": "Best candidate save requested from !peer2 @ 2026-03-30 12:05:00 (12 bytes)"}],
                    }
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("zork_saves:1234:zork1", page)
        self.assertIn("Best-Candidate Resolver", page)
        self.assertIn("1234:zork1", page)

    def test_flowchart_page_includes_sync_pipeline_summary(self):
        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        flowchart_response = client.get("/system/flowchart")
        self.assertEqual(flowchart_response.status_code, 200)
        page = flowchart_response.get_data(as_text=True)
        self.assertIn("Five-Phase Mesh Sync", page)
        self.assertIn("DELETE_ZORKSAVE", page)
        self.assertIn("CANDREQ / CANDRSP", page)

    def test_radio_device_page_describes_meshcore_companion(self):
        config = configparser.ConfigParser()
        config.read(self.config_path)
        config["interface"] = {
            "type": "meshcore_tcp",
            "hostname": "192.0.2.20",
            "tcp_port": "5000",
        }
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config.write(config_file)

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = client.get("/system/meshtastic")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("MeshCore Companion Radio", page)
        self.assertIn("192.0.2.20:5000", page)
        self.assertIn("contact list", page)

    def test_radio_device_page_shows_both_radios_in_bridge_mode(self):
        config = configparser.ConfigParser()
        config.read(self.config_path)
        config["interface"] = {"type": "serial", "port": "COM3"}
        config["interface2"] = {
            "type": "meshcore_tcp",
            "hostname": "192.0.2.30",
            "tcp_port": "5000",
        }
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config.write(config_file)

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = client.get("/system/meshtastic")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Radio 1 (primary)", page)
        self.assertIn("Radio 2 (secondary", page)
        self.assertIn("MeshCore Companion Radio", page)
        self.assertIn("192.0.2.30:5000", page)
        self.assertIn("bridge mode", page)

    def test_radio_device_page_single_radio_unchanged_without_interface2(self):
        """No [interface2] at all -- must render exactly as before dual-radio
        bridge mode existed (no 'Radio 1'/'Radio 2' labels, no bridge-mode note)."""
        config = configparser.ConfigParser()
        config.read(self.config_path)
        config["interface"] = {"type": "serial", "port": "COM3"}
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config.write(config_file)

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = client.get("/system/meshtastic")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertNotIn("Radio 1 (primary)", page)
        self.assertNotIn("bridge mode", page)
        self.assertIn("Meshtastic Device", page)

    def test_settings_diagnostics_shows_per_radio_breakdown_in_bridge_mode(self):
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "interface_attached": True,
                    "interface_type": "SerialInterface",
                    "radio_protocol": "Meshtastic",
                    "mesh_node_count": 7,
                    "local_node_id": "!snap1234",
                    "bbs_nodes": ["!sync1"],
                    "allowed_nodes": [],
                    "radios": [
                        {
                            "name": "primary",
                            "interface_type": "SerialInterface",
                            "radio_protocol": "Meshtastic",
                            "connected": True,
                            "reconnecting": False,
                            "mesh_node_count": 7,
                            "local_node_id": "!snap1234",
                            "bbs_nodes": ["!sync1"],
                            "allowed_nodes": [],
                        },
                        {
                            "name": "secondary",
                            "interface_type": "MeshCoreInterface",
                            "radio_protocol": "MeshCore",
                            "connected": True,
                            "reconnecting": False,
                            "mesh_node_count": 3,
                            "local_node_id": "7e18ca9d",
                            "bbs_nodes": ["7e18ca9d30a1"],
                            "allowed_nodes": [],
                        },
                    ],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("Bridge mode active", page)
        self.assertIn("Radio: primary (Meshtastic)", page)
        self.assertIn("Radio: secondary (MeshCore)", page)
        self.assertIn("7e18ca9d30a1", page)

    def test_api_status_links_reports_every_link_including_mqtt(self):
        """The nav-bar status badges' poll endpoint must be generic over
        transport -- an MQTT bridge shows up exactly like a radio, since
        server.py's snapshot already treats every link uniformly."""
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "interface_attached": True,
                    "radios": [
                        {
                            "name": "primary",
                            "radio_protocol": "Meshtastic",
                            "connected": True,
                            "reconnecting": False,
                        },
                        {
                            "name": "secondary",
                            "radio_protocol": "MeshCore",
                            "connected": False,
                            "reconnecting": True,
                        },
                        {
                            "name": "mqtt1",
                            "radio_protocol": "MQTT:mqtt1",
                            "connected": False,
                            "reconnecting": False,
                        },
                    ],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = client.get("/api/status/links")
        self.assertEqual(response.status_code, 200)
        links = response.get_json()["links"]
        self.assertEqual(len(links), 3)

        by_name = {link["name"]: link for link in links}
        self.assertEqual(by_name["primary"]["protocol"], "Meshtastic")
        self.assertTrue(by_name["primary"]["connected"])
        self.assertFalse(by_name["primary"]["reconnecting"])

        self.assertEqual(by_name["secondary"]["protocol"], "MeshCore")
        self.assertFalse(by_name["secondary"]["connected"])
        self.assertTrue(by_name["secondary"]["reconnecting"])

        self.assertEqual(by_name["mqtt1"]["protocol"], "MQTT:mqtt1")
        self.assertFalse(by_name["mqtt1"]["connected"])
        self.assertFalse(by_name["mqtt1"]["reconnecting"])

    def test_api_status_links_includes_non_link_services_tagged_by_kind(self):
        """Services (JS8Call, API gateway) share the status display with
        links but are tagged 'service' so the UI knows not to offer a
        Reconnect button for something with no connection to cycle."""
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "radios": [
                        {"name": "primary", "radio_protocol": "Meshtastic",
                         "connected": True, "reconnecting": False},
                    ],
                    "services": [
                        {"name": "gateway", "protocol": "API Gateway",
                         "connected": True, "reconnecting": False},
                        {"name": "js8call", "protocol": "JS8Call",
                         "connected": False, "reconnecting": False},
                    ],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        links = client.get("/api/status/links").get_json()["links"]
        by_name = {entry["name"]: entry for entry in links}
        self.assertEqual(len(links), 3)
        self.assertEqual(by_name["primary"]["kind"], "link")
        self.assertEqual(by_name["gateway"]["kind"], "service")
        self.assertEqual(by_name["js8call"]["kind"], "service")
        self.assertFalse(by_name["js8call"]["connected"])

    def test_links_card_offers_reconnect_only_for_links_not_services(self):
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "radios": [
                        {"name": "primary", "radio_protocol": "Meshtastic",
                         "connected": True, "reconnecting": False},
                    ],
                    "services": [
                        {"name": "js8call", "protocol": "JS8Call",
                         "connected": True, "reconnecting": False},
                    ],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("JS8Call", page)
        # The link is reconnectable; the service is listed but has no button.
        self.assertIn('value="primary"', page)
        self.assertNotIn('name="link_name" value="js8call"', page)

    def test_api_status_links_snapshot_without_services_key_still_works(self):
        """Back-compat: a snapshot written by a server.py from before
        services existed must still render its links."""
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "radios": [
                        {"name": "primary", "radio_protocol": "Meshtastic",
                         "connected": True, "reconnecting": False},
                    ],
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        links = client.get("/api/status/links").get_json()["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["kind"], "link")

    def test_api_status_links_empty_when_no_snapshot(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = client.get("/api/status/links")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["links"], [])

    def test_api_status_links_requires_login(self):
        app = create_app()
        client = app.test_client()
        response = client.get("/api/status/links")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_settings_diagnostics_single_radio_no_bridge_banner(self):
        """No 'radios' array in the snapshot (old server.py, or single-radio) --
        must fall back to exactly one synthesized radio entry, no bridge-mode banner."""
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "interface_attached": True,
                    "interface_type": "SerialInterface",
                    "mesh_node_count": 7,
                    "local_node_id": "!snap1234",
                    "bbs_nodes": ["!sync1"],
                    "allowed_nodes": [],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertNotIn("Bridge mode active", page)
        self.assertIn("Interface type:</strong> SerialInterface", page)

    def test_accounts_settings_section_renders_with_defaults(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Account Linking", page)
        self.assertIn('value="10"', page)  # default link_code_ttl_minutes

    def test_accounts_settings_save_persists_to_config(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        response = self.post_with_csrf(
            client, "/settings",
            data={
                "settings_section": "accounts",
                "link_code_ttl_minutes": "15",
                "link_requests_per_hour": "2",
                "link_attempts_per_hour": "4",
                "max_linked_devices": "3",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("accounts", "link_code_ttl_minutes"), "15")
        self.assertEqual(config.get("accounts", "max_linked_devices"), "3")

    def test_create_app_initializes_schema_without_a_prior_initialize_database_call(self):
        """Regression test: bacon-web-admin.service is a separate process
        from mesh-bbs.service and must not depend on that other service
        having run schema migrations first -- a fresh DB file (or a window
        where mesh-bbs.service is down while the web admin stays up) must
        not 500 on /accounts. Deliberately does NOT call
        db_operations.initialize_database() itself, unlike every other test
        in this class -- create_app() alone must be sufficient."""
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        response = client.get("/accounts")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No accounts yet.", response.get_data(as_text=True))

    def test_accounts_list_page_shows_accounts(self):
        db_operations.initialize_database()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.set_account_alias(account_id, "BaconFan")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        page = client.get("/accounts").get_data(as_text=True)
        self.assertIn("BaconFan", page)
        self.assertIn(account_id, page)

    def test_accounts_list_page_empty_state(self):
        db_operations.initialize_database()
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        page = client.get("/accounts").get_data(as_text=True)
        self.assertIn("No accounts yet", page)

    def test_account_detail_shows_linked_devices(self):
        db_operations.initialize_database()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        page = client.get(f"/accounts/{account_id}").get_data(as_text=True)
        self.assertIn("!aaa11111", page)
        self.assertIn("7e18ca9d30a1", page)
        self.assertIn("meshtastic", page)
        self.assertIn("meshcore", page)

    def test_account_detail_unknown_account_redirects(self):
        db_operations.initialize_database()
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        response = client.get("/accounts/does-not-exist")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/accounts"))

    def test_account_detail_set_alias(self):
        db_operations.initialize_database()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        response = self.post_with_csrf(
            client, f"/accounts/{account_id}",
            data={"action": "set_alias", "alias": "NewAlias"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db_operations.get_account_alias(account_id), "NewAlias")

    def test_account_detail_unlink_device(self):
        db_operations.initialize_database()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        db_operations.link_node_to_account("7e18ca9d30a1", account_id, "meshcore")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        response = self.post_with_csrf(
            client, f"/accounts/{account_id}",
            data={"action": "unlink", "node_id": "7e18ca9d30a1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db_operations.get_account_id_for_node("7e18ca9d30a1"))

    def test_account_detail_unlink_last_device_refused(self):
        db_operations.initialize_database()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        self.post_with_csrf(
            client, f"/accounts/{account_id}",
            data={"action": "unlink", "node_id": "!aaa11111"},
            follow_redirects=False,
        )
        self.assertEqual(db_operations.get_account_id_for_node("!aaa11111"), account_id)

    def test_account_detail_force_link(self):
        db_operations.initialize_database()
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        response = self.post_with_csrf(
            client, f"/accounts/{account_id}",
            data={"action": "force_link", "new_node_id": "7e18ca9d30a1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db_operations.get_account_id_for_node("7e18ca9d30a1"), account_id)

    def test_account_detail_force_link_rejects_already_linked_node(self):
        db_operations.initialize_database()
        account_a = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_a, "meshtastic")
        account_b = db_operations.create_account()
        db_operations.link_node_to_account("!bbb22222", account_b, "meshtastic")

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        self.post_with_csrf(
            client, f"/accounts/{account_a}",
            data={"action": "force_link", "new_node_id": "!bbb22222"},
            follow_redirects=False,
        )
        # unchanged -- still linked to its original account
        self.assertEqual(db_operations.get_account_id_for_node("!bbb22222"), account_b)

    def test_devices_section_renders_with_defaults(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Device Configuration", page)
        self.assertIn("Primary Radio", page)
        self.assertIn("Secondary Radio", page)
        # setUp's config.ini has no [interface] section at all -- must not 500.
        self.assertIn('name="primary_type"', page)

    def test_devices_save_primary_serial(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "serial",
                "primary_port": "/dev/ttyUSB0",
                "primary_baudrate": "115200",
                "secondary_enabled": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/settings#devices"))

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("interface", "type"), "serial")
        self.assertEqual(config.get("interface", "port"), "/dev/ttyUSB0")

    def test_devices_save_primary_tcp_requires_hostname(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "tcp",
                "primary_hostname": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("hostname is required", page)

        # Nothing was written -- original config untouched.
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("interface"))

    def test_devices_save_enables_secondary_radio_bridge_mode(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "serial",
                "primary_port": "/dev/ttyUSB0",
                "secondary_enabled": "1",
                "secondary_type": "meshcore_tcp",
                "secondary_hostname": "192.168.1.50",
                "secondary_tcp_port": "5000",
                "bbs_nodes2": "7e18ca9d30a1",
                "allowed_nodes2": "7e18ca9d30a1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("interface2", "type"), "meshcore_tcp")
        self.assertEqual(config.get("interface2", "enabled"), "true")
        self.assertEqual(config.get("interface2", "hostname"), "192.168.1.50")
        self.assertEqual(config.get("sync2", "bbs_nodes"), "7e18ca9d30a1")
        self.assertEqual(config.get("allow_list2", "allowed_nodes"), "7e18ca9d30a1")

        # Round-trip: the settings page must now show it as enabled.
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn('id="secondary_enabled" name="secondary_enabled" value="1" checked', page)
        self.assertIn("192.168.1.50", page)

    def test_devices_secondary_rejects_bad_type(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "serial",
                "primary_port": "/dev/ttyUSB0",
                "secondary_enabled": "1",
                "secondary_type": "",
            },
            follow_redirects=True,
        )
        page = response.get_data(as_text=True)
        self.assertIn("choose a valid device type", page)
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("interface2"))

    def test_devices_rejects_duplicate_serial_port(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "serial",
                "primary_port": "/dev/ttyUSB0",
                "secondary_enabled": "1",
                "secondary_type": "meshcore_serial",
                "secondary_port": "/dev/ttyUSB0",
            },
            follow_redirects=True,
        )
        page = response.get_data(as_text=True)
        self.assertIn("cannot use the same serial port", page)
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("interface"))

    def test_devices_disabling_secondary_keeps_saved_settings(self):
        """Disabling the checkbox flips enabled=false but doesn't wipe the
        saved connection details, so re-enabling later doesn't need re-entry."""
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        self.post_with_csrf(
            client, "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "serial",
                "primary_port": "/dev/ttyUSB0",
                "secondary_enabled": "1",
                "secondary_type": "meshcore_tcp",
                "secondary_hostname": "192.168.1.50",
            },
        )
        self.post_with_csrf(
            client, "/settings",
            data={
                "settings_section": "devices",
                "primary_type": "serial",
                "primary_port": "/dev/ttyUSB0",
                "secondary_enabled": "",
                "secondary_type": "meshcore_tcp",
                "secondary_hostname": "192.168.1.50",
            },
        )

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("interface2", "enabled"), "false")
        self.assertEqual(config.get("interface2", "hostname"), "192.168.1.50")

    def test_links_card_lists_every_link_with_reconnect_buttons(self):
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "interface_attached": True,
                    "radios": [
                        {"name": "primary", "radio_protocol": "Meshtastic",
                         "connected": True, "reconnecting": False},
                        {"name": "mqtt1", "radio_protocol": "MQTT:mqtt1",
                         "connected": False, "reconnecting": True},
                    ],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Links &amp; Services", page)
        self.assertIn("MQTT:mqtt1", page)
        self.assertIn('value="reconnect_link"', page)
        self.assertIn('value="mqtt1"', page)
        self.assertIn('value="all"', page)

    def test_saving_mqtt_settings_requests_a_live_reload(self):
        """Saving must apply without a restart -- otherwise a newly added
        broker would sit in config.ini doing nothing until the service was
        restarted, which is exactly the confusing behavior this replaced."""
        reload_trigger = self.root / "reload_links.trigger"
        with mock.patch.dict(os.environ, {"BBS_LINKS_RELOAD_TRIGGER_PATH": str(reload_trigger)}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            response = self.post_with_csrf(
                client, "/settings",
                data={
                    "settings_section": "mqtt",
                    "mqtt_indexes": "2",
                    "mqtt_2_host": "broker2.example.com",
                    "mqtt_2_topic_prefix": "baconbs/site-b",
                },
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn("no restart needed", page)
            self.assertNotIn("Restart the mesh-bbs service", page)
            self.assertTrue(reload_trigger.exists())

    def test_reload_links_button_writes_trigger(self):
        reload_trigger = self.root / "reload_links.trigger"
        with mock.patch.dict(os.environ, {"BBS_LINKS_RELOAD_TRIGGER_PATH": str(reload_trigger)}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            response = self.post_with_csrf(
                client, "/settings",
                data={"settings_section": "reload_links"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/settings#links"))
            self.assertTrue(reload_trigger.exists())

    def test_reconnect_link_writes_trigger_with_link_name(self):
        trigger_path = self.root / "reconnect_link.trigger"
        with mock.patch.dict(os.environ, {"BBS_LINK_RECONNECT_TRIGGER_PATH": str(trigger_path)}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            response = self.post_with_csrf(
                client, "/settings",
                data={"settings_section": "reconnect_link", "link_name": "mqtt1"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/settings#links"))
            self.assertTrue(trigger_path.exists())
            self.assertEqual(trigger_path.read_text(encoding="utf-8").strip(), "mqtt1")

    def test_reconnect_all_links_writes_all_sentinel(self):
        trigger_path = self.root / "reconnect_link.trigger"
        with mock.patch.dict(os.environ, {"BBS_LINK_RECONNECT_TRIGGER_PATH": str(trigger_path)}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            self.post_with_csrf(
                client, "/settings",
                data={"settings_section": "reconnect_link", "link_name": "all"},
            )
            self.assertEqual(trigger_path.read_text(encoding="utf-8").strip(), "all")

    def test_reconnect_link_without_name_is_rejected(self):
        trigger_path = self.root / "reconnect_link.trigger"
        with mock.patch.dict(os.environ, {"BBS_LINK_RECONNECT_TRIGGER_PATH": str(trigger_path)}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            response = self.post_with_csrf(
                client, "/settings",
                data={"settings_section": "reconnect_link", "link_name": ""},
                follow_redirects=True,
            )
            self.assertIn("Pick a link to reconnect", response.get_data(as_text=True))
            self.assertFalse(trigger_path.exists())

    def test_mqtt_section_renders_with_defaults(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("MQTT Bridges", page)
        self.assertIn("Add Broker", page)
        # setUp's config.ini has no [mqttN] sections -- must not 500.
        self.assertIn('id="mqtt_indexes" value=""', page)

    def test_mqtt_save_adds_new_broker(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_enabled": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_port": "8883",
                "mqtt_1_tls": "1",
                "mqtt_1_username": "bacon",
                "mqtt_1_password": "hunter2",
                "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                "mqtt_1_local_id": "cityA-node",
                "mqtt_1_client_id": "",
                "mqtt_1_keepalive": "60",
                "mqtt_1_bbs_nodes": "mqtt:baconbs/cityA-cityB:cityB-node",
                "mqtt_1_allowed_nodes": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/settings#mqtt"))

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("mqtt1", "host"), "broker.example.com")
        self.assertEqual(config.get("mqtt1", "port"), "8883")
        self.assertEqual(config.get("mqtt1", "tls"), "true")
        self.assertEqual(config.get("mqtt1", "topic_prefix"), "baconbs/cityA-cityB")
        self.assertEqual(config.get("sync_mqtt1", "bbs_nodes"), "mqtt:baconbs/cityA-cityB:cityB-node")

        # Round-trip: the settings page must now show the saved broker.
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("broker.example.com", page)
        self.assertIn("baconbs/cityA-cityB", page)

    def test_mqtt_save_persists_advanced_tls_certificate_options(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                "mqtt_1_tls": "1",
                "mqtt_1_tls_ca_certs": "/etc/ssl/certs/my-ca.crt",
                "mqtt_1_tls_certfile": "/etc/baconbs/client.crt",
                "mqtt_1_tls_keyfile": "/etc/baconbs/client.key",
                "mqtt_1_tls_keyfile_password": "s3cret",
                "mqtt_1_tls_insecure": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("mqtt1", "tls_ca_certs"), "/etc/ssl/certs/my-ca.crt")
        self.assertEqual(config.get("mqtt1", "tls_certfile"), "/etc/baconbs/client.crt")
        self.assertEqual(config.get("mqtt1", "tls_keyfile"), "/etc/baconbs/client.key")
        self.assertEqual(config.get("mqtt1", "tls_keyfile_password"), "s3cret")
        self.assertEqual(config.get("mqtt1", "tls_insecure"), "true")

        # Round-trip: the saved values must come back in the form.
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Advanced TLS / Certificates", page)
        self.assertIn("/etc/ssl/certs/my-ca.crt", page)
        self.assertIn("/etc/baconbs/client.crt", page)

    def _pem_cert_bytes(self):
        """A structurally valid self-signed PEM certificate (generated once,
        checked in as a literal so the tests need no crypto dependency)."""
        return (
            b"-----BEGIN CERTIFICATE-----\n"
            b"MIIBITCByAIJAJ+Kx3aQ2n5cMAoGCCqGSM49BAMCMBUxEzARBgNVBAMMCnRlc3Qt\n"
            b"Y2EtMDEwHhcNMjUwMTAxMDAwMDAwWhcNMzUwMTAxMDAwMDAwWjAVMRMwEQYDVQQD\n"
            b"DAp0ZXN0LWNhLTAxMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEo3Gm6kFhFPBB\n"
            b"cNlG5nHhVfBvKQFmYQvVfKXbEfPRkzKqXWXBqPQnZQZLKKmXbLPQZ8YQVLnMqLQF\n"
            b"YQZKqLQFYTAKBggqhkjOPQQDAgNIADBFAiEA0000000000000000000000000000\n"
            b"00000000000AiA00000000000000000000000000000000000000000A==\n"
            b"-----END CERTIFICATE-----\n"
        )

    def test_mqtt_upload_rejects_key_uploaded_into_certificate_field(self):
        """The mistake that actually happens -- must be caught in the form,
        not surfaced as an opaque SSL error at connect time."""
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        key_pem = b"-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBg==\n-----END PRIVATE KEY-----\n"
        csrf_token = self.get_csrf_token(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": csrf_token,
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                "mqtt_1_tls_ca_certs_upload": (io.BytesIO(key_pem), "oops.key"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("PRIVATE KEY", page)

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("mqtt1"))

    def test_mqtt_upload_rejects_binary_der_file(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        csrf_token = self.get_csrf_token(client)
        response = client.post(
            "/settings",
            data={
                "csrf_token": csrf_token,
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                "mqtt_1_tls_ca_certs_upload": (io.BytesIO(b"\x30\x82\x01\xff\x00\xfe"), "ca.der"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("binary", response.get_data(as_text=True))

    def test_mqtt_upload_saves_file_and_points_config_at_it(self):
        cert_dir = os.path.join(self.root, "certs")
        with mock.patch.dict(os.environ, {"BBS_MQTT_CERT_DIR": cert_dir}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            csrf_token = self.get_csrf_token(client)
            response = client.post(
                "/settings",
                data={
                    "csrf_token": csrf_token,
                    "settings_section": "mqtt",
                    "mqtt_indexes": "1",
                    "mqtt_1_host": "broker.example.com",
                    "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                    "mqtt_1_tls_ca_certs_upload": (
                        io.BytesIO(self._pem_cert_bytes()), "my-ca.pem"),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)

            saved = os.path.join(cert_dir, "mqtt1", "ca.pem")
            self.assertTrue(os.path.isfile(saved), "uploaded CA was not written to disk")
            with open(saved, "r", encoding="utf-8") as handle:
                self.assertIn("BEGIN CERTIFICATE", handle.read())

            config = configparser.ConfigParser()
            config.read(self.config_path)
            self.assertEqual(config.get("mqtt1", "tls_ca_certs"), saved)

    def test_mqtt_upload_uses_fixed_name_ignoring_uploaded_filename(self):
        """A crafted filename must not influence the path written to."""
        cert_dir = os.path.join(self.root, "certs")
        with mock.patch.dict(os.environ, {"BBS_MQTT_CERT_DIR": cert_dir}):
            app = create_app()
            client = app.test_client()
            self.assertEqual(self.login(client).status_code, 302)

            csrf_token = self.get_csrf_token(client)
            client.post(
                "/settings",
                data={
                    "csrf_token": csrf_token,
                    "settings_section": "mqtt",
                    "mqtt_indexes": "1",
                    "mqtt_1_host": "broker.example.com",
                    "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                    "mqtt_1_tls_ca_certs_upload": (
                        io.BytesIO(self._pem_cert_bytes()), "../../../../etc/evil.pem"),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )
            self.assertTrue(os.path.isfile(os.path.join(cert_dir, "mqtt1", "ca.pem")))
            config = configparser.ConfigParser()
            config.read(self.config_path)
            self.assertEqual(
                config.get("mqtt1", "tls_ca_certs"),
                os.path.join(cert_dir, "mqtt1", "ca.pem"),
            )

    def test_mqtt_save_rejects_client_key_without_certificate(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
                "mqtt_1_tls_keyfile": "/etc/baconbs/client.key",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("client certificate", response.get_data(as_text=True))

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("mqtt1"))

    def test_mqtt_save_persists_publish_selection_and_prefix(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        self.post_with_csrf(
            client, "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "baconbs/cityA",
                "mqtt_1_publish_clients": "1",
                "mqtt_1_publish_telemetry": "1",
                "mqtt_1_publish_prefix": "homeassistant/baconbbs",
            },
        )

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("mqtt1", "publish_clients"), "true")
        self.assertEqual(config.get("mqtt1", "publish_telemetry"), "true")
        # Unchecked boxes must persist as false, not silently stay on.
        self.assertEqual(config.get("mqtt1", "publish_activity"), "false")
        self.assertEqual(config.get("mqtt1", "publish_status"), "false")
        self.assertEqual(config.get("mqtt1", "publish_prefix"), "homeassistant/baconbbs")

    def test_mqtt_save_normalizes_spaces_in_topic_fields(self):
        """Spaces break CLI tooling and broker ACL patterns -- config.ini
        must store the value that will actually be used as a topic."""
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        self.post_with_csrf(
            client, "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "bacon bbs/city A",
                "mqtt_1_local_id": "Burlington NNE",
                "mqtt_1_publish_prefix": "home assistant/bacon bbs",
            },
        )

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("mqtt1", "topic_prefix"), "bacon-bbs/city-A")
        self.assertEqual(config.get("mqtt1", "local_id"), "Burlington-NNE")
        self.assertEqual(config.get("mqtt1", "publish_prefix"), "home-assistant/bacon-bbs")

    def test_mqtt_save_requires_host_and_topic_prefix(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "",
                "mqtt_1_topic_prefix": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("host is required", page)
        self.assertIn("topic prefix is required", page)

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("mqtt1"))

    def test_mqtt_save_rejects_duplicate_topic_prefix(self):
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1,2",
                "mqtt_1_host": "broker-a.example.com",
                "mqtt_1_topic_prefix": "baconbs/shared",
                "mqtt_2_host": "broker-b.example.com",
                "mqtt_2_topic_prefix": "baconbs/shared",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("unique prefix", response.get_data(as_text=True))

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("mqtt1"))
        self.assertFalse(config.has_section("mqtt2"))

    def test_mqtt_save_removes_broker_dropped_from_indexes(self):
        """A broker whose row was removed client-side (so its index is no
        longer in mqtt_indexes) must be deleted from config.ini entirely --
        unlike [interface2]'s fixed slot, MQTT links are open-ended and
        user-managed, so 'removed in the form' means 'gone', not disabled."""
        app = create_app()
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)

        self.post_with_csrf(
            client, "/settings",
            data={
                "settings_section": "mqtt",
                "mqtt_indexes": "1",
                "mqtt_1_host": "broker.example.com",
                "mqtt_1_topic_prefix": "baconbs/cityA-cityB",
            },
        )
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertTrue(config.has_section("mqtt1"))

        self.post_with_csrf(
            client, "/settings",
            data={"settings_section": "mqtt", "mqtt_indexes": ""},
        )
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertFalse(config.has_section("mqtt1"))
        self.assertFalse(config.has_section("sync_mqtt1"))
        self.assertFalse(config.has_section("allow_list_mqtt1"))

    def test_settings_diagnostics_snapshot_fallback(self):
        with open(self.runtime_diag_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(
                {
                    "updated_at": "2026-03-30T01:02:03+00:00",
                    "interface_attached": True,
                    "interface_type": "SerialInterface",
                    "mesh_node_count": 7,
                    "local_node_id": "!snap1234",
                    "local_short_name": "SNAP",
                    "local_long_name": "Snapshot Node",
                    "bbs_nodes": ["!sync1", "!sync2"],
                    "allowed_nodes": ["!allow1"],
                    "error": "",
                },
                snapshot_file,
            )

        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("Runtime source:</strong> Snapshot file", page)
        self.assertIn("Interface type:</strong> SerialInterface", page)
        self.assertIn("Local short name:</strong> SNAP", page)
        self.assertIn("Configured sync peers:</strong> 2 (!sync1, !sync2)", page)

    def test_database_wipe_clears_application_tables(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS mail (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, sender_short_name TEXT, recipient TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS channel_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL, sender_short_name TEXT NOT NULL, date TEXT NOT NULL, content TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS zork_saves (user_id TEXT NOT NULL, game_id TEXT NOT NULL DEFAULT 'zork1', save_data BLOB NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, short_name TEXT NOT NULL DEFAULT '', long_name TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, messages_sent INTEGER NOT NULL DEFAULT 0, bio TEXT NOT NULL DEFAULT '')")
        conn.execute("CREATE TABLE IF NOT EXISTS game_scores (user_id TEXT NOT NULL, game_id TEXT NOT NULL, short_name TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0, max_score INTEGER NOT NULL DEFAULT 0, moves INTEGER NOT NULL DEFAULT 0, achieved_at TEXT NOT NULL, PRIMARY KEY (user_id, game_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS connection_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_time TEXT NOT NULL, sender_num TEXT, sender_node_id TEXT, sender_short_name TEXT, to_id TEXT, message_type TEXT NOT NULL, event_text TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS peer_sync_state (peer_node_id TEXT PRIMARY KEY, bulletins INTEGER NOT NULL DEFAULT 0, mail INTEGER NOT NULL DEFAULT 0, channels INTEGER NOT NULL DEFAULT 0, zork_saves INTEGER NOT NULL DEFAULT 0, profiles INTEGER NOT NULL DEFAULT 0, game_scores INTEGER NOT NULL DEFAULT 0, bulletins_hash TEXT NOT NULL DEFAULT '', mail_hash TEXT NOT NULL DEFAULT '', channels_hash TEXT NOT NULL DEFAULT '', zork_saves_hash TEXT NOT NULL DEFAULT '', profiles_hash TEXT NOT NULL DEFAULT '', game_scores_hash TEXT NOT NULL DEFAULT '', reported_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS deleted_sync_tombstones (tombstone_key TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)")
        conn.execute("INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only) VALUES ('General', 'CALL', '2026-03-30', 'Subj', 'Body', 'uid-b', 0)")
        conn.execute("INSERT INTO mail (sender, sender_short_name, recipient, date, subject, content, unique_id) VALUES ('1', 'CALL', '2', '2026-03-30', 'Mail', 'Body', 'uid-m')")
        conn.execute("INSERT INTO channels (name, url, local_only) VALUES ('Tech', 'mesh://tech', 0)")
        conn.execute("INSERT INTO user_profiles (user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio) VALUES ('1', 'CALL', 'Caller', '2026-03-30', '2026-03-30', 1, '')")
        conn.execute("INSERT INTO game_scores (user_id, game_id, short_name, score, max_score, moves, achieved_at) VALUES ('1', 'zork1', 'CALL', 10, 100, 5, '2026-03-30')")
        conn.execute("INSERT INTO connection_events (event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text) VALUES ('2026-03-30', '1', '!node', 'CALL', '2', 'mail', 'test')")
        conn.execute("INSERT INTO peer_sync_state (peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores, bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash, reported_at) VALUES ('!peer1', 1, 1, 1, 0, 1, 1, '', '', '', '', '', '', '2026-03-30')")
        conn.execute("INSERT INTO deleted_sync_tombstones (tombstone_key, deleted_at) VALUES ('bulletins:uid-b', '2026-03-30')")
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        wipe_response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "wipe_database",
                "wipe_confirmation": "WIPE DATABASE",
            },
            follow_redirects=True,
        )
        self.assertEqual(wipe_response.status_code, 200)
        self.assertIn("Local database wiped.", wipe_response.get_data(as_text=True))

        conn = sqlite3.connect(self.db_path)
        for table_name in [
            "bulletins",
            "mail",
            "channels",
            "channel_comments",
            "zork_saves",
            "user_profiles",
            "game_scores",
            "connection_events",
            "peer_sync_state",
            "deleted_sync_tombstones",
        ]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            self.assertEqual(count, 0, table_name)
        conn.close()

    def test_database_wipe_requires_exact_confirmation(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS bulletins (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, sender_short_name TEXT, date TEXT, subject TEXT, content TEXT, unique_id TEXT, local_only INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only) VALUES ('General', 'CALL', '2026-03-30', 'Subj', 'Body', 'uid-b', 0)")
        conn.commit()
        conn.close()

        app = create_app()
        client = app.test_client()
        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        wipe_response = self.post_with_csrf(
            client,
            "/settings",
            data={
                "settings_section": "wipe_database",
                "wipe_confirmation": "wipe database",
            },
            follow_redirects=True,
        )
        self.assertEqual(wipe_response.status_code, 200)
        self.assertIn("Database wipe cancelled.", wipe_response.get_data(as_text=True))

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM bulletins").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class ClientsTableWidthTests(unittest.TestCase):
    """The Clients table ran ~200 characters wide and scrolled off its card.

    The cause was lopsided rather than general: 318 of 319 rows had a
    9-character Meshtastic node id, and the single MeshCore row's 64-char
    public key set the width of the column for all of them.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "bulletins.db")
        os.environ["BBS_DB_PATH"] = self.db_path
        self.app = create_app()
        self.conn = sqlite3.connect(self.db_path)
        db_operations.thread_local.connection = self.conn
        db_operations.initialize_database()

    def tearDown(self):
        self.conn.close()
        if getattr(db_operations.thread_local, "connection", None) is not None:
            del db_operations.thread_local.connection
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass  # Windows holds the sqlite file briefly after close.

    LONG_ID = "78cb1cc70466915f74e30e7050162165d52cbb3f6574a1b2c3d4e5f60718293a"

    def _page(self):
        db_operations.upsert_mesh_clients([
            {"link_name": "primary", "node_id": "!04058ac8", "node_num": 67328712,
             "protocol": "Meshtastic", "short_name": "BACN", "long_name": "Bacon BBS",
             "hw_model": "HELTEC_V3", "role": "CLIENT", "battery_level": 84,
             "last_heard_epoch": None},
            {"link_name": "secondary", "node_id": self.LONG_ID, "node_num": None,
             "protocol": "MeshCore", "short_name": "DKSM",
             "long_name": "USM Auriga Solar", "hw_model": "HELTEC_VISION_MASTER_T190",
             "role": "ROUTER", "battery_level": None, "last_heard_epoch": None},
        ])
        self.conn.commit()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        return client.get("/clients").get_data(as_text=True)

    def test_paired_columns_are_merged(self):
        page = self._page()
        headers = re.findall(r"<th>([^<]*)</th>", page)[:6]
        self.assertEqual(headers, ["Device", "Name", "Hardware", "Role", "Battery", "Seen"])

    def test_long_node_id_is_truncated_on_screen(self):
        page = self._page()
        self.assertNotIn(self.LONG_ID + "<", page)  # never rendered as cell text
        self.assertIn("78cb1cc7…", page)

    def test_truncated_id_keeps_the_full_value_reachable(self):
        """The visible text no longer contains the whole key, so selecting
        it on screen would silently give you a shortened id."""
        page = self._page()
        self.assertIn(f'data-copy="{self.LONG_ID}"', page)
        self.assertIn(f'title="{self.LONG_ID} (click to copy)"', page)

    def test_short_node_ids_are_left_intact(self):
        """Truncating a 9-character id would cost readability for nothing;
        318 of 319 real rows are this shape."""
        page = self._page()
        self.assertIn(">!04058ac8<", page)

    def test_timestamps_render_relatively_with_exact_values_on_hover(self):
        page = self._page()
        self.assertRegex(page, r">\s*\d+[smhd] ago\s*<")
        self.assertRegex(page, r'title="Last seen \d{4}-\d{2}-\d{2} ')

    def test_empty_state_colspan_matches_the_new_column_count(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        page = client.get("/clients").get_data(as_text=True)
        self.assertIn('colspan="6"', page)


class TemplateFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["BBS_DB_PATH"] = os.path.join(self.temp_dir.name, "bulletins.db")
        self.app = create_app()

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def _render(self, template):
        with self.app.app_context():
            return self.app.jinja_env.from_string(template).render()

    def test_middle_ellipsis_keeps_both_ends(self):
        """The tail is what distinguishes two keys sharing a prefix, so a
        trailing ellipsis would discard the useful half."""
        out = self._render("{{ '" + "a" * 30 + "bcdefghi' | middle_ellipsis }}")
        self.assertTrue(out.startswith("aaaaaaaa"), out)
        self.assertTrue(out.endswith("bcdefghi"), out)
        self.assertIn("…", out)

    def test_middle_ellipsis_leaves_short_values_alone(self):
        self.assertEqual(self._render("{{ '!04058ac8' | middle_ellipsis }}"), "!04058ac8")

    def test_relative_age_buckets(self):
        from datetime import timedelta
        now = datetime.now()
        cases = [
            (now - timedelta(seconds=5), "s ago"),
            (now - timedelta(minutes=5), "m ago"),
            (now - timedelta(hours=5), "h ago"),
            (now - timedelta(days=5), "d ago"),
        ]
        for when, suffix in cases:
            stamp = when.strftime("%Y-%m-%d %H:%M:%S")
            with self.subTest(stamp=stamp):
                self.assertTrue(
                    self._render("{{ '" + stamp + "' | relative_age }}").endswith(suffix))

    def test_relative_age_passes_through_what_it_cannot_parse(self):
        """An empty cell would hide that a timestamp exists but is wrong."""
        self.assertEqual(self._render("{{ 'not a date' | relative_age }}"), "not a date")
        self.assertEqual(self._render("{{ '' | relative_age }}"), "")


class ResponsiveTableMarkupTests(unittest.TestCase):
    """Static checks on the table markup and stylesheet.

    These are string-level rather than request-level on purpose: the bug
    that made every table overflow its card was a class name in the
    templates ('table-scroll-container') that no CSS rule ever matched
    ('table-scroll'). Both files were individually fine, so nothing that
    rendered a page could catch it -- only comparing the two can.
    """

    REPO = Path(__file__).resolve().parent.parent

    def _templates(self):
        return sorted((self.REPO / "templates").glob("*.html"))

    def _css(self):
        return (self.REPO / "static" / "css" / "app.css").read_text(encoding="utf-8")

    def test_every_wrapper_class_used_in_a_template_exists_in_the_css(self):
        css = self._css()
        used = set()
        for path in self._templates():
            text = path.read_text(encoding="utf-8")
            used.update(re.findall(r'class="(table-scroll[\w-]*)"', text))
        self.assertTrue(used, "no table scroll wrappers found in templates")
        for cls in sorted(used):
            with self.subTest(css_class=cls):
                self.assertIn(f".{cls}", css,
                              f"templates use .{cls} but app.css defines no such rule")

    def test_the_scroll_wrapper_actually_scrolls_and_is_bounded(self):
        """max-width is what contains the overflow; overflow-x alone still
        lets a grid/flex child grow to its content's width."""
        css = self._css()
        rule = css[css.index(".table-scroll-container"):]
        rule = rule[:rule.index("}")]
        self.assertIn("overflow-x: auto", rule)
        self.assertIn("max-width: 100%", rule)

    def test_every_table_sits_inside_a_scroll_wrapper(self):
        """The convention is that the wrapper div directly encloses the
        table, so look immediately behind each <table> rather than tracking
        nesting depth -- these files mix Jinja and JS-built HTML strings,
        which no cheap tag counter parses correctly."""
        for path in self._templates():
            text = path.read_text(encoding="utf-8")
            # Jinja comments are not markup and can sit between the wrapper
            # and the table; drop them so they don't push the two apart.
            text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
            for match in re.finditer(r"<table[\s>]", text):
                preceding = text[max(0, match.start() - 200):match.start()]
                with self.subTest(template=path.name, line=text[:match.start()].count("\n") + 1):
                    self.assertIn(
                        "table-scroll", preceding,
                        f"{path.name} has a <table> with no scroll wrapper directly around it")

    def test_mobile_stacking_is_gated_on_labelled_tables(self):
        """A row of values with no column names is worse than sideways
        scrolling, so stacking must never apply to an unlabelled table."""
        css = self._css()
        block = css[css.index("@media (max-width: 760px)"):]
        block = block[:block.index("@media (max-width: 480px)")]
        self.assertIn("table[data-stacked]", block)
        # The old rule stacked every table unconditionally.
        self.assertNotRegex(block, r"(?m)^\s*table,\s*thead")

    def test_stacked_cells_can_wrap_long_unbreakable_values(self):
        """Node IDs and public keys have no break opportunity, and the
        value is an anonymous flex item that will not shrink without these."""
        css = self._css()
        block = css[css.index("@media (max-width: 760px)"):]
        block = block[:block.index("@media (max-width: 480px)")]
        cell = block[block.index("table[data-stacked] td {"):]
        cell = cell[:cell.index("}")]
        self.assertIn("min-width: 0", cell)
        self.assertIn("overflow-wrap: anywhere", cell)

    def test_labeller_is_exposed_for_rerendered_tables(self):
        js = (self.REPO / "static" / "js" / "data-table.js").read_text(encoding="utf-8")
        self.assertIn("window.BaconTables", js)
        self.assertIn("labelStacked", js)
        transmissions = (self.REPO / "templates" / "transmissions.html").read_text(encoding="utf-8")
        # Its tables are rebuilt from strings on a poll, so labels applied
        # at page load are destroyed on the first refresh.
        self.assertIn("BaconTables", transmissions)


if __name__ == "__main__":
    unittest.main()
