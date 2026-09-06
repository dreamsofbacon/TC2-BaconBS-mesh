"""The Node View lens: whose content a session is reading.

Every bulletin, mail and comment already records the node it was written on,
and every one of those columns was invisible -- command_handlers.py did not
mention source_node_id once. This is the layer that turns that recorded
origin into something a user can ask about.

Two rules here are easy to implement in a way that looks right and is not:

  * "This node" is a SET of ids. A node answers to a radio id AND one
    mqtt:<topic>:<label> per bridge, and on the live fleet almost all
    content is stamped with the mqtt one. An implementation comparing
    get_local_node_id() alone passes any test whose fixture happens to use
    the radio id.
  * A NULL origin means "written here, before origin tracking existed".
    That has to be true in BOTH directions: NULL rows appear under This
    node, and disappear under a peer. A test asserting only one direction
    passes an implementation that hardcodes the other.
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import db_operations
import utils


RADIO = "!04058ac8"
BRIDGE = "mqtt:baconbbsvt:Burlington-NNE"
PEER = "mqtt:baconbbsvt:Chattanooga"
PEER_RADIO = "!0408b778"
CAPTURE = "5a582498f3d5f2b91a9ea3bbb21c6f1f2355bc3eca060cfac6a98a5105f69930"


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.ini"
        self.config_path.write_text("[boards]\nbulletin_boards = General\n",
                                    encoding="utf-8")
        self.env_patch = mock.patch.dict(
            os.environ,
            {"BBS_DB_PATH": str(self.root / "bulletins.db"),
             "BBS_CONFIG_PATH": str(self.config_path)},
            clear=False)
        self.env_patch.start()
        db_operations.initialize_database()

        self._saved = (db_operations._local_node_id,
                       db_operations.get_local_link_identities(),
                       db_operations.get_local_capture_identities())
        db_operations.set_local_node_id(RADIO)
        db_operations.set_local_link_identities([RADIO, BRIDGE])
        db_operations.set_local_capture_identities([CAPTURE])

        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._restore)
        self.addCleanup(self._close)

    def _restore(self):
        db_operations._LOCAL_IDENTITY_CACHE = None
        db_operations.set_local_node_id(self._saved[0])
        db_operations.set_local_link_identities(self._saved[1])
        db_operations.set_local_capture_identities(self._saved[2])

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        if hasattr(db_operations.thread_local, "connection_origin"):
            del db_operations.thread_local.connection_origin

    def _bulletin(self, subject, source_node_id):
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO bulletins (board, sender_short_name, date, subject, content,"
            " unique_id, source_node_id) VALUES ('General','who','2026-09-05 10:00',?,'body',?,?)",
            (subject, f"uid-{subject}", source_node_id))
        conn.commit()

    def _mail(self, subject, source_node_id, recipient=RADIO):
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO mail (sender, sender_short_name, recipient, date, subject,"
            " content, unique_id, source_node_id)"
            " VALUES ('s','who',?,'2026-09-05 10:00',?,'body',?,?)",
            (recipient, subject, f"m-{subject}", source_node_id))
        conn.commit()

    def _subjects(self, rows):
        """Bulletin rows: (id, subject, sender_short_name, date, unique_id)."""
        return sorted(r[1] for r in rows)

    def _mail_subjects(self, rows):
        """Mail rows put the sender first: (id, sender, subject, date, uid)."""
        return sorted(r[2] for r in rows)


class NullOriginTests(_DbCase):
    """A row with no recorded origin was written here."""

    def setUp(self):
        super().setUp()
        self._bulletin("old", None)
        self._bulletin("mine", RADIO)
        self._bulletin("theirs", PEER)

    def test_this_node_includes_rows_written_before_origin_tracking(self):
        rows = db_operations.get_bulletins("General", [RADIO, BRIDGE])
        self.assertEqual(self._subjects(rows), ["mine", "old"])

    def test_a_peer_scope_excludes_them(self):
        """The other direction, and it must be asserted separately: an
        implementation that always folds NULL in passes the test above."""
        rows = db_operations.get_bulletins("General", [PEER])
        self.assertEqual(self._subjects(rows), ["theirs"])

    def test_all_nodes_still_returns_everything(self):
        rows = db_operations.get_bulletins("General")
        self.assertEqual(self._subjects(rows), ["mine", "old", "theirs"])

    def test_the_hidden_count_agrees_with_what_was_left_out(self):
        for scope, shown, hidden in (([RADIO, BRIDGE], 2, 1), ([PEER], 1, 2)):
            with self.subTest(scope=scope):
                self.assertEqual(len(db_operations.get_bulletins("General", scope)), shown)
                self.assertEqual(
                    db_operations.count_hidden_bulletins("General", scope), hidden)

    def test_shown_plus_hidden_is_always_the_whole_board(self):
        """The property that makes the on-screen count trustworthy."""
        total = len(db_operations.get_bulletins("General"))
        for scope in ([RADIO], [BRIDGE], [PEER], [RADIO, PEER], [PEER_RADIO]):
            with self.subTest(scope=scope):
                shown = len(db_operations.get_bulletins("General", scope))
                hidden = db_operations.count_hidden_bulletins("General", scope)
                self.assertEqual(shown + hidden, total)


class IdentitySetTests(_DbCase):
    """A node is not one id."""

    def test_this_node_covers_the_mqtt_identity_too(self):
        """The live fleet stamps almost everything with the bridge id, so an
        implementation comparing get_local_node_id() alone loses nearly all
        of it while still passing any radio-id-only fixture."""
        self._bulletin("viaradio", RADIO)
        self._bulletin("viabridge", BRIDGE)
        rows = db_operations.get_bulletins("General", [RADIO, BRIDGE])
        self.assertEqual(self._subjects(rows), ["viabridge", "viaradio"])

    def test_null_is_local_by_way_of_the_bridge_id_alone(self):
        """Picking This node on a node whose radio id never stamped anything
        must still bring the pre-tracking rows with it."""
        self._bulletin("old", None)
        rows = db_operations.get_bulletins("General", [BRIDGE])
        self.assertEqual(self._subjects(rows), ["old"])

    def test_a_peer_id_that_is_not_ours_does_not_pull_in_null(self):
        self._bulletin("old", None)
        self.assertEqual(db_operations.get_bulletins("General", [PEER_RADIO]), [])


class SeparateProcessTests(_DbCase):
    """The SSH front end and the web admin own no radio.

    This shipped broken and was caught only by driving a real SSH session:
    set_local_link_identities is called by server.py, and bacon-ssh is a
    different process with its own module globals, so the set was empty
    there. "This node" then meant an empty id list, which is indistinguishable
    from "no filter" -- the picker starred All nodes and This node at once
    and narrowing did nothing at all.

    Every other test in this file sets the globals in-process, which is
    exactly why none of them saw it.
    """

    def _as_a_separate_process(self):
        """Forget everything only server.py could know."""
        # '' not None: set_local_node_id stringifies, so None would become
        # the literal 'None' and then look like an id this node answers to.
        db_operations.set_local_node_id('')
        db_operations.set_local_link_identities([])
        db_operations.set_local_capture_identities([])
        db_operations._LOCAL_IDENTITY_CACHE = None

    def test_this_node_is_still_known_without_the_radios(self):
        db_operations.persist_local_identities([RADIO, BRIDGE], [CAPTURE])
        self._as_a_separate_process()
        self.assertEqual(db_operations.get_local_identities_for_scope(),
                         {RADIO, BRIDGE, CAPTURE})

    def test_null_is_still_local_without_the_radios(self):
        """The rule that decides whether pre-tracking rows belong to you."""
        db_operations.persist_local_identities([RADIO, BRIDGE], [])
        self._as_a_separate_process()
        self._bulletin("old", None)
        self._bulletin("theirs", PEER)
        rows = db_operations.get_bulletins("General", [RADIO, BRIDGE])
        self.assertEqual(self._subjects(rows), ["old"])

    def test_a_capture_id_reads_as_this_node_without_the_radios(self):
        db_operations.persist_local_identities([RADIO], [CAPTURE])
        self._as_a_separate_process()
        self.assertEqual(utils.node_display_name(CAPTURE), "this node")

    def test_the_sync_identity_set_is_left_narrow(self):
        """get_local_link_identities decides 'is this me?' during sync, where
        a wrong answer makes a node repair against itself forever. It must
        NOT pick up persisted or capture ids."""
        db_operations.persist_local_identities([RADIO, BRIDGE], [CAPTURE])
        self.assertNotIn(CAPTURE, db_operations.get_local_link_identities())
        self._as_a_separate_process()
        self.assertEqual(db_operations.get_local_link_identities(), set())

    def test_persisting_replaces_rather_than_accumulates(self):
        """A bridge removed from config stops being us."""
        db_operations.persist_local_identities([RADIO, BRIDGE], [])
        db_operations.persist_local_identities([RADIO], [])
        self._as_a_separate_process()
        self.assertEqual(db_operations.get_local_identities_for_scope(), {RADIO})

    def test_the_radio_id_is_recorded_once_it_is_known(self):
        """Startup publishes before the runtime snapshot has run, and the
        snapshot is what calls set_local_node_id -- so the first publish
        knows the bridge ids and not the radio id. Live, the persisted set
        held two mqtt ids and no '!04058ac8'."""
        import server

        saved_links = server._active_links
        server._active_links = []
        self.addCleanup(setattr, server, '_active_links', saved_links)

        db_operations.set_local_node_id('')
        server.publish_local_identities()          # as at startup
        db_operations._LOCAL_IDENTITY_CACHE = None
        self.assertNotIn(RADIO, db_operations.get_persisted_local_identities())

        db_operations.set_local_node_id(RADIO)     # as the snapshot resolves it
        server.publish_local_identities()
        db_operations._LOCAL_IDENTITY_CACHE = None
        self.assertIn(RADIO, db_operations.get_persisted_local_identities())

    def test_startup_actually_writes_them_down(self):
        """The half that is easy to leave out: a working persist function
        nothing ever calls looks exactly like a working feature until you
        open the BBS over SSH."""
        import server

        class _Link:
            def __init__(self, node_id, capture):
                self.interface = types.SimpleNamespace(
                    self_node_id=node_id,
                    public_chatter_capture_node_id=capture)

        saved_links = server._active_links
        server._active_links = [_Link(BRIDGE, CAPTURE)]
        self.addCleanup(setattr, server, '_active_links', saved_links)

        db_operations.set_local_node_id(RADIO)
        server.publish_local_identities()
        self._as_a_separate_process()
        self.assertEqual(db_operations.get_local_identities_for_scope(),
                         {RADIO, BRIDGE, CAPTURE})


class EmptyScopeTests(_DbCase):
    def test_an_empty_scope_matches_nothing_rather_than_everything(self):
        """A filter that silently does the opposite of what was asked is
        worse than one that returns an empty screen you can see and widen."""
        self._bulletin("mine", RADIO)
        self.assertEqual(db_operations.get_bulletins("General", [""]), [])
        self.assertEqual(db_operations.count_hidden_bulletins("General", [""]), 1)

    def test_none_is_not_the_same_as_empty(self):
        self._bulletin("mine", RADIO)
        self.assertEqual(len(db_operations.get_bulletins("General", None)), 1)


class MailScopeTests(_DbCase):
    def setUp(self):
        super().setUp()
        self._mail("here", None)
        self._mail("fromthem", PEER)

    def test_the_mailbox_narrows(self):
        rows = db_operations.get_mail(RADIO, [PEER])
        self.assertEqual(self._mail_subjects(rows), ["fromthem"])

    def test_this_node_keeps_the_pre_tracking_mail(self):
        rows = db_operations.get_mail(RADIO, [RADIO, BRIDGE])
        self.assertEqual(self._mail_subjects(rows), ["here"])

    def test_the_hidden_count_is_what_the_user_is_told(self):
        self.assertEqual(db_operations.count_hidden_mail(RADIO, [PEER]), 1)
        self.assertEqual(db_operations.count_hidden_mail(RADIO, None), 0)

    def test_a_selected_message_stays_readable_while_narrowed(self):
        """The handlers pass None deliberately. Narrowing between the list
        and the read must not answer 'Mail not found' about a message the
        user was just looking at."""
        row_id = db_operations.get_db_connection().execute(
            "SELECT id FROM mail WHERE subject = 'here'").fetchone()[0]
        self.assertIsNotNone(db_operations.get_mail_content(row_id, RADIO))
        self.assertIsNotNone(db_operations.get_mail_content(row_id, RADIO, None))
        self.assertIsNone(db_operations.get_mail_content(row_id, RADIO, [PEER]))


class CommentScopeTests(_DbCase):
    def setUp(self):
        super().setUp()
        conn = db_operations.get_db_connection()
        conn.execute("INSERT INTO channels (name, url) VALUES ('Chan','psk')")
        self.channel_id = conn.execute(
            "SELECT id FROM channels WHERE name='Chan'").fetchone()[0]
        for uid, source in (("c1", None), ("c2", PEER)):
            conn.execute(
                "INSERT INTO channel_comments (channel_id, sender_short_name, date,"
                " content, unique_id, source_node_id)"
                " VALUES (?,'who','2026-09-05 10:00','text',?,?)",
                (self.channel_id, uid, source))
        conn.commit()

    def test_comments_narrow_and_the_count_agrees(self):
        rows = db_operations.get_channel_comments(self.channel_id, [PEER])
        self.assertEqual([r[4] for r in rows], ["c2"])
        self.assertEqual(
            db_operations.count_hidden_channel_comments(self.channel_id, [PEER]), 1)

    def test_all_nodes_returns_both(self):
        self.assertEqual(
            len(db_operations.get_channel_comments(self.channel_id)), 2)


class DefaultScopeCostTests(_DbCase):
    """All nodes is the default. It must cost exactly what it did before."""

    def _captured_sql(self, call):
        """Every SQL string one call issues, in order."""
        captured = []
        real_conn = db_operations.get_db_connection()

        class _Cursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=()):
                captured.append(sql)
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class _Conn:
            def cursor(self):
                return _Cursor(real_conn.cursor())

            def execute(self, sql, params=()):
                captured.append(sql)
                return real_conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        with mock.patch.object(db_operations, 'get_db_connection', _Conn):
            call()
        return captured

    def test_the_counters_do_not_touch_the_database(self):
        """Every screen calls these. On a radio each query sits in front of
        a 2-second-per-chunk send, so an unconditional COUNT would tax the
        default path for a line it will not print."""
        for label, call in (
                ("bulletins", lambda: db_operations.count_hidden_bulletins("General", None)),
                ("mail", lambda: db_operations.count_hidden_mail(RADIO, None)),
                ("comments", lambda: db_operations.count_hidden_channel_comments(1, None))):
            with self.subTest(scope=label):
                self.assertEqual(self._captured_sql(call), [])

    def test_the_bulletin_query_is_unchanged(self):
        """Byte-identical SQL, not merely equivalent results -- an extra
        clause that happens to select everything is still a behaviour change
        on the path every existing user takes."""
        self.assertEqual(
            self._captured_sql(lambda: db_operations.get_bulletins("General")),
            ["SELECT id, CASE WHEN COALESCE(content_complete, 1) = 0 THEN subject"
             " || ' [incomplete]' ELSE subject END, sender_short_name, date,"
             " unique_id FROM bulletins WHERE board = ? COLLATE NOCASE"])

    def test_the_mail_query_is_unchanged(self):
        sql = self._captured_sql(lambda: db_operations.get_mail(RADIO))
        self.assertIn(
            "SELECT id, sender_short_name, CASE WHEN COALESCE(content_complete, 1) = 0"
            " THEN subject || ' [incomplete]' ELSE subject END, date, unique_id"
            " FROM mail WHERE recipient IN (?) ORDER BY id", sql)


class DiscoveryTests(_DbCase):
    def test_only_nodes_with_content_are_offered(self):
        """Offering a node holding nothing gives the user a choice whose
        only outcome is an empty screen they cannot distinguish from a bug."""
        self._bulletin("mine", RADIO)
        self._mail("theirs", PEER)
        self.assertEqual(db_operations.get_content_source_nodes(), [RADIO, PEER])

    def test_null_origins_are_not_offered_as_a_node(self):
        self._bulletin("old", None)
        self.assertEqual(db_operations.get_content_source_nodes(), [])

    def test_chatter_capture_ids_are_included(self):
        """A different id namespace for the same physical node -- chatter is
        stamped with a public key, everything else with user['id']."""
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO public_chatter (unique_id, network, channel_index,"
            " channel_name, sender_node_id, sender_name, content,"
            " message_timestamp, captured_at, expires_at, capture_node_id)"
            " VALUES ('p1','meshtastic',0,'LongFast','!x','who','hi',"
            "'2026-09-05T10:00:00Z','2026-09-05T10:00:00Z','2099-01-01T00:00:00Z',?)",
            (CAPTURE,))
        conn.commit()
        self.assertIn(CAPTURE, db_operations.get_content_source_nodes())


class NameResolutionTests(_DbCase):
    def _with_nicknames(self, body):
        self.config_path.write_text(f"[node_names]\n{body}\n", encoding="utf-8")

    def test_an_mqtt_id_reads_as_its_label_with_no_config(self):
        """Why this feature is useful before anyone edits anything."""
        self.assertEqual(utils.node_display_name(PEER), "Chattanooga")

    def test_a_nickname_wins_over_everything(self):
        self._with_nicknames(f"Down South = {PEER}")
        self.assertEqual(utils.node_display_name(PEER), "Down South")

    def test_our_own_ids_read_as_this_node(self):
        for node_id in (RADIO, BRIDGE, CAPTURE):
            with self.subTest(node_id=node_id):
                self.assertEqual(utils.node_display_name(node_id), "this node")

    def test_our_own_mqtt_id_prefers_our_name_over_its_label(self):
        """Ordering: the local check must sit ahead of the mqtt-tail rule, or
        this node advertises itself to its own users as a stranger."""
        self.assertEqual(utils.node_display_name(BRIDGE), "this node")

    def test_an_unmappable_key_falls_back_to_a_short_id(self):
        key = "hW9UeHYKg+eUfhBDhHNBoT38QCnmlAXebk1OR/l6LGc="
        self.assertEqual(utils.node_display_name(key), "hW9UeHYK..LGc=")

    def test_a_nickname_can_group_two_namespaces(self):
        """The only place a peer's BBS id and its chatter capture key can be
        joined -- nothing in the database relates them."""
        self._with_nicknames(f"Chattanooga = {PEER}, {PEER_RADIO}")
        self.assertEqual(
            sorted(utils.node_ids_for_name("Chattanooga")), sorted([PEER, PEER_RADIO]))

    def test_keys_keep_their_case_and_punctuation(self):
        """ConfigParser lowercases option names by default, which would
        destroy a base64 key and mangle mqtt:<topic>:<Label>."""
        key = "hW9UeHYKg+eUfhBDhHNBoT38QCnmlAXebk1OR/l6LGc="
        self._with_nicknames(f"Vermont = {key}")
        self.assertEqual(utils.node_display_name(key), "Vermont")

    def test_a_percent_in_a_key_is_not_a_format_error(self):
        self._with_nicknames("Odd = abc%def")
        self.assertEqual(utils.node_display_name("abc%def"), "Odd")


class ScopeStateTests(unittest.TestCase):
    def setUp(self):
        utils.clear_view_scope(7)
        self.addCleanup(utils.clear_view_scope, 7)

    def test_the_default_is_all_nodes(self):
        self.assertIsNone(utils.get_view_scope(7))

    def test_setting_and_clearing(self):
        utils.set_view_scope(7, [PEER])
        self.assertEqual(utils.get_view_scope(7), (PEER,))
        utils.clear_view_scope(7)
        self.assertIsNone(utils.get_view_scope(7))

    def test_an_empty_list_widens_back_to_all(self):
        """One code path for widening, so no caller can forget the other."""
        utils.set_view_scope(7, [PEER])
        utils.set_view_scope(7, [])
        self.assertIsNone(utils.get_view_scope(7))

    def test_the_scope_keeps_every_id_it_was_given(self):
        """A grouped nickname sets several ids at once; keeping only the
        first silently drops half a node."""
        utils.set_view_scope(7, [PEER, PEER_RADIO])
        self.assertEqual(utils.get_view_scope(7), (PEER, PEER_RADIO))

    def test_duplicates_and_blanks_are_dropped_in_order(self):
        utils.set_view_scope(7, [PEER, "", "  ", PEER])
        self.assertEqual(utils.get_view_scope(7), (PEER,))

    def test_a_menu_move_does_not_disturb_it(self):
        """The reason this is not inside user_states: update_user_state
        replaces that dict wholesale on every menu move."""
        utils.set_view_scope(7, [PEER])
        utils.update_user_state(7, {'command': 'MAIL', 'step': 1})
        utils.update_user_state(7, {'command': 'MAIN_MENU', 'step': 1})
        utils.clear_user_state(7)
        self.assertEqual(utils.get_view_scope(7), (PEER,))


class ScopeNoticeTests(_DbCase):
    def setUp(self):
        super().setUp()
        utils.clear_view_scope(7)
        self.addCleanup(utils.clear_view_scope, 7)

    def test_the_default_scope_says_nothing_at_all(self):
        """Empty string, not a sentence about being unfiltered: this is what
        keeps every All-scope screen byte-identical to a build without the
        feature, on a transport where a line costs two seconds of airtime."""
        self.assertEqual(utils.scope_notice(7), "")

    def test_a_narrowed_scope_names_the_node(self):
        utils.set_view_scope(7, [PEER])
        self.assertEqual(utils.scope_notice(7), "Node view: Chattanooga. !V=all")

    def test_our_own_scope_reads_as_this_node(self):
        utils.set_view_scope(7, [RADIO, BRIDGE])
        self.assertEqual(utils.scope_notice(7), "Node view: this node. !V=all")

    def test_the_hidden_count_is_stated(self):
        utils.set_view_scope(7, [PEER])
        self.assertEqual(utils.scope_notice(7, 2),
                         "Node view: Chattanooga. 2 more from other nodes. !V=all")

    def test_mail_says_mail(self):
        utils.set_view_scope(7, [PEER])
        self.assertEqual(utils.scope_notice(7, 2, noun='mail'),
                         "Node view: Chattanooga. 2 more mail from other nodes. !V=all")

    def test_the_escape_hatch_is_prefixed(self):
        """At a mail or bulletin prompt a bare letter is read as an item
        number and never reaches the menu handlers, so only !V works. The
        wording is the mitigation, not decoration."""
        utils.set_view_scope(7, [PEER])
        notice = utils.scope_notice(7, 1)
        self.assertIn("!V=all", notice)

    def test_a_grouped_scope_names_the_node_once(self):
        self.config_path.write_text(
            f"[node_names]\nChattanooga = {PEER}, {PEER_RADIO}\n", encoding="utf-8")
        utils.set_view_scope(7, [PEER, PEER_RADIO])
        self.assertEqual(utils.scope_notice(7), "Node view: Chattanooga. !V=all")


if __name__ == "__main__":
    unittest.main()
