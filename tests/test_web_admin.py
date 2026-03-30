import configparser
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from web_admin import create_app


class FakeInterface:
    def __init__(self):
        self.bbs_nodes = []
        self.allowed_nodes = []


class WebAdminSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.ini"
        self.db_path = self.root / "bulletins.db"

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
                "BBS_WEBGUI_SECRET": "test-secret",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self):
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
            },
            follow_redirects=True,
        )
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(runtime_interface.bbs_nodes, ["!node1", "!node2"])
        self.assertEqual(runtime_interface.allowed_nodes, ["!allow1", "!allow2"])

        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.assertEqual(config.get("sync", "bbs_nodes"), "!node1,!node2")
        self.assertEqual(config.get("allow_list", "allowed_nodes"), "!allow1,!allow2")


if __name__ == "__main__":
    unittest.main()