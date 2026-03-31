import configparser
import json
import os
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
                "BBS_WEBGUI_SECRET": "test-secret",
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
        return client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )

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

    def test_admin_password_change_persists_across_app_restart(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        response = client.post(
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

        save_response = client.post(
            "/settings",
            data={
                "settings_section": "sync",
                "bbs_nodes": "!node1\n!node2\n!node1",
                "allowed_nodes": "!allow1, !allow2",
                "sync_interval_minutes": "5",
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
        self.assertEqual(config.get("allow_list", "allowed_nodes"), "!allow1,!allow2")

    def test_manual_sync_api_creates_trigger_file(self):
        app = create_app()
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        api_response = client.post("/api/sync/manual")
        self.assertEqual(api_response.status_code, 200)
        self.assertTrue(self.manual_trigger_path.exists())

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

    def test_settings_diagnostics_show_runtime_details(self):
        app = create_app(runtime_interface=FakeInterface())
        client = app.test_client()

        response = self.login(client)
        self.assertEqual(response.status_code, 302)

        settings_response = client.get("/settings")
        self.assertEqual(settings_response.status_code, 200)
        page = settings_response.get_data(as_text=True)
        self.assertIn("Interface attached:</strong> Yes", page)
        self.assertIn("Local short name:</strong> BBS", page)
        self.assertIn("Local long name:</strong> Bacon BBS", page)

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

        wipe_response = client.post(
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

        wipe_response = client.post(
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


if __name__ == "__main__":
    unittest.main()