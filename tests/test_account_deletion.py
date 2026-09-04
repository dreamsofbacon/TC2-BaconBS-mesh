"""Deleting an account has to stay deleted, and stay inside its namespace.

Two failure modes drive these tests. The first is resurrection: mail rows
sync between BBS nodes, so a bare DELETE is undone by the next reconcile
pass -- which is why the handoff warned against removing account rows by
hand. The second is over-reach: a node id is the only thing mail
authorization checks, so an account holding a real radio's id must not be
dissolvable from an admin page.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db_operations


class AccountDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "bulletins.db"
        self.env_patch = mock.patch.dict(
            os.environ, {"BBS_DB_PATH": str(self.db_path)}, clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._close_connection)

    def _close_connection(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def _ssh_account(self, alias="tester"):
        account_id = db_operations.create_ssh_account(alias, "deadbeef", "cafe")
        self.assertIsNotNone(account_id, "the fixture account should have been created")
        return account_id

    def _mesh_account(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        return account_id

    def _add_mail(self, recipient, unique_id, sender="!sender01"):
        conn = db_operations.get_db_connection()
        conn.execute(
            """INSERT INTO mail (sender, sender_short_name, recipient, date,
                                 subject, content, unique_id)
               VALUES (?, 'SND', ?, '2026-09-03 10:00:00', 'subject', 'body', ?)""",
            (sender, recipient, unique_id))
        conn.commit()

    def _rows(self, sql, params=()):
        return db_operations.get_db_connection().execute(sql, params).fetchall()

    # -- the namespace rule ----------------------------------------------

    def test_an_account_holding_a_mesh_device_is_refused(self):
        account_id = self._mesh_account()
        blockers = db_operations.account_deletion_blockers(account_id)
        self.assertTrue(blockers)
        self.assertIn("!aaa11111", blockers[0])
        self.assertIsNone(db_operations.delete_account(account_id))
        self.assertIsNotNone(db_operations.get_account(account_id))

    def test_an_ssh_account_that_later_linked_a_radio_is_refused(self):
        account_id = self._ssh_account()
        db_operations.link_node_to_account("!bbb22222", account_id, "meshtastic")
        self.assertTrue(db_operations.account_deletion_blockers(account_id))
        self.assertIsNone(db_operations.delete_account(account_id))

    def test_a_plain_ssh_account_is_deletable(self):
        account_id = self._ssh_account()
        self.assertEqual(db_operations.account_deletion_blockers(account_id), [])
        self.assertIsNotNone(db_operations.delete_account(account_id))

    def test_an_unknown_account_is_refused_rather_than_silently_accepted(self):
        self.assertTrue(db_operations.account_deletion_blockers("no-such-account"))
        self.assertIsNone(db_operations.delete_account("no-such-account"))
        self.assertTrue(db_operations.account_deletion_blockers(""))

    # -- what actually goes ----------------------------------------------

    def test_the_login_and_its_identity_are_gone(self):
        account_id = self._ssh_account("gonesoon")
        db_operations.delete_account(account_id)
        self.assertIsNone(db_operations.get_account(account_id))
        self.assertIsNone(db_operations.get_ssh_credentials("gonesoon"))
        self.assertEqual(
            db_operations.get_account_id_for_node(f"ssh:{account_id}"), None)

    def test_the_alias_frees_up_but_the_identity_does_not_come_back(self):
        first = self._ssh_account("reused")
        db_operations.delete_account(first)
        second = db_operations.create_ssh_account("reused", "beef", "f00d")
        self.assertIsNotNone(second, "the alias should be available again")
        # The point: same name, different account id, so a new registration
        # never inherits the old identity's mail scope.
        self.assertNotEqual(second, first)

    def test_mail_addressed_to_the_account_is_deleted(self):
        account_id = self._ssh_account()
        self._add_mail(f"ssh:{account_id}", "mail-in-1")
        self._add_mail(f"ssh:{account_id}", "mail-in-2")
        removed = db_operations.delete_account(account_id)
        self.assertEqual(removed["mail"], 2)
        self.assertEqual(self._rows("SELECT unique_id FROM mail"), [])

    def test_deleted_mail_is_tombstoned_so_a_peer_cannot_send_it_back(self):
        account_id = self._ssh_account()
        self._add_mail(f"ssh:{account_id}", "mail-in-1")
        db_operations.delete_account(account_id)
        # This is the whole reason delete_account routes through delete_mail
        # instead of issuing one DELETE.
        self.assertTrue(db_operations.has_sync_tombstone("mail", "mail-in-1"))

    def test_mail_the_account_sent_to_others_is_left_alone(self):
        account_id = self._ssh_account()
        self._add_mail("!someoneelse", "their-copy", sender=f"ssh:{account_id}")
        db_operations.delete_account(account_id)
        # It is in somebody else's mailbox. Deleting the sender's account is
        # not consent to reach into it.
        self.assertEqual(
            self._rows("SELECT unique_id FROM mail"), [("their-copy",)])

    def test_pending_relay_deliveries_are_cleared(self):
        account_id = self._ssh_account()
        conn = db_operations.get_db_connection()
        conn.execute(
            """INSERT INTO mail_dm_deliveries
               (mail_unique_id, recipient_account_id, target_node_id, created_at)
               VALUES ('m1', ?, ?, '2026-09-03 10:00:00')""",
            (account_id, f"ssh:{account_id}"))
        conn.commit()
        db_operations.delete_account(account_id)
        self.assertEqual(self._rows("SELECT id FROM mail_dm_deliveries"), [])

    def test_outstanding_link_codes_are_revoked(self):
        account_id = self._ssh_account()
        conn = db_operations.get_db_connection()
        conn.execute(
            """INSERT INTO link_codes
               (code, account_id, requested_by_node_id, created_at, expires_at)
               VALUES ('123456', ?, 'ssh:x', '2026-09-03 10:00:00', '2036-01-01 00:00:00')""",
            (account_id,))
        conn.commit()
        db_operations.delete_account(account_id)
        self.assertEqual(self._rows("SELECT code FROM link_codes"), [])

    # -- the relay directory ---------------------------------------------

    def test_the_relay_preference_is_retired_rather_than_removed(self):
        account_id = self._ssh_account("relayer")
        db_operations.set_account_mail_relay(account_id, True)
        node_id = f"ssh:{account_id}"
        self.assertIn(node_id, [entry["recipient_node_id"]
                                for entry in db_operations.get_mail_relay_directory()])

        db_operations.delete_account(account_id)

        # Kept, and disabled. get_mail_relay_directory lists any node id with
        # enabled = 1 whether or not an account backs it, so a deleted row
        # would let a stale RELAYPREF from a peer re-advertise a dead
        # mailbox. A disabled row carrying a fresh timestamp cannot.
        rows = self._rows(
            "SELECT enabled FROM mail_relay_preferences WHERE node_id = ?", (node_id,))
        self.assertEqual(rows, [(0,)])
        self.assertNotIn(node_id, [entry["recipient_node_id"]
                                   for entry in db_operations.get_mail_relay_directory()])

    def test_a_stale_peer_preference_cannot_re_advertise_the_dead_mailbox(self):
        account_id = self._ssh_account("relayer")
        db_operations.set_account_mail_relay(account_id, True)
        node_id = f"ssh:{account_id}"
        db_operations.delete_account(account_id)

        applied = db_operations.apply_synced_mail_relay_preference(
            node_id, True, "2020-01-01T00:00:00+00:00")
        self.assertFalse(applied, "an older timestamp must lose")
        self.assertNotIn(node_id, [entry["recipient_node_id"]
                                   for entry in db_operations.get_mail_relay_directory()])

    # -- what is deliberately kept ---------------------------------------

    def test_the_audit_trail_survives_the_account(self):
        account_id = self._ssh_account()
        db_operations.record_link_attempt("ssh-ip:203.0.113.9", "ssh_register", True)
        db_operations.delete_account(account_id)
        # Erasing an account's history at the moment you delete it defeats
        # the point of keeping one.
        self.assertTrue(self._rows("SELECT id FROM link_attempts"))

    def test_other_accounts_are_untouched(self):
        doomed = self._ssh_account("doomed")
        keeper = self._ssh_account("keeper")
        self._add_mail(f"ssh:{keeper}", "keepers-mail")
        db_operations.delete_account(doomed)
        self.assertIsNotNone(db_operations.get_account(keeper))
        self.assertEqual(
            self._rows("SELECT unique_id FROM mail"), [("keepers-mail",)])


class AccountDeletionWebTests(unittest.TestCase):
    """The page has to require a deliberate act, not just a click."""

    def setUp(self):
        import configparser

        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp_dir.name)
        self.config_path = root / "config.ini"
        config = configparser.ConfigParser()
        config["admin"] = {"username": "admin", "password": "oldpass"}
        config["boards"] = {"bulletin_boards": "General"}
        with open(self.config_path, "w", encoding="utf-8") as handle:
            config.write(handle)

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "BBS_CONFIG_PATH": str(self.config_path),
                "BBS_DB_PATH": str(root / "bulletins.db"),
                "BBS_WEBGUI_SECRET": "test-secret",
                "BBS_VERSION_DISPLAY": "test-version",
            },
            clear=False,
        )
        self.env_patch.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._close_connection)

        from web_admin import create_app

        self.client = create_app().test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    def _close_connection(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def _csrf(self):
        return self.client.get("/api/csrf-token").get_json()["csrf_token"]

    def _post_delete(self, account_id, confirm):
        return self.client.post(
            f"/accounts/{account_id}",
            data={
                "csrf_token": self._csrf(),
                "action": "delete_account",
                "confirm_alias": confirm,
            },
            follow_redirects=False,
        )

    def test_the_wrong_confirmation_deletes_nothing(self):
        account_id = db_operations.create_ssh_account("keepme", "hash", "salt")
        self._post_delete(account_id, "something else")
        self.assertIsNotNone(db_operations.get_account(account_id))

    def test_an_empty_confirmation_deletes_nothing(self):
        account_id = db_operations.create_ssh_account("keepme", "hash", "salt")
        self._post_delete(account_id, "")
        self.assertIsNotNone(db_operations.get_account(account_id))

    def test_typing_the_alias_deletes_the_account(self):
        account_id = db_operations.create_ssh_account("byebye", "hash", "salt")
        response = self._post_delete(account_id, "byebye")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db_operations.get_account(account_id))

    def test_a_mesh_account_is_refused_even_with_the_right_confirmation(self):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, "meshuser")
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        self._post_delete(account_id, "meshuser")
        self.assertIsNotNone(db_operations.get_account(account_id))

    def test_the_page_explains_why_a_mesh_account_cannot_be_deleted(self):
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!aaa11111", account_id, "meshtastic")
        page = self.client.get(f"/accounts/{account_id}").get_data(as_text=True)
        self.assertIn("!aaa11111", page)
        self.assertNotIn('value="delete_account"', page)

    def test_the_page_offers_deletion_for_an_ssh_account(self):
        account_id = db_operations.create_ssh_account("deletable", "hash", "salt")
        page = self.client.get(f"/accounts/{account_id}").get_data(as_text=True)
        self.assertIn('value="delete_account"', page)

    def test_deletion_needs_a_login(self):
        account_id = db_operations.create_ssh_account("byebye", "hash", "salt")
        self.client.get("/logout")
        self.client.post(
            f"/accounts/{account_id}",
            data={"action": "delete_account", "confirm_alias": "byebye"})
        self.assertIsNotNone(db_operations.get_account(account_id))

    def test_deletion_needs_the_csrf_token(self):
        account_id = db_operations.create_ssh_account("byebye", "hash", "salt")
        self.client.post(
            f"/accounts/{account_id}",
            data={"action": "delete_account", "confirm_alias": "byebye"})
        self.assertIsNotNone(db_operations.get_account(account_id))


if __name__ == "__main__":
    unittest.main()
