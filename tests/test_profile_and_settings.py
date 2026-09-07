"""Profile and Settings: one place for who you are, one for what it does.

Both used to be a mess. Profile was defined but never shown -- reachable
only as !P. The 'S' entry was labelled "Linked Devices" and jumped straight
past a Settings menu that existed in the code and was never rendered. Your
bio was in one place, your linked devices in another, and the mail relay
toggle sat on the Profile screen despite being a preference.

The split now: Profile is who you are (name, alias, role, stats, scores,
bio, and the devices that are you). Settings is what the BBS does for you
(mail relay, the Node View lens, what node you reached).
"""

import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)

import command_handlers as ch
import db_operations


class _Interface:
    bbs_nodes = []
    nodes = {"!abc": {"num": 1234, "user": {"id": "!abc", "shortName": "bac"}}}


class _Case(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.sent = []
        self._real = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _Interface()
        db_operations.auto_upsert_user_profile(1234, "bac", "bacon")
        self.addCleanup(self._restore)

    def _restore(self):
        ch.send_message = self._real
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    @property
    def last(self):
        return self.sent[-1]


class MenuPlacementTests(_Case):
    def test_profile_can_no_longer_be_hidden(self):
        """It was defined but absent from every live menu, so the only way
        in was knowing !P existed."""
        self.assertIn("P", ch.MENU_REQUIRED["main"])
        rendered = ch.build_menu(["Q", "B", "U", "X"], "BBS")
        self.assertIn("Profile", rendered)

    def test_the_s_entry_says_what_it_opens(self):
        self.assertEqual(ch.MAIN_MENU_LABELS["S"], "Settings")

    def test_both_are_on_the_main_menu(self):
        rendered = ch.build_menu(["Q", "B", "U", "X"], "BBS")
        self.assertIn("Profile", rendered)
        self.assertIn("Settings", rendered)

    def test_the_menu_still_fits_one_meshcore_packet(self):
        """160 bytes total, and this screen is re-sent on every return to
        the top. Two more entries must not cost a second chunk."""
        rendered = ch.build_menu(["Q", "B", "U", "X"], "\U0001F4BEBacon BBS\U0001F4BE (✉️:0)")
        self.assertLessEqual(len(rendered.encode("utf-8")), 160)


class ProfileScreenTests(_Case):
    def test_it_shows_who_you_are_and_what_you_have_done(self):
        db_operations.update_user_bio(1234, "likes radios")
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertIn("bac", self.last)
        self.assertIn("Msgs:", self.last)
        self.assertIn("likes radios", self.last)

    def test_linked_devices_is_reachable_from_profile(self):
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertIn("Linked devices", self.last)
        self.sent.clear()
        ch.handle_profile_steps(1234, "2", self.iface, sender_node_id="!abc")
        self.assertIn("Request link code", self.last)
        self.assertEqual(ch.get_user_state(1234)["command"], "ACCOUNT")

    def test_back_from_linked_devices_returns_to_profile(self):
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        ch.handle_profile_steps(1234, "2", self.iface, sender_node_id="!abc")
        self.sent.clear()
        ch.handle_account_steps(1234, "0", self.iface, "!abc")
        self.assertIn("bac", self.last)
        self.assertEqual(ch.get_user_state(1234)["command"], "PROFILE")

    def test_the_bio_can_be_edited(self):
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        ch.handle_profile_steps(1234, "1", self.iface, sender_node_id="!abc")
        self.assertIn("bio", self.last.lower())
        ch.handle_profile_steps(1234, "a new bio", self.iface, sender_node_id="!abc")
        self.assertIn("a new bio", self.last)

    def test_your_role_is_shown_when_you_have_one(self):
        """You can be banned or promoted without ever being told. The role
        belongs on the screen about you."""
        db_operations.set_node_role("!abc", "vip")
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertIn("vip", self.last)

    def test_an_ordinary_user_is_not_told_a_role(self):
        """'Role:unregistered' is noise on a 160-byte screen."""
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertNotIn("Role:", self.last)

    def test_the_alias_is_shown_only_when_it_differs(self):
        """The alias is the name that actually appears on your posts; the
        short name is whatever your radio reports."""
        account_id = db_operations.create_account()
        db_operations.link_node_to_account("!abc", account_id, "meshtastic")
        db_operations.set_account_alias(account_id, "Bacon")
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertIn("Posts as: Bacon", self.last)

        db_operations.set_account_alias(account_id, "bac")
        self.sent.clear()
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertNotIn("Posts as", self.last)

    def test_preferences_are_not_on_the_profile_screen(self):
        """They moved to Settings. Leaving a copy here is how two screens
        start disagreeing about one value."""
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertNotIn("relay", self.last.lower())


class SettingsScreenTests(_Case):
    def test_it_reports_the_current_value_of_each_setting(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertIn("Offline mail relay: Off", self.last)
        self.assertIn("Node View: All nodes", self.last)

    def test_the_lens_is_named_once_it_is_narrowed(self):
        ch.set_view_scope(1234, ["mqtt:baconbbsvt:Chattanooga"])
        self.addCleanup(ch.clear_view_scope, 1234)
        with mock.patch.object(ch, "get_node_nicknames",
                               lambda: {"mqtt:baconbbsvt:Chattanooga": "Chattanooga"}), \
             mock.patch.object(ch, "local_identities_for_display", lambda: set()):
            ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertIn("Node View: Chattanooga", self.last)

    def test_this_node_reads_as_this_node(self):
        ch.set_view_scope(1234, ["mqtt:baconbbs:bbs-main"])
        self.addCleanup(ch.clear_view_scope, 1234)
        with mock.patch.object(ch, "local_identities_for_display",
                               lambda: {"mqtt:baconbbs:bbs-main"}):
            ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertIn("Node View: This node", self.last)

    def test_the_node_view_picker_opens(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        ch.handle_settings_steps(1234, "2", self.iface, "!abc")
        self.assertEqual(ch.get_user_state(1234)["command"], "NODE_VIEW")

    def test_about_says_the_version_and_returns(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.sent.clear()
        ch.handle_settings_steps(1234, "3", self.iface, "!abc")
        self.assertIn("Bacon BBS", self.sent[0])
        self.assertIn("Settings", self.last)

    def test_zero_leaves(self):
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.sent.clear()
        ch.handle_settings_steps(1234, "0", self.iface, "!abc")
        self.assertIn("Bacon BBS", self.last)

    def test_linking_is_not_duplicated_here(self):
        """It lives in Profile now. Two doors to one flow is what made the
        old S entry unlabelable."""
        ch.handle_settings_command(1234, self.iface, "!abc")
        self.assertNotIn("Request link code", self.last)
        self.assertNotIn("Linked", self.last)


class BioSurvivesSyncTests(_Case):
    """The bio is the one field on a profile a person actually authors, and
    it was the one field they could not keep.

    upsert_synced_user_profile took `bio = excluded.bio` unconditionally
    while every other column had a newer-wins guard. On the live fleet an
    edit was accepted, displayed back, and empty again within one sync
    cycle, because the peer still held the older copy and its arrival won.
    """

    def _peer_sends(self, bio, last_seen):
        db_operations.upsert_synced_user_profile(
            "1234", "bac", "bacon", "2026-09-01 10:00:00", last_seen, 5, bio)

    def _bio(self):
        return db_operations.get_db_connection().execute(
            "SELECT bio FROM user_profiles WHERE user_id = '1234'").fetchone()[0]

    def test_an_older_peer_copy_cannot_wipe_a_fresh_edit(self):
        db_operations.update_user_bio(1234, "runs a solar node in VT")
        self._peer_sends("", "2026-09-01 10:00:00")
        self.assertEqual(self._bio(), "runs a solar node in VT")

    def test_a_newer_peer_copy_still_wins(self):
        """Editing on your phone must still reach the node you last used."""
        db_operations.update_user_bio(1234, "old text")
        self._peer_sends("edited elsewhere", "2099-01-01 00:00:00")
        self.assertEqual(self._bio(), "edited elsewhere")

    def test_editing_stamps_the_row_as_the_newest_copy(self):
        """The guard compares last_seen, so writing a bio without touching
        it leaves the edit stamped in the past and the peer wins anyway.

        The peer here is stamped BETWEEN the row's old value and now, which
        is the real case -- forgecam's copy is a few seconds old, not
        ancient. A peer stamped at the row's original time would be refused
        with or without the fix, and would prove nothing.
        """
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE user_profiles SET last_seen = '2026-01-01 00:00:00'"
                     " WHERE user_id = '1234'")
        conn.commit()

        db_operations.update_user_bio(1234, "something new")
        self._peer_sends("", "2026-06-01 00:00:00")
        self.assertEqual(self._bio(), "something new")

    def test_the_edit_survives_a_full_round_trip_through_the_handler(self):
        """End to end: the path a user actually walks."""
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        ch.handle_profile_steps(1234, "1", self.iface, sender_node_id="!abc")
        ch.handle_profile_steps(1234, "a real bio", self.iface, sender_node_id="!abc")
        self._peer_sends("", "2026-09-01 10:00:00")
        self.sent.clear()
        ch.handle_profile_command(1234, self.iface, sender_node_id="!abc")
        self.assertIn("a real bio", self.last)


if __name__ == "__main__":
    unittest.main()
