"""Four small screens a live beta test called out as confusing, not broken:

- Linked Devices fetched linked_at and threw it away, leaving no way to
  tell several opaque "ssh:<uuid>" entries apart when deciding which one is
  safe to unlink.
- The bio editor advertised "max 100 chars", silently truncated anything
  longer with no warning, and had no way to remove an existing bio -- a
  blank line typed at the BBS prompt never even reaches process_message
  (ssh_server.py just redraws the prompt), so "submit nothing" was never a
  workable way to clear it.
- A bulletin board's "[N]" selection labels were raw database ids, which
  skip whatever was deleted or never synced -- "[1]", "[3]", "[8]" invited a
  reader to try "[2]" and get "Invalid bulletin number."
- Starting Zork from a saved game is a cold dfrotz spawn plus a
  restore/look handshake before anything is sent back; with no immediate
  acknowledgment that stretch of silence read as the BBS having dropped the
  request.

Each class below drives the real handler and asserts on what the user is
actually sent, per the project's own standing rule that testing the query
underneath a screen and never checking the screen itself is how a vacuous
test gets written.
"""
import sqlite3
import sys
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import command_handlers as ch
import db_operations


class _Iface:
    def __init__(self):
        self.bbs_nodes = []
        self.allowed_nodes = []
        self.nodes = {}


def _sent(mock_send_message):
    return [call.args[0] for call in mock_send_message.call_args_list]


class _DbCase(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.iface = _Iface()

    def tearDown(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection


SENDER = 9001
NODE_A = "ssh:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NODE_B = "ssh:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class LinkedDeviceTimestampTests(_DbCase):
    """The date each device was linked, now shown instead of fetched and
    discarded."""

    def setUp(self):
        super().setUp()
        self.account_id = db_operations.create_account()
        db_operations.link_node_to_account(
            NODE_A, self.account_id, "ssh", now="2026-01-15 09:00:00")
        db_operations.link_node_to_account(
            NODE_B, self.account_id, "ssh", now="2026-06-02 14:30:00")

    def test_the_device_list_shows_when_each_one_was_linked(self):
        with mock.patch.object(ch, "send_message") as sm:
            ch._handle_list_devices(SENDER, self.iface, NODE_A)
        screen = _sent(sm)[0]
        self.assertIn("linked 2026-01-15", screen)
        self.assertIn("linked 2026-06-02", screen)

    def test_the_current_device_is_still_marked_alongside_its_date(self):
        with mock.patch.object(ch, "send_message") as sm:
            ch._handle_list_devices(SENDER, self.iface, NODE_A)
        # Matched on the shortened id -- the full 36-character one is no
        # longer what the screen prints, which is the point of the labels.
        line = next(l for l in _sent(sm)[0].splitlines()
                    if ch.short_node_id(NODE_A) in l)
        self.assertIn("(this device)", line)
        self.assertIn("linked 2026-01-15", line)

    def test_the_unlink_picker_also_shows_when_each_device_was_linked(self):
        with mock.patch.object(ch, "send_message") as sm:
            ch._handle_start_unlink(SENDER, self.iface, NODE_A)
        screen = _sent(sm)[0]
        self.assertIn("linked 2026-01-15", screen)
        self.assertIn("linked 2026-06-02", screen)


RADIO_NODE = "!04058ac8"


class LinkedDeviceLabelTests(_DbCase):
    """"Improve linked-device listings with user-readable labels" -- the
    other half of the same beta-test complaint the linked-at date started
    on. Seven "ssh:<32 hex>" strings told a user nothing about which old
    device was safe to unlink.

    Everything shown is derived from what the node already stores: the
    mesh_clients roster a device's own advertisement populates, and the
    id's own shape. Nothing new to collect, nothing for a user to set.
    """

    def setUp(self):
        super().setUp()
        self.account_id = db_operations.create_account()
        db_operations.link_node_to_account(
            RADIO_NODE, self.account_id, "meshtastic", now="2026-01-15 09:00:00")
        db_operations.link_node_to_account(
            NODE_A, self.account_id, "ssh", now="2026-06-02 14:30:00")

    def _roster(self, **overrides):
        row = {
            "link_name": "primary", "node_id": RADIO_NODE, "node_num": 67472072,
            "protocol": "Meshtastic", "short_name": "BCON",
            "long_name": "Bacon Nomad", "hw_model": "HELTEC_V3",
            "role": "CLIENT", "battery_level": None, "last_heard_epoch": None,
        }
        row.update(overrides)
        db_operations.upsert_mesh_clients([row])

    # -- the label itself --------------------------------------------------
    def test_a_radio_device_is_named_by_what_it_advertised(self):
        self._roster()
        roster = db_operations.get_mesh_client_names([RADIO_NODE])
        self.assertEqual(ch._linked_device_label(RADIO_NODE, roster),
                         "BCON (HELTEC_V3)")

    def test_an_unconfigured_hardware_model_is_not_printed(self):
        """UNSET is what a Meshtastic device reports before anyone names
        its hardware -- real, and useless on screen."""
        self._roster(hw_model="UNSET")
        roster = db_operations.get_mesh_client_names([RADIO_NODE])
        self.assertEqual(ch._linked_device_label(RADIO_NODE, roster), "BCON")

    def test_a_device_with_no_short_name_falls_back_to_its_long_name(self):
        self._roster(short_name="")
        roster = db_operations.get_mesh_client_names([RADIO_NODE])
        self.assertEqual(ch._linked_device_label(RADIO_NODE, roster),
                         "Bacon Nomad (HELTEC_V3)")

    def test_a_device_the_roster_has_never_heard_of_gets_a_shortened_id(self):
        """An SSH account identity is never a radio device, so it is never
        in the roster -- and the full id is what made these unreadable."""
        label = ch._linked_device_label(NODE_A, {})
        self.assertEqual(label, ch.short_node_id(NODE_A))
        self.assertLess(len(label), len(NODE_A))

    def test_two_different_ssh_ids_still_look_different_after_shortening(self):
        """Shortening must not collapse distinct devices into one string --
        that would trade one unreadable screen for a worse one."""
        self.assertNotEqual(ch._linked_device_label(NODE_A, {}),
                            ch._linked_device_label(NODE_B, {}))

    # -- the lookup underneath --------------------------------------------
    def test_the_lookup_returns_only_ids_the_roster_knows(self):
        """Every caller has to handle a missing key, so the lookup must
        not invent one."""
        self._roster()
        found = db_operations.get_mesh_client_names([RADIO_NODE, NODE_A])
        self.assertIn(RADIO_NODE, found)
        self.assertNotIn(NODE_A, found)

    def test_the_lookup_asks_for_nothing_when_given_nothing(self):
        self.assertEqual(db_operations.get_mesh_client_names([]), {})
        self.assertEqual(db_operations.get_mesh_client_names(None), {})

    def test_an_empty_id_list_never_reaches_the_database_at_all(self):
        """Not just a wasted round-trip: with no ids the query would read
        "node_id IN ()", which is not portable SQL. SQLite happens to
        accept it here and return nothing, so the RESULT looks fine either
        way -- but this fleet's two nodes already run different SQLite
        versions (3.46.1 and 3.34.1), and that exact kind of
        works-on-one-version difference has bitten this project twice
        before. The guard is what makes it never come up."""
        with mock.patch.object(db_operations, "get_db_connection") as conn:
            self.assertEqual(db_operations.get_mesh_client_names([]), {})
        conn.assert_not_called()

    def test_the_lookup_does_not_return_the_whole_roster(self):
        """It is scoped in SQL, not filtered in Python afterwards -- the
        point is not reading a thousand rows to label six."""
        self._roster()
        self._roster(node_id="!ffffffff", short_name="OTHER", node_num=999)
        found = db_operations.get_mesh_client_names([RADIO_NODE])
        self.assertEqual(list(found), [RADIO_NODE])

    # -- through the real screens -----------------------------------------
    def test_the_device_list_shows_the_radios_name_not_its_id(self):
        self._roster()
        with mock.patch.object(ch, "send_message") as sm:
            ch._handle_list_devices(SENDER, self.iface, NODE_A)
        screen = _sent(sm)[0]
        self.assertIn("BCON (HELTEC_V3)", screen)
        self.assertNotIn(NODE_A, screen)

    def test_the_device_list_still_shows_network_and_date(self):
        """The label replaces the id, not everything else that was
        earning its place on the line."""
        self._roster()
        with mock.patch.object(ch, "send_message") as sm:
            ch._handle_list_devices(SENDER, self.iface, NODE_A)
        line = next(l for l in _sent(sm)[0].splitlines() if "BCON" in l)
        self.assertIn("[meshtastic]", line)
        self.assertIn("linked 2026-01-15", line)

    def test_the_unlink_picker_names_the_radio_too(self):
        """The screen where picking the wrong one actually costs
        something."""
        self._roster()
        with mock.patch.object(ch, "send_message") as sm:
            ch._handle_start_unlink(SENDER, self.iface, NODE_A)
        screen = _sent(sm)[0]
        self.assertIn("BCON (HELTEC_V3)", screen)
        self.assertNotIn(NODE_A, screen)


class BioTruncationAndClearTests(_DbCase):
    """Silent truncation gets a warning; clearing gets a documented word,
    because a blank line can never reach here to mean it."""

    def setUp(self):
        super().setUp()
        db_operations.auto_upsert_user_profile(SENDER, "Tester", "Tester Long")
        ch.update_user_state(SENDER, {'command': 'PROFILE', 'step': 2})

    def tearDown(self):
        ch.update_user_state(SENDER, None)
        super().tearDown()

    def test_a_bio_within_the_limit_gets_the_plain_confirmation(self):
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_profile_steps(SENDER, "Runs the BBS.", self.iface)
        self.assertEqual(_sent(sm)[0], "Bio updated!")
        self.assertEqual(db_operations.get_user_profile(SENDER)[-1], "Runs the BBS.")

    def test_an_overlong_bio_is_truncated_and_says_so(self):
        overlong = "x" * 137
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_profile_steps(SENDER, overlong, self.iface)
        notice = _sent(sm)[0]
        self.assertIn("trimmed", notice.lower())
        stored = db_operations.get_user_profile(SENDER)[-1]
        self.assertEqual(stored, "x" * 100)

    def test_clear_removes_an_existing_bio_and_confirms_it(self):
        db_operations.update_user_bio(SENDER, "Old bio text.")
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_profile_steps(SENDER, "CLEAR", self.iface)
        self.assertEqual(_sent(sm)[0], "Bio cleared.")
        self.assertEqual(db_operations.get_user_profile(SENDER)[-1], "")

    def test_clear_is_not_case_sensitive(self):
        db_operations.update_user_bio(SENDER, "Old bio text.")
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_profile_steps(SENDER, "clear", self.iface)
        self.assertEqual(_sent(sm)[0], "Bio cleared.")

    def test_the_prompt_advertises_the_clear_word(self):
        ch.update_user_state(SENDER, {'command': 'PROFILE', 'step': 1})
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_profile_steps(SENDER, "1", self.iface)
        self.assertIn("CLEAR", _sent(sm)[0])

    def test_cancel_still_leaves_an_existing_bio_untouched(self):
        db_operations.update_user_bio(SENDER, "Untouched bio.")
        with mock.patch.object(ch, "send_message"):
            ch.handle_profile_steps(SENDER, "!cancel", self.iface)
        self.assertEqual(db_operations.get_user_profile(SENDER)[-1], "Untouched bio.")


class BulletinPositionalNumberingTests(_DbCase):
    """Listing selection numbers are positions in the shown list, not
    whatever id the row happens to have in the table."""

    def setUp(self):
        super().setUp()
        # Deliberately non-contiguous ids: delete the middle one after
        # creating three, the way real moderation and sync gaps do.
        db_operations.add_bulletin("General", "alice", "First post", "body one",
                                   [], None, unique_id="u1")
        db_operations.add_bulletin("General", "bob", "Second post", "body two",
                                   [], None, unique_id="u2")
        db_operations.add_bulletin("General", "carol", "Third post", "body three",
                                   [], None, unique_id="u3")
        db_operations.delete_bulletin("u2", [], None)
        # Remaining ids are 1 and 3 -- a gap in the middle.

    def _list_state(self):
        return {'command': 'BULLETIN_ACTION', 'step': 2,
                'board': 'General', 'boards': ['General']}

    def test_the_listing_numbers_from_one_with_no_gaps(self):
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_bb_steps(SENDER, 'r', 2, self._list_state(), self.iface, [])
        texts = _sent(sm)
        self.assertIn("[1] First post", texts)
        self.assertIn("[2] Third post", texts)
        self.assertFalse(any("[3]" in t for t in texts))

    def test_the_second_listed_item_is_reachable_as_position_two(self):
        """The exact complaint: a reader tries "2" for the second entry
        shown and, before this fix, got told it was invalid because the
        underlying id was 3."""
        with mock.patch.object(ch, "send_message"):
            ch.handle_bb_steps(SENDER, 'r', 2, self._list_state(), self.iface, [])
        state = ch.get_user_state(SENDER)
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_bb_steps(SENDER, '2', 3, state, self.iface, [])
        self.assertIn("Third post", _sent(sm)[0])

    def test_an_out_of_range_position_is_rejected(self):
        with mock.patch.object(ch, "send_message"):
            ch.handle_bb_steps(SENDER, 'r', 2, self._list_state(), self.iface, [])
        state = ch.get_user_state(SENDER)
        with mock.patch.object(ch, "send_message") as sm:
            ch.handle_bb_steps(SENDER, '3', 3, state, self.iface, [])
        self.assertIn("Invalid bulletin number", _sent(sm)[0])


class ZorkLoadingAcknowledgmentTests(unittest.TestCase):
    """A cold start does real, slow work (spawn dfrotz, then a
    restore/look handshake) before it has anything to send back. The
    acknowledgment has to go out before that call, not after."""

    def setUp(self):
        self.iface = _Iface()

    def test_a_saved_game_gets_an_immediate_loading_message_first(self):
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", return_value=False), \
                mock.patch.object(ch, "has_zork_save", return_value=True), \
                mock.patch.object(ch, "start_zork_session", return_value="Restored."), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        texts = _sent(sm)
        self.assertEqual(texts[0], "Loading your saved game...")
        self.assertIn("Restored.", texts)

    def test_a_brand_new_game_gets_no_loading_message(self):
        """Nothing to restore, nothing slow to wait on -- the extra line
        would just be noise."""
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", return_value=False), \
                mock.patch.object(ch, "has_zork_save", return_value=False), \
                mock.patch.object(ch, "start_zork_session", return_value="You are here."), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        self.assertFalse(any("Loading your saved game" in t for t in _sent(sm)))

    def test_resuming_a_live_session_also_gets_no_loading_message(self):
        """A live session is already in memory -- resuming it is fast, and
        this is not the slow path the notice exists for."""
        with mock.patch.object(ch, "send_message") as sm, \
                mock.patch.object(ch, "has_zork_session", return_value=True), \
                mock.patch.object(ch, "resume_zork_session", return_value="Where you left off."), \
                mock.patch.object(ch, "get_zork_save_sync_notice", return_value=""):
            ch._launch_game(SENDER, self.iface, "zork1", "Zork I")
        self.assertFalse(any("Loading your saved game" in t for t in _sent(sm)))


if __name__ == "__main__":
    unittest.main()
