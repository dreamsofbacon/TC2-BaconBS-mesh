import sqlite3
import unittest

import bbs_emulator
import db_operations
import ssh_auth
import utils


class SSHAuthTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()

    def tearDown(self):
        for token in list(bbs_emulator._sessions):
            bbs_emulator.end_session(token)
        connection = getattr(db_operations.thread_local, "connection", None)
        if connection is not None:
            connection.close()
            del db_operations.thread_local.connection

    def test_password_hashes_are_salted_and_verifiable(self):
        first_hash, first_salt = ssh_auth.hash_password("correct horse")
        second_hash, second_salt = ssh_auth.hash_password("correct horse")
        self.assertNotEqual(first_salt, second_salt)
        self.assertNotEqual(first_hash, second_hash)
        self.assertTrue(ssh_auth.verify_password(
            "correct horse", first_hash, first_salt))
        self.assertFalse(ssh_auth.verify_password(
            "wrong password", first_hash, first_salt))

    def test_registration_requires_explicit_new_prefix(self):
        self.assertIsNone(ssh_auth.authenticate(
            "NewUser", "long-enough-password", "192.0.2.1"))
        result = ssh_auth.authenticate(
            "new:NewUser", "long-enough-password", "192.0.2.1")
        self.assertIsNotNone(result)
        self.assertTrue(result.registered)

    def test_registration_rejects_mesh_roster_names(self):
        connection = db_operations.get_db_connection()
        connection.execute(
            """INSERT INTO mesh_clients
               (link_name, node_id, node_num, short_name, long_name,
                first_seen, last_seen) VALUES (?,?,?,?,?,?,?)""",
            ("primary", "!12345678", 1, "NOMAD", "Nomad Radio",
             "2026-01-01", "2026-01-01"),
        )
        connection.commit()
        self.assertIsNone(ssh_auth.authenticate(
            "new:nomad", "long-enough-password", "192.0.2.2"))
        self.assertIsNone(ssh_auth.authenticate(
            "new:Nomad-Radio", "long-enough-password", "192.0.2.2"))

    def test_registration_is_rate_limited_per_source(self):
        source = "192.0.2.3"
        for alias in ("new:One", "new:Two"):
            self.assertIsNotNone(ssh_auth.authenticate(
                alias, "long-enough-password", source,
                registration_limit_per_hour=2))
        self.assertIsNone(ssh_auth.authenticate(
            "new:Three", "long-enough-password", source,
            registration_limit_per_hour=2))

    def test_login_returns_same_server_owned_identity(self):
        registered = ssh_auth.authenticate(
            "new:Caller", "long-enough-password", "192.0.2.4")
        logged_in = ssh_auth.authenticate(
            "caller", "long-enough-password", "192.0.2.5")
        self.assertEqual(logged_in.account_id, registered.account_id)
        session = bbs_emulator.start_ssh_session(
            logged_in.account_id, logged_in.alias)
        self.assertEqual(
            session.sender_node_id, f"ssh:{registered.account_id}")
        self.assertEqual(utils.home_network(session.sender_node_id), "ssh")
        self.assertEqual(session.interface.max_text_bytes, 8192)
        self.assertEqual(session.interface.allowed_nodes, [])

    def test_radio_account_without_password_cannot_log_in(self):
        account_id = db_operations.create_account()
        db_operations.set_account_alias(account_id, "RadioOnly")
        self.assertIsNone(ssh_auth.authenticate(
            "RadioOnly", "long-enough-password", "192.0.2.6"))

    def test_invalid_account_id_cannot_become_an_ssh_identity(self):
        with self.assertRaises(ValueError):
            bbs_emulator.start_ssh_session("!victim", "Victim")

    def test_ssh_accounts_cannot_cross_read_mail(self):
        first = ssh_auth.authenticate(
            "new:FirstUser", "long-enough-password", "192.0.2.7")
        second = ssh_auth.authenticate(
            "new:SecondUser", "long-enough-password", "192.0.2.8")
        first_id = f"ssh:{first.account_id}"
        second_id = f"ssh:{second.account_id}"
        db_operations.link_node_to_account(
            "!first-radio", first.account_id, "meshtastic")
        db_operations.add_mail(
            second_id, "SecondUser", first_id, "Private", "SSH only",
            [], None)
        db_operations.add_mail(
            second_id, "SecondUser", "!first-radio", "Linked", "Same owner",
            [], None)

        first_subjects = {row[2] for row in db_operations.get_mail(first_id)}
        self.assertEqual(first_subjects, {"Private", "Linked"})
        self.assertEqual(db_operations.get_mail(second_id), [])


if __name__ == "__main__":
    unittest.main()