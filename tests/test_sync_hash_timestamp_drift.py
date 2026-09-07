"""One instant, two spellings, one hash -- for every scope that hashes one.

`f2d1337` fixed this for zork_saves. The same defect was still live in
game_scores, and dormant in channels, bulletins and mail:

    bbs.local   game_scores.achieved_at   '2026-09-06 21:24:31'
    forgecam    game_scores.achieved_at   '2026-09-06T21:24:31'

Two rows, one instant, and the record hash is built from that string, so
the two nodes disagreed about the hash permanently. The manifest diff said
the record was missing; the apply path said the incoming copy was not
newer; neither could move, so the peer asked again every cycle. That scope
was the only one our two nodes could not agree on, and it drove 88 targeted
repairs in 24 hours.

Two functions hash rows and they do NOT hash the same columns, so both are
tested here for every scope. Normalising only one is worse than normalising
neither: the scope reports a mismatch that the record diff then finds
nothing to fix, and the repair cycle runs forever finding nothing.

public_chatter is deliberately out of scope for this pass -- its expires_at
is derived and rows leave the scope as they expire, so its hash churns by
design. It is also the only scope whose timestamps never pass through
decode_ts_second, so nothing here can introduce drift into it.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations

SPACE_FORM = "2026-09-06 21:24:31"
T_FORM = "2026-09-06T21:24:31"
Z_FORM = "2026-09-06T21:24:31Z"
ALL_SPELLINGS = (SPACE_FORM, T_FORM, Z_FORM)


class _ScopeCase(unittest.TestCase):
    """A real database, because these are SQL row hashes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_DB_PATH": str(Path(self.temp_dir.name) / "bulletins.db")},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    @property
    def conn(self):
        return db_operations.get_db_connection()

    def assert_one_hash_across_spellings(self, store, scope, aggregate_key):
        """The whole contract, both functions, in one place."""
        manifests, aggregates = [], []
        for spelling in ALL_SPELLINGS:
            store(spelling)
            manifests.append(db_operations.get_record_hash_manifest(scope))
            aggregates.append(
                db_operations.get_local_record_counts()[aggregate_key])
        for index, spelling in enumerate(ALL_SPELLINGS[1:], start=1):
            with self.subTest(spelling=spelling, function="manifest"):
                self.assertEqual(manifests[0], manifests[index])
            with self.subTest(spelling=spelling, function="aggregate"):
                self.assertEqual(aggregates[0], aggregates[index])


class GameScoreDriftTests(_ScopeCase):
    """The one scope that was actively failing on the live fleet."""

    def _store(self, achieved_at):
        self.conn.execute("DELETE FROM game_scores")
        self.conn.execute(
            "INSERT INTO game_scores (user_id, game_id, short_name, score,"
            " max_score, moves, achieved_at) VALUES ('3758096387', 'trivia',"
            " 'baconbot', 2200, 0, 12, ?)", (achieved_at,))
        self.conn.commit()

    def test_one_instant_hashes_the_same_however_it_is_spelled(self):
        self.assert_one_hash_across_spellings(
            self._store, 'game_scores', 'game_scores_hash')

    def test_the_two_functions_agree_on_the_row_count(self):
        for spelling in ALL_SPELLINGS:
            with self.subTest(spelling=spelling):
                self._store(spelling)
                counts = db_operations.get_local_record_counts()
                manifest = db_operations.get_record_hash_manifest('game_scores')
                self.assertEqual(counts['game_scores'], len(manifest))

    def test_a_synced_score_is_stored_in_the_canonical_form(self):
        """The direct cause of the live divergence: upsert_synced_game_score
        computed a normalised value for the tombstone check and then stored
        the raw one."""
        db_operations.upsert_synced_game_score(
            "3758096387", "trivia", "baconbot", 2200, 0, 12, T_FORM)
        stored = self.conn.execute(
            "SELECT achieved_at FROM game_scores").fetchone()[0]
        self.assertEqual(stored, SPACE_FORM)

    def test_a_score_does_not_ping_pong_between_two_nodes(self):
        """End to end. Our copy arrived over the wire; the peer made it
        locally. Applying the peer's copy must leave the hash untouched --
        that is the repair loop, gone."""
        self._store(T_FORM)
        before = db_operations.get_record_hash_manifest('game_scores')
        db_operations.upsert_synced_game_score(
            "3758096387", "trivia", "baconbot", 2200, 0, 12, SPACE_FORM)
        self.assertEqual(
            before, db_operations.get_record_hash_manifest('game_scores'))

    def test_a_genuinely_better_score_still_replaces(self):
        """The normalisation must not flatten real differences."""
        self._store(SPACE_FORM)
        db_operations.upsert_synced_game_score(
            "3758096387", "trivia", "baconbot", 9999, 0, 5,
            "2026-09-07T08:00:00")
        row = self.conn.execute(
            "SELECT score, achieved_at FROM game_scores").fetchone()
        self.assertEqual(row[0], 9999)
        self.assertEqual(row[1], "2026-09-07 08:00:00")

    def test_two_different_instants_still_hash_differently(self):
        """A test that only proves values collapse would pass if the hash
        ignored the timestamp entirely."""
        self._store(SPACE_FORM)
        same = db_operations.get_record_hash_manifest('game_scores')
        self._store("2026-09-06 21:24:32")
        self.assertNotEqual(
            same, db_operations.get_record_hash_manifest('game_scores'))


class ChannelCommentDriftTests(_ScopeCase):
    """channels hashes cc.date in the aggregate and cc.date plus
    cc.source_timestamp in the manifest -- three columns, two functions."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO channels (name, url, local_only)"
            " VALUES ('General', 'https://example.invalid/g', 0)")
        self.conn.commit()
        self.channel_id = self.conn.execute(
            "SELECT id FROM channels").fetchone()[0]

    def _store(self, spelling):
        self.conn.execute("DELETE FROM channel_comments")
        self.conn.execute(
            "INSERT INTO channel_comments (channel_id, sender_short_name,"
            " date, content, unique_id, expected_content_length,"
            " content_complete, source_node_id, source_timestamp, received_at)"
            " VALUES (?, 'bacon', ?, 'hello', 'uid-1', 5, 1,"
            " 'mqtt:baconbbsvt:Burlington-NNE', ?, ?)",
            (self.channel_id, spelling, spelling, spelling))
        self.conn.commit()

    def test_one_instant_hashes_the_same_however_it_is_spelled(self):
        self.assert_one_hash_across_spellings(
            self._store, 'channel_comments', 'channels_hash')

    def test_the_date_column_is_normalised_in_the_aggregate(self):
        """cc.date is hashed by the aggregate and nothing else covers it."""
        self._store(SPACE_FORM)
        space = db_operations.get_local_record_counts()['channels_hash']
        self.conn.execute("UPDATE channel_comments SET date = ?", (T_FORM,))
        self.conn.commit()
        self.assertEqual(
            space, db_operations.get_local_record_counts()['channels_hash'])

    def test_the_source_timestamp_is_normalised_in_the_manifest(self):
        """source_timestamp is hashed by the manifest and not the aggregate,
        so only the manifest can prove this one."""
        self._store(SPACE_FORM)
        space = db_operations.get_record_hash_manifest('channel_comments')
        self.conn.execute(
            "UPDATE channel_comments SET source_timestamp = ?", (T_FORM,))
        self.conn.commit()
        self.assertEqual(
            space, db_operations.get_record_hash_manifest('channel_comments'))


class DormantScopeTests(_ScopeCase):
    """bulletins and mail hash source_timestamp in the manifest but NOT in
    the aggregate, so their drift was dormant: the aggregate matched, no
    repair was triggered, and nothing was noticed. It stops being dormant
    the moment the aggregate mismatches for any other reason -- the record
    diff then reports phantom missing records, present on both sides and
    spelled differently, and the loop starts."""

    def _store_bulletin(self, spelling):
        self.conn.execute("DELETE FROM bulletins")
        self.conn.execute(
            "INSERT INTO bulletins (board, sender_short_name, date, subject,"
            " content, unique_id, local_only, expected_content_length,"
            " content_complete, source_node_id, source_timestamp, received_at)"
            " VALUES ('General', 'bacon', ?, 'subj', 'body', 'uid-b', 0, 4, 1,"
            " 'mqtt:baconbbsvt:Burlington-NNE', ?, ?)",
            (spelling, spelling, spelling))
        self.conn.commit()

    def _store_mail(self, spelling):
        self.conn.execute("DELETE FROM mail")
        self.conn.execute(
            "INSERT INTO mail (sender, sender_short_name, recipient, date,"
            " subject, content, unique_id, expected_content_length,"
            " content_complete, source_node_id, source_timestamp, received_at)"
            " VALUES ('!a', 'bacon', '!b', ?, 'subj', 'body', 'uid-m', 4, 1,"
            " 'mqtt:baconbbsvt:Burlington-NNE', ?, ?)",
            (spelling, spelling, spelling))
        self.conn.commit()

    def test_bulletins_hash_the_same_however_spelled(self):
        self.assert_one_hash_across_spellings(
            self._store_bulletin, 'bulletins', 'bulletins_hash')

    def test_mail_hashes_the_same_however_spelled(self):
        self.assert_one_hash_across_spellings(
            self._store_mail, 'mail', 'mail_hash')

    def test_the_two_functions_agree_on_the_row_count(self):
        for scope, store, key in (('bulletins', self._store_bulletin, 'bulletins'),
                                  ('mail', self._store_mail, 'mail')):
            for spelling in ALL_SPELLINGS:
                with self.subTest(scope=scope, spelling=spelling):
                    store(spelling)
                    counts = db_operations.get_local_record_counts()
                    manifest = db_operations.get_record_hash_manifest(scope)
                    self.assertEqual(counts[key], len(manifest))


class OneRuleTests(_ScopeCase):
    """The aggregate used a SQL CASE for zork_saves while the manifest used
    _normalize_sync_timestamp. Two spellings of one rule: the CASE handled
    only the T-for-space swap, the Python also strips a trailing 'Z'. That
    is the exact failure the fix was written to prevent, reintroduced by the
    fix for it. Latent -- no stored timestamp carries a 'Z' today."""

    def _store_zork(self, spelling):
        self.conn.execute("DELETE FROM zork_saves")
        self.conn.execute(
            "INSERT INTO zork_saves (user_id, game_id, save_data, updated_at)"
            " VALUES ('67472072', 'hhgttg', ?, ?)", (b"save", spelling))
        self.conn.commit()

    def test_the_z_form_agrees_in_both_functions(self):
        self.assert_one_hash_across_spellings(
            self._store_zork, 'zork_saves', 'zork_saves_hash')

    def test_the_aggregate_no_longer_spells_the_rule_in_sql(self):
        import inspect
        source = inspect.getsource(db_operations.get_local_record_counts)
        self.assertNotIn("substr(updated_at", source)


class InterpreterIndependenceTests(unittest.TestCase):
    """The nodes run different Pythons -- bbs 3.13, forgecam 3.9.

    datetime.fromisoformat accepts any number of fractional digits from 3.11
    on, and only 3 or 6 before it. So a value carrying 2 or 4 digits parsed
    on one node and was returned untouched by the other, and the two nodes
    hashed one record differently: this function's own defect, one layer
    down, and invisible to a suite that runs on a single interpreter.

    Stripping the fraction and the offset before parsing removes the
    dependency. Neither survives the output format anyway, so no result
    changes -- verified against all 8737 distinct timestamp values on the
    live nodes, under both interpreters.
    """

    def test_nothing_version_sensitive_reaches_fromisoformat(self):
        """This is the only test here that can actually fail.

        The 3.9-versus-3.11 difference lives inside fromisoformat, so on a
        modern interpreter every call-and-compare test below passes whether
        or not the fix is present -- all three mutations of it survived
        exactly such a test. So pin the input rather than the output:
        whatever reaches the parser must carry no fractional seconds and no
        UTC offset, because those two things are precisely what the
        interpreters disagree about.
        """
        real = db_operations.datetime

        class _Spy:
            seen = []

            @staticmethod
            def fromisoformat(text):
                _Spy.seen.append(text)
                return real.fromisoformat(text)

        for value in ("2026-08-12T00:17:16.84+00:00",
                      "2026-08-12T00:17:16.848137Z",
                      "2026-08-12 00:17:16.8481-04:00",
                      "2026-08-12T00:17:16+05:30"):
            with self.subTest(value=value):
                _Spy.seen = []
                with mock.patch.object(db_operations, "datetime", _Spy):
                    db_operations._normalize_sync_timestamp(value)
                self.assertEqual(len(_Spy.seen), 1, "parser was not reached")
                time_part = _Spy.seen[0].partition("T")[2]
                self.assertNotIn(".", time_part, "fractional seconds survived")
                self.assertNotIn("+", time_part, "offset survived")
                self.assertNotIn("-", time_part, "negative offset survived")
                self.assertNotIn("Z", time_part, "Z survived")

    def test_every_fractional_precision_lands_on_one_answer(self):
        expected = "2026-08-12 00:17:16"
        for fraction in ("", ".8", ".84", ".848", ".8481", ".848137",
                         ".8481370000"):
            for suffix in ("", "Z", "+00:00", "-04:00", "+05:30"):
                value = f"2026-08-12T00:17:16{fraction}{suffix}"
                with self.subTest(value=value):
                    self.assertEqual(
                        db_operations._normalize_sync_timestamp(value), expected)

    def test_the_space_form_is_treated_the_same(self):
        self.assertEqual(
            db_operations._normalize_sync_timestamp("2026-08-12 00:17:16.84+00:00"),
            "2026-08-12 00:17:16")

    def test_a_missing_seconds_field_is_still_filled_in(self):
        """Real stored values look like this; the rewrite must not lose it."""
        self.assertEqual(
            db_operations._normalize_sync_timestamp("2026-03-01 21:32"),
            "2026-03-01 21:32:00")

    def test_garbage_is_still_returned_untouched(self):
        for value in ("garbage", "2026-13-45T99:99:99", "T", ""):
            with self.subTest(value=value):
                self.assertEqual(
                    db_operations._normalize_sync_timestamp(value), value)

    def test_a_date_with_no_time_survives(self):
        self.assertEqual(
            db_operations._normalize_sync_timestamp("2026-08-12"),
            "2026-08-12 00:00:00")


if __name__ == "__main__":
    unittest.main()
