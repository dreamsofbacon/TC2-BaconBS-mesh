"""The web admin can delete several mail messages at once.

Reported 2026-09-15: "I have no method to delete mail" in the web admin, and
a wish to delete more than one message at a time. Each row did have a Delete
button, but in the last column of a table wide enough to push it off the
screen, and only one message per click.

Mail rows now carry a checkbox at the left, with select-all, and one
"Delete selected" action. Each message goes through delete_mail, exactly as
the single Delete does, so a tombstone stops sync putting it back.
"""
import os
from pathlib import Path
from unittest import mock

import db_operations
from web_admin import create_app
from test_web_admin import _WebAdminHarness


class MailBulkDeleteTests(_WebAdminHarness):
    def setUp(self):
        super().setUp()
        self.trigger_path = Path(self.temp_dir.name) / "manual_sync.trigger"
        self.extra_env = mock.patch.dict(
            os.environ, {"BBS_MANUAL_SYNC_TRIGGER_PATH": str(self.trigger_path)}, clear=False)
        self.extra_env.start()
        db_operations.initialize_database()
        self.client = create_app().test_client()
        self.login(self.client)

    def tearDown(self):
        # Before the harness's own patch is stopped: stopping this one
        # afterwards would restore the environment it snapshotted, which
        # still held the harness's temp paths, and leak them into every
        # later test.
        self.extra_env.stop()
        super().tearDown()

    def mail(self, subject):
        unique_id = db_operations.add_mail("!11112222", "Pers", "!abcd1234", subject, "Body", [], None)
        if unique_id is None:
            unique_id = db_operations.get_db_connection().execute(
                "SELECT unique_id FROM mail WHERE subject = ?", (subject,)).fetchone()[0]
        row_id = db_operations.get_db_connection().execute(
            "SELECT id FROM mail WHERE subject = ?", (subject,)).fetchone()[0]
        return row_id, unique_id

    def subjects(self):
        return sorted(r[0] for r in db_operations.get_db_connection().execute(
            "SELECT subject FROM mail").fetchall())

    def delete(self, row_ids, **extra):
        return self.post_with_csrf(self.client, "/mail/delete-selected",
                                   data={"row_ids": [str(r) for r in row_ids], **extra})

    def test_the_mail_page_offers_selection(self):
        self.mail("One")
        page = self.client.get("/mail").get_data(as_text=True)
        self.assertIn('id="bulk-delete-form"', page)
        self.assertIn("data-bulk-all", page)
        self.assertEqual(page.count("data-bulk-row"), 1)
        self.assertIn("js/bulk-delete.js", page)

    def test_other_tables_do_not(self):
        page = self.client.get("/bulletins").get_data(as_text=True)
        self.assertNotIn("bulk-delete-form", page)
        self.assertNotIn("data-bulk-row", page)

    def test_selected_messages_are_deleted_and_the_rest_kept(self):
        one, one_uid = self.mail("One")
        two, two_uid = self.mail("Two")
        self.mail("Three")
        response = self.delete([one, two])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.subjects(), ["Three"])
        self.assertTrue(db_operations.has_sync_tombstone("mail", one_uid))
        self.assertTrue(db_operations.has_sync_tombstone("mail", two_uid))
        self.assertTrue(self.trigger_path.exists(), "no sync requested after deleting mail")
        flashed = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Deleted 2 messages.", flashed)

    def test_nothing_selected_deletes_nothing(self):
        self.mail("One")
        response = self.delete([])
        self.assertEqual(self.subjects(), ["One"])
        self.assertIn("Select at least one message",
                      self.client.get(response.headers["Location"]).get_data(as_text=True))

    def test_junk_ids_are_ignored(self):
        one, _ = self.mail("One")
        self.mail("Two")
        self.post_with_csrf(self.client, "/mail/delete-selected",
                            data={"row_ids": ["abc", "", str(one), "99999"]})
        self.assertEqual(self.subjects(), ["Two"])

    def test_the_search_is_kept_after_deleting(self):
        one, _ = self.mail("One")
        response = self.delete([one], q="One")
        self.assertIn("q=One", response.headers["Location"])

    def test_only_mail_allows_it(self):
        db_operations.add_bulletin("General", "AAA", "s", "c", [], None)
        row_id = db_operations.get_db_connection().execute("SELECT id FROM bulletins").fetchone()[0]
        self.post_with_csrf(self.client, "/bulletins/delete-selected", data={"row_ids": [str(row_id)]})
        self.assertEqual(db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM bulletins").fetchone()[0], 1)

    def test_it_needs_a_login_and_a_csrf_token(self):
        one, _ = self.mail("One")
        self.client.post("/mail/delete-selected", data={"row_ids": [str(one)]})
        self.assertEqual(self.subjects(), ["One"], "deleted without a CSRF token")
        anonymous = create_app().test_client()
        token = anonymous.get("/api/csrf-token").get_json()["csrf_token"]
        anonymous.post("/mail/delete-selected", data={"row_ids": [str(one)], "csrf_token": token})
        self.assertEqual(self.subjects(), ["One"], "deleted without logging in")
