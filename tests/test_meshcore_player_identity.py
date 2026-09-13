"""MeshCore players are identified by the 12-hex prefix of their key.

The old id was int(key[:8], 16). Two MeshCore players whose keys shared 8 hex
characters -- or a MeshCore player whose number equalled a Meshtastic node --
were merged into one score row, one save slot and one profile.

The fix has three parts, and each is pinned here:

  identity   player_identity turns a key into a runtime number and a stored
             "mc-<prefix>" key. The prefix is what MeshCore sends with every
             message, so a player is the same person whether or not they are
             already in the radio's contact list.
  storage    every player-keyed database function files by that key, so the
             games and menus did not have to change.
  migration  records already under the old number move to the new key, with
             tombstones so peers drop the old copies and sync cannot bring
             them back.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import player_identity as pid

# materva's real key, and a second key sharing its first 8 hex characters.
KEY = "dbc375683936c0803c30cd465acc15a1ac0f6d8f824652fe9554d066442b424f"
TWIN = "dbc37568ffff" + "0" * 52
LEGACY = str(int(KEY[:8], 16))            # 3687019880, the old player id
NEW = "mc-dbc375683936"


class _Db(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    @property
    def conn(self):
        return db_operations.get_db_connection()

    def roster(self, node_id, protocol="MeshCore", node_num="1"):
        self.conn.execute(
            "INSERT INTO mesh_clients (link_name, node_id, node_num, protocol, first_seen,"
            " last_seen) VALUES ('primary', ?, ?, ?, '2026-09-01', '2026-09-01')",
            (node_id, str(node_num), protocol))
        self.conn.commit()

    def rows(self, table, user_id):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
                                 (user_id,)).fetchone()[0]


class IdentityTests(unittest.TestCase):
    def test_the_stored_key_is_the_12_hex_prefix(self):
        self.assertEqual(pid.meshcore_player_key(KEY), NEW)

    def test_a_contact_and_a_non_contact_are_the_same_person(self):
        """MeshCore sends a 12-hex prefix; the full key is only known for
        existing contacts. Both must land on one identity."""
        self.assertEqual(pid.meshcore_player_number(KEY[:12]),
                         pid.meshcore_player_number(KEY))

    def test_a_leading_bang_does_not_change_who_it_is(self):
        self.assertEqual(pid.meshcore_player_number("!" + KEY),
                         pid.meshcore_player_number(KEY))

    def test_keys_sharing_8_hex_characters_are_different_people(self):
        """The collision this whole change exists to fix."""
        self.assertEqual(int(KEY[:8], 16), int(TWIN[:8], 16))
        self.assertNotEqual(pid.meshcore_player_number(KEY), pid.meshcore_player_number(TWIN))
        self.assertNotEqual(pid.meshcore_player_key(KEY), pid.meshcore_player_key(TWIN))

    def test_a_meshcore_number_can_never_equal_any_32_bit_number(self):
        """Meshtastic nodes, SSH senders, emulator sessions and broadcast
        addresses are all 32-bit. The smallest possible key cannot reach."""
        for key in (KEY, "000000000000", "ffffffffffff"):
            with self.subTest(key=key):
                self.assertGreater(pid.meshcore_player_number(key), 0xFFFFFFFF)

    def test_the_runtime_number_and_stored_key_convert_exactly(self):
        self.assertEqual(pid.player_key(pid.meshcore_player_number(KEY)), NEW)
        self.assertEqual(pid.player_key(str(pid.meshcore_player_number(KEY))), NEW)

    def test_other_players_keep_the_number_they_always_had(self):
        for user_id in (67472072, "67472072", 3781033626, 0xE0000001):
            with self.subTest(user_id=user_id):
                self.assertEqual(pid.player_key(user_id), str(int(user_id)))

    def test_an_existing_mc_key_passes_through(self):
        self.assertEqual(pid.player_key(NEW), NEW)
        self.assertEqual(pid.player_key(NEW.upper()), NEW)

    def test_the_legacy_number_is_what_old_records_are_filed_under(self):
        self.assertEqual(pid.legacy_meshcore_number(KEY), LEGACY)

    def test_the_key_survives_a_tombstone_key_being_split(self):
        """Tombstone and sync keys are "user_id:game_id", split on the first
        colon -- by this code and by peers on older code. The separator is
        "-" so the split still yields the whole player id."""
        user_id, game_id = f"{NEW}:baconfall".split(":", 1)
        self.assertEqual((user_id, game_id), (NEW, "baconfall"))

    def test_the_radio_layer_uses_it(self):
        import meshcore_interface
        self.assertEqual(meshcore_interface._node_num(KEY), pid.meshcore_player_number(KEY))


class StorageTests(_Db):
    """Callers pass the runtime number; storage files it by the key."""

    def test_a_meshcore_score_is_filed_under_the_mc_key(self):
        number = pid.meshcore_player_number(KEY)
        db_operations.upsert_game_score(number, "trivia", "Pers", 500, 0, 9)
        self.assertEqual(self.rows("game_scores", NEW), 1)
        self.assertEqual(self.rows("game_scores", str(number)), 0)
        self.assertEqual(db_operations.get_user_game_scores(number)[0][1], 500)

    def test_two_players_sharing_8_hex_no_longer_share_a_score_row(self):
        db_operations.upsert_game_score(pid.meshcore_player_number(KEY), "trivia", "a", 500, 0, 9)
        db_operations.upsert_game_score(pid.meshcore_player_number(TWIN), "trivia", "b", 100, 0, 9)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM game_scores WHERE game_id='trivia'").fetchone()[0], 2)

    def test_profile_and_save_follow_the_same_key(self):
        number = pid.meshcore_player_number(KEY)
        db_operations.auto_upsert_user_profile(number, "Pers", "Perseid")
        db_operations.upsert_zork_save(number, b"state", "zork1")
        self.assertEqual(self.rows("user_profiles", NEW), 1)
        self.assertEqual(db_operations.get_zork_save(number, "zork1"), b"state")

    def test_a_meshtastic_players_records_are_unchanged(self):
        db_operations.upsert_game_score(67472072, "trivia", "bacon", 10, 0, 1)
        self.assertEqual(self.rows("game_scores", "67472072"), 1)

    def test_baconfall_runs_use_the_same_key(self):
        import baconfall_port
        baconfall_port.play(pid.meshcore_player_number(KEY))
        self.assertEqual(self.rows("baconfall_runs", NEW), 1)


class MigrationTests(_Db):
    def setUp(self):
        super().setUp()
        self.roster(KEY)

    def seed_legacy(self, score=500, bio="radios", help_tips=1):
        db_operations.upsert_synced_game_score(LEGACY, "baconfall", "Pers", score, 0, 9,
                                               "2026-09-01 10:00:00")
        db_operations.upsert_synced_zork_save(LEGACY, "zork1", b"old-save", "2026-09-01T10:00:00")
        db_operations.upsert_synced_user_profile(LEGACY, "Pers", "Perseid", "2026-08-01",
                                                 "2026-09-01 10:00:00", 7, bio)
        self.conn.execute("UPDATE user_profiles SET help_tips = ? WHERE user_id = ?",
                          (help_tips, LEGACY))
        self.conn.commit()

    def test_scores_saves_and_profile_move_to_the_new_key(self):
        self.seed_legacy()
        summary = db_operations.migrate_meshcore_player_ids()
        self.assertEqual(summary["players"], 1)
        for table in ("game_scores", "zork_saves", "user_profiles"):
            with self.subTest(table=table):
                self.assertEqual(self.rows(table, NEW), 1)
                self.assertEqual(self.rows(table, LEGACY), 0)

    def test_the_old_records_are_tombstoned(self):
        self.seed_legacy()
        db_operations.migrate_meshcore_player_ids()
        self.assertTrue(db_operations.has_sync_tombstone("game_scores", f"{LEGACY}:baconfall"))
        self.assertTrue(db_operations.has_sync_tombstone("zork_saves", f"{LEGACY}:zork1"))
        self.assertTrue(db_operations.has_sync_tombstone("profiles", LEGACY))

    def test_a_peer_resending_the_old_copy_cannot_bring_it_back(self):
        """What the tombstones are for."""
        self.seed_legacy()
        db_operations.migrate_meshcore_player_ids()
        db_operations.upsert_synced_game_score(LEGACY, "baconfall", "Pers", 500, 0, 9,
                                               "2026-09-01 10:00:00")
        db_operations.upsert_synced_user_profile(LEGACY, "Pers", "Perseid", "2026-08-01",
                                                 "2026-09-01 10:00:00", 7, "radios")
        self.assertEqual(self.rows("game_scores", LEGACY), 0)
        self.assertEqual(self.rows("user_profiles", LEGACY), 0)

    def test_the_better_score_wins_when_the_new_key_already_has_one(self):
        self.seed_legacy(score=900)
        db_operations.upsert_game_score(pid.meshcore_player_number(KEY), "baconfall", "Pers", 400, 0, 9)
        db_operations.migrate_meshcore_player_ids()
        best = self.conn.execute("SELECT score FROM game_scores WHERE user_id = ?", (NEW,)).fetchone()[0]
        self.assertEqual(best, 900)

    def test_someone_who_turned_tips_off_keeps_them_off(self):
        """Profile sync predates help tips and does not carry the setting."""
        self.seed_legacy(help_tips=0)
        db_operations.migrate_meshcore_player_ids()
        self.assertFalse(db_operations.get_help_tips_enabled(NEW))

    def test_the_bio_survives(self):
        self.seed_legacy(bio="radios and bacon")
        db_operations.migrate_meshcore_player_ids()
        self.assertEqual(db_operations.get_user_profile(NEW)[6], "radios and bacon")

    def test_it_is_idempotent(self):
        self.seed_legacy()
        db_operations.migrate_meshcore_player_ids()
        self.assertEqual(db_operations.migrate_meshcore_player_ids()["players"], 0)
        self.assertEqual(self.rows("game_scores", NEW), 1)

    def test_a_number_two_meshcore_keys_share_is_left_alone(self):
        """No way to tell whose rows those are; guessing would hand one
        person's history to another."""
        self.roster(TWIN)
        self.seed_legacy()
        summary = db_operations.migrate_meshcore_player_ids()
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(self.rows("game_scores", LEGACY), 1)
        self.assertEqual(self.rows("game_scores", NEW), 0)

    def test_a_number_a_meshtastic_node_also_uses_is_left_alone(self):
        self.roster("!dbc37568", protocol="Meshtastic", node_num=LEGACY)
        self.seed_legacy()
        self.assertEqual(db_operations.migrate_meshcore_player_ids()["ambiguous"], 1)
        self.assertEqual(self.rows("game_scores", LEGACY), 1)

    def test_players_who_are_not_meshcore_are_untouched(self):
        db_operations.upsert_synced_game_score("67472072", "trivia", "bacon", 10, 0, 1,
                                               "2026-09-01 10:00:00")
        db_operations.migrate_meshcore_player_ids()
        self.assertEqual(self.rows("game_scores", "67472072"), 1)

    def test_a_baconfall_run_moves_too(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS baconfall_runs "
                          "(user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
        self.conn.execute("INSERT INTO baconfall_runs VALUES (?, ?)", (LEGACY, '{"phase":"map"}'))
        self.conn.commit()
        db_operations.migrate_meshcore_player_ids()
        self.assertEqual(self.rows("baconfall_runs", NEW), 1)
        self.assertEqual(self.rows("baconfall_runs", LEGACY), 0)

    def test_it_runs_during_maintenance(self):
        with mock.patch.object(db_operations, "migrate_meshcore_player_ids",
                               return_value={"players": 3}) as migrate:
            summary = db_operations.run_db_maintenance()
        migrate.assert_called_once()
        self.assertEqual(summary["meshcore_players_migrated"], 3)


class ScoreboardNameTests(_Db):
    def test_a_migrated_player_is_still_named_by_their_account(self):
        account_id = db_operations.create_account()
        self.conn.execute("UPDATE accounts SET alias='Materva', alias_normalized='materva'"
                          " WHERE account_id=?", (account_id,))
        self.conn.execute("INSERT INTO linked_nodes (node_id, account_id, network, linked_at)"
                          " VALUES (?, ?, 'meshcore', '2026-09-01')", (KEY, account_id))
        self.conn.commit()
        self.assertEqual(db_operations.get_score_account_names([NEW]), {NEW: "Materva"})


if __name__ == "__main__":
    unittest.main()
