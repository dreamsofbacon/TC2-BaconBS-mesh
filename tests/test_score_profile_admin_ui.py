"""Deleting a score or profile from the web admin has to leave a tombstone.

The row disappearing is the easy half and the half that lies. A bare
`DELETE FROM game_scores` also makes the row disappear, and the page would
look right -- until the next reconcile pass, when the first peer that still
holds the record hands it straight back. That is how an exploited Trivia
score survived being deleted from both nodes.

So these tests assert the tombstone, not the empty table, and then push the
record back the way a peer would to confirm it stays out.
"""

import configparser
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db_operations
from web_admin import create_app


USER = "3758096387"
GAME = "trivia"


class ScoreProfileAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.ini"
        self.db_path = self.root / "bulletins.db"

        config = configparser.ConfigParser()
        config["admin"] = {"username": "admin", "password": "oldpass"}
        config["boards"] = {"bulletin_boards": "General"}
        with open(self.config_path, "w", encoding="utf-8") as handle:
            config.write(handle)

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BBS_CONFIG_PATH": str(self.config_path),
                "BBS_DB_PATH": str(self.db_path),
                "BBS_WEBGUI_SECRET": "test-secret",
                "BBS_VERSION_DISPLAY": "test-version",
            },
            clear=False,
        )
        self.env_patch.start()
        db_operations.initialize_database()
        self._seed()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin
        db_operations.remove_connection_log_handler()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _seed(self):
        db_operations.upsert_synced_game_score(
            USER, GAME, "baconbot", 2200, 3000, 12, "2026-09-03 20:24:24")
        db_operations.upsert_synced_user_profile(
            USER, "baconbot", "Bacon Bot", "2026-09-01 09:00:00",
            "2026-09-03 20:24:24", 5, "a bio")

    def _raw(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def _client(self):
        app = create_app()
        client = app.test_client()
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        client.post("/login", data={"username": "admin", "password": "oldpass",
                                    "csrf_token": token})
        return client

    def _anon_post(self, path, data):
        """Unauthenticated, but carrying a valid CSRF token.

        Posting with no token at all proves nothing about @login_required --
        CSRF rejects it first, so the test passes with the login gate
        removed. Tokens are handed out before login, so this is what an
        unauthenticated attempt actually looks like."""
        client = create_app().test_client()
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        form = dict(data)
        form["csrf_token"] = token
        return client.post(path, data=form, follow_redirects=False)

    def _post(self, client, path, data):
        token = client.get("/api/csrf-token").get_json()["csrf_token"]
        form = dict(data)
        form["csrf_token"] = token
        return client.post(path, data=form, follow_redirects=False)


class ScorePageTests(ScoreProfileAdminTests):
    def test_the_page_lists_the_score(self):
        page = self._client().get("/scores").get_data(as_text=True)
        self.assertIn("2200", page)
        self.assertIn("baconbot", page)
        self.assertIn(GAME, page)

    def test_the_page_offers_a_delete_for_it(self):
        page = self._client().get("/scores").get_data(as_text=True)
        self.assertIn('name="user_id" value="%s"' % USER, page)
        self.assertIn('name="game_id" value="%s"' % GAME, page)

    def test_deleting_removes_the_row(self):
        client = self._client()
        response = self._post(client, "/scores/delete",
                              {"user_id": USER, "game_id": GAME})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._raw("SELECT * FROM game_scores"), [])

    def test_deleting_leaves_a_tombstone(self):
        """The row going is what a raw DELETE also achieves. This is the part
        that stops a peer putting it back."""
        client = self._client()
        self._post(client, "/scores/delete", {"user_id": USER, "game_id": GAME})
        self.assertTrue(
            db_operations.has_sync_tombstone('game_scores', f"{USER}:{GAME}"))

    def test_the_deleted_score_cannot_be_pushed_back(self):
        """End to end, the way it actually failed: delete it from the page,
        then let a peer offer the same row again."""
        client = self._client()
        self._post(client, "/scores/delete", {"user_id": USER, "game_id": GAME})
        db_operations.upsert_synced_game_score(
            USER, GAME, "baconbot", 2200, 3000, 12, "2026-09-03T20:24:24")
        self.assertEqual(self._raw("SELECT * FROM game_scores"), [],
                         "the deleted score came back")

    def test_the_delete_is_restorable(self):
        client = self._client()
        self._post(client, "/scores/delete", {"user_id": USER, "game_id": GAME})
        page = client.get("/deleted").get_data(as_text=True)
        self.assertIn(f"game_scores:{USER}:{GAME}", page)
        self.assertTrue(
            db_operations.restore_sync_tombstone(f"game_scores:{USER}:{GAME}"))
        self.assertEqual(len(self._raw("SELECT * FROM game_scores")), 1)

    def test_a_missing_selection_is_refused_rather_than_guessed(self):
        """Passing the blanks through deletes no row -- nothing matches -- but
        still records a tombstone for the empty key, and a tombstone is not
        nothing: it is a permanent entry suppressing a record that may yet
        arrive. Checking only the row count misses that entirely."""
        client = self._client()
        self._post(client, "/scores/delete", {"user_id": "", "game_id": ""})
        self.assertEqual(len(self._raw("SELECT * FROM game_scores")), 1)
        self.assertEqual(
            self._raw("SELECT tombstone_key FROM deleted_sync_tombstones"), [],
            "a blank selection wrote a tombstone")

    def test_the_page_needs_a_login(self):
        client = create_app().test_client()
        self.assertEqual(client.get("/scores").status_code, 302)

    def test_the_delete_needs_a_login(self):
        response = self._anon_post("/scores/delete",
                                   {"user_id": USER, "game_id": GAME})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        self.assertEqual(len(self._raw("SELECT * FROM game_scores")), 1)
        self.assertEqual(
            self._raw("SELECT tombstone_key FROM deleted_sync_tombstones"), [])

    def test_the_delete_needs_a_csrf_token(self):
        client = self._client()
        client.post("/scores/delete", data={"user_id": USER, "game_id": GAME})
        self.assertEqual(len(self._raw("SELECT * FROM game_scores")), 1,
                         "a cross-site post deleted a score")

    def test_the_page_is_reachable_from_the_nav(self):
        page = self._client().get("/scores").get_data(as_text=True)
        self.assertIn('href="/scores"', page)


class ProfileDeleteTests(ScoreProfileAdminTests):
    def test_the_profile_page_offers_a_delete(self):
        page = self._client().get(f"/clients/{USER}/profile").get_data(as_text=True)
        self.assertIn(f"/clients/{USER}/profile/delete", page)

    def test_deleting_removes_the_row(self):
        client = self._client()
        response = self._post(client, f"/clients/{USER}/profile/delete", {})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._raw("SELECT * FROM user_profiles"), [])

    def test_deleting_leaves_a_tombstone(self):
        client = self._client()
        self._post(client, f"/clients/{USER}/profile/delete", {})
        self.assertTrue(db_operations.has_sync_tombstone('profiles', USER))

    def test_the_deleted_profile_cannot_be_pushed_back(self):
        client = self._client()
        self._post(client, f"/clients/{USER}/profile/delete", {})
        db_operations.upsert_synced_user_profile(
            USER, "baconbot", "Bacon Bot", "2026-09-01 09:00:00",
            "2026-09-03T20:24:24", 5, "a bio")
        self.assertEqual(self._raw("SELECT * FROM user_profiles"), [],
                         "the deleted profile came back")

    def test_a_profile_with_no_record_offers_no_delete(self):
        """The button would post a delete for a row that does not exist,
        writing a tombstone that suppresses the real profile when it
        eventually syncs in."""
        page = self._client().get("/clients/9999999/profile").get_data(as_text=True)
        self.assertNotIn("/clients/9999999/profile/delete", page)

    def test_the_delete_needs_a_login(self):
        response = self._anon_post(f"/clients/{USER}/profile/delete", {})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        self.assertEqual(len(self._raw("SELECT * FROM user_profiles")), 1)
        self.assertEqual(
            self._raw("SELECT tombstone_key FROM deleted_sync_tombstones"), [])

    def test_the_delete_needs_a_csrf_token(self):
        client = self._client()
        client.post(f"/clients/{USER}/profile/delete", data={})
        self.assertEqual(len(self._raw("SELECT * FROM user_profiles")), 1,
                         "a cross-site post deleted a profile")

    def test_deleting_a_profile_leaves_the_score_alone(self):
        """Separate scopes. Removing someone's name is not removing what they
        did, and a delete that quietly took both would be unrecoverable from
        one tombstone."""
        client = self._client()
        self._post(client, f"/clients/{USER}/profile/delete", {})
        self.assertEqual(len(self._raw("SELECT * FROM game_scores")), 1)


if __name__ == "__main__":
    unittest.main()
