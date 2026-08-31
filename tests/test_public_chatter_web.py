import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import db_operations
import web_admin


class PublicChatterWebTests(unittest.TestCase):
    def setUp(self):
        connection = getattr(db_operations.thread_local, "connection", None)
        if connection is not None:
            connection.close()
            del db_operations.thread_local.connection
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "chatter.db")
        self.config_path = os.path.join(self.temp_dir.name, "config.ini")
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            config_file.write("[admin]\nusername = admin\npassword = test\n")
        self.environment = mock.patch.dict(os.environ, {
            "BBS_DB_PATH": self.db_path,
            "BBS_CONFIG_PATH": self.config_path,
        })
        self.environment.start()
        self.app = web_admin.create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        connection = getattr(db_operations.thread_local, "connection", None)
        if connection is not None:
            connection.close()
            del db_operations.thread_local.connection
        self.environment.stop()
        self.temp_dir.cleanup()

    def add_message(self):
        now = datetime.now(timezone.utc)
        db_operations.add_public_chatter(
            "pch:web", "meshtastic", 0, "LongFast", "!sender", "CALL",
            "<img src=x onerror=alert(1)>", now.isoformat().replace("+00:00", "Z"),
            now.isoformat().replace("+00:00", "Z"), "!capture",
            (now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            sync_received=True,
        )

    def test_page_and_api_are_public_and_window_is_clamped(self):
        self.add_message()
        page = self.client.get("/chatter")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Public Chatter", page.data)
        response = self.client.get("/api/public/chatter?hours=999&network=meshtastic")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["hours"], 168)
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["content"], "<img src=x onerror=alert(1)>")

    def test_invalid_channel_filter_is_rejected(self):
        response = self.client.get("/api/public/chatter?channel=LongFast")
        self.assertEqual(response.status_code, 400)

    def test_browser_renderer_uses_text_content(self):
        script_path = os.path.join(os.path.dirname(web_admin.__file__), "static", "js", "public-chatter.js")
        with open(script_path, "r", encoding="utf-8") as script_file:
            script = script_file.read()
        self.assertIn("element.textContent = text", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()