"""High scores name the player's account first, and their device beside it.

A score records a numeric sender id and whatever the device called itself
when the score was set. So the Baconfall board showed "Pers" for a player
whose linked account is "Materva" -- the name people know them by across every
device they own. The account now comes first, with the device name in
brackets, because the device is still how others on the mesh recognise them
and it tells one person's two devices apart.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations

RADIO_NUM = "3687019880"
RADIO_NODE = "dbc375683936c0803c30cd465acc15a1ac0f6d8f824652fe9554d066442b424f"


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def account(self, alias, sender_num=None):
        account_id = db_operations.create_account()
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE accounts SET alias = ?, alias_normalized = ?, sender_num = ?"
                     " WHERE account_id = ?",
                     (alias, db_operations.normalize_alias(alias), sender_num, account_id))
        conn.commit()
        return account_id

    def radio(self, node_id, node_num, account_id=None, link="primary"):
        conn = db_operations.get_db_connection()
        conn.execute("INSERT INTO mesh_clients (link_name, node_id, node_num, protocol,"
                     " short_name, first_seen, last_seen) VALUES (?, ?, ?, 'meshcore', 'x', ?, ?)",
                     (link, node_id, str(node_num), "2026-09-01", "2026-09-01"))
        if account_id:
            conn.execute("INSERT OR IGNORE INTO linked_nodes (node_id, account_id, network,"
                         " linked_at) VALUES (?, ?, 'meshcore', '2026-09-01')",
                         (node_id, account_id))
        conn.commit()

    def score(self, user_id, device, game="baconfall", points=100):
        db_operations.upsert_game_score(int(user_id), game, device, points, 0, 10)


class LookupTests(_Case):
    def test_a_radio_players_account_is_found_through_the_roster(self):
        """The live case: device "Pers", account "Materva"."""
        self.radio(RADIO_NODE, RADIO_NUM, self.account("Materva"))
        self.assertEqual(db_operations.get_score_account_names([RADIO_NUM]),
                         {RADIO_NUM: "Materva"})

    def test_an_ssh_players_account_is_found_by_sender_number(self):
        self.account("baconbot", sender_num=3781033626)
        self.assertEqual(db_operations.get_score_account_names(["3781033626"]),
                         {"3781033626": "baconbot"})

    def test_a_player_with_no_account_is_simply_absent(self):
        self.radio(RADIO_NODE, RADIO_NUM)  # heard, never linked
        self.assertEqual(db_operations.get_score_account_names([RADIO_NUM]), {})

    def test_an_account_with_no_alias_is_not_a_name(self):
        self.radio(RADIO_NODE, RADIO_NUM, self.account(""))
        self.assertEqual(db_operations.get_score_account_names([RADIO_NUM]), {})

    def test_a_device_heard_on_two_links_is_still_one_player(self):
        account_id = self.account("Materva")
        self.radio(RADIO_NODE, RADIO_NUM, account_id, link="primary")
        self.radio(RADIO_NODE, RADIO_NUM, account_id, link="mqtt1")
        self.assertEqual(db_operations.get_score_account_names([RADIO_NUM]),
                         {RADIO_NUM: "Materva"})

    def test_a_number_leading_to_two_accounts_is_credited_to_neither(self):
        """Node numbers can collide between MeshCore and Meshtastic in
        dual-radio mode. Guessing would put one person's score under another
        person's name."""
        self.radio("!aaaa0001", RADIO_NUM, self.account("Alice"))
        self.radio("bbbb0002" * 8, RADIO_NUM, self.account("Bob"), link="mqtt1")
        self.assertEqual(db_operations.get_score_account_names([RADIO_NUM]), {})

    def test_nothing_to_look_up_reads_nothing(self):
        with mock.patch.object(db_operations, "get_db_connection") as conn:
            self.assertEqual(db_operations.get_score_account_names([]), {})
        conn.assert_not_called()


class LabelTests(unittest.TestCase):
    def test_account_first_device_in_brackets(self):
        self.assertEqual(ch._score_player_label("Pers", "Materva"), "Materva (Pers)")

    def test_the_same_name_is_said_once(self):
        self.assertEqual(ch._score_player_label("baconbot", "baconbot"), "baconbot")

    def test_same_name_differing_only_in_case_is_said_once(self):
        self.assertEqual(ch._score_player_label("BaconBot", "baconbot"), "baconbot")

    def test_no_account_falls_back_to_the_device(self):
        self.assertEqual(ch._score_player_label("Pers", None), "Pers")

    def test_no_device_name_still_shows_the_account(self):
        self.assertEqual(ch._score_player_label("", "Materva"), "Materva")


class ScreenTests(_Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        patch = mock.patch.object(ch, "send_message",
                                  side_effect=lambda text, *a: self.sent.append(text))
        patch.start()
        self.addCleanup(patch.stop)
        games = mock.patch.object(ch, "handle_games_command")
        games.start()
        self.addCleanup(games.stop)
        self.radio(RADIO_NODE, RADIO_NUM, self.account("Materva"))
        self.score(RADIO_NUM, "Pers", points=500)
        self.score("67472072", "Stranger", points=100)

    def test_the_scoreboard_names_the_account_and_the_device(self):
        index = [g for g, _ in ch.GAME_LIST].index("baconfall") + 1
        ch.handle_scoreboard_steps(1234, str(index), None)
        board = "\n".join(self.sent)
        self.assertIn("1. Materva (Pers) 500", board)
        self.assertIn("2. Stranger 100", board)

    def test_the_hall_of_fame_names_the_account_and_the_device(self):
        ch.handle_hall_of_fame_command(1234, None)
        self.assertIn("Materva (Pers) 500", "\n".join(self.sent))

    def test_a_board_still_renders_rows_without_a_user_id(self):
        """Older four-value rows, as some callers and tests still produce."""
        with mock.patch.object(ch, "get_game_scoreboard", return_value=[("Ada", 100, 350, 12)]):
            ch.handle_scoreboard_steps(1234, "2", None)
        self.assertIn("1. Ada 100/350", "\n".join(self.sent))


class WebPageTests(_Case):
    def test_the_scores_page_shows_the_account_and_the_device(self):
        import os
        import tempfile
        import web_admin
        tmp = tempfile.mkdtemp()
        cfg = os.path.join(tmp, "config.ini")
        with open(cfg, "w") as handle:
            handle.write("[admin]\nusername=admin\npassword=test\n[interface]\ntype=none\n")
        db_path = os.path.join(tmp, "b.db")
        with mock.patch.dict(os.environ, {"BBS_DB_PATH": db_path, "BBS_CONFIG_PATH": cfg}):
            db_operations.thread_local.connection.close()
            del db_operations.thread_local.connection
            db_operations.initialize_database()
            self.radio(RADIO_NODE, RADIO_NUM, self.account("Materva"))
            self.score(RADIO_NUM, "Pers", points=500)
            app = web_admin.create_app()
            app.config["TESTING"] = True
            client = app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            page = client.get("/scores").data.decode("utf-8")
        self.assertIn("<strong>Materva</strong>", page)
        self.assertIn("Pers", page)


if __name__ == "__main__":
    unittest.main()
