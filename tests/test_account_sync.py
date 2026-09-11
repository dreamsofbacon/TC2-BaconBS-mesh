"""Accounts that travel between nodes, and mail that waits for a radio.

Accounts used to stop at the node that created them, so a person known on
one BBS was a stranger on every other one: their mail could not be addressed,
their devices could not be recognised, and their relay consent was invisible.
These frames fix that.

What they deliberately do NOT carry is password material. A node learning an
account can address mail to that person and can never authenticate as them,
which is what makes it safe to share accounts with a node somebody else
operates. Most of this file is about the ways a peer might try to become
someone -- take their name, take their radio, outrank them -- and the fact
that none of them work.
"""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_operations
import utils

LOCAL = "11111111111111111111111111111111"
PEER = "22222222222222222222222222222222"
OTHER = "33333333333333333333333333333333"


def stamp(offset_seconds=0):
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).isoformat()


class _DbCase(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def account(self, account_id, alias="", **columns):
        """A local account, the way registering on this node would make one."""
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO accounts (account_id, alias, alias_normalized, created_at,"
            " alias_updated_at, password_hash, password_salt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, alias, db_operations.normalize_alias(alias),
             columns.get("created_at", stamp(-3600)),
             columns.get("alias_updated_at", stamp(-3600)),
             columns.get("password_hash", "localhash"),
             columns.get("password_salt", "localsalt")))
        conn.commit()

    def column(self, account_id, name):
        row = db_operations.get_db_connection().execute(
            f"SELECT {name} FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        return None if row is None else row[0]


class LearningAnAccountTests(_DbCase):
    def test_a_peer_account_arrives_with_its_name(self):
        self.assertTrue(db_operations.apply_synced_account_identity(
            PEER, "materva", stamp(), None, stamp(-60)))
        self.assertEqual(self.column(PEER, "alias"), "materva")

    def test_a_learned_account_carries_no_way_to_log_in(self):
        """The whole reason sharing accounts with someone else's node is safe.

        Password material is not in the frame and is not written here, so a
        node that learns an account -- or a peer that invents one -- cannot
        produce credentials that authenticate anywhere.
        """
        db_operations.apply_synced_account_identity(PEER, "materva", stamp())
        self.assertIsNone(self.column(PEER, "password_hash"))
        self.assertIsNone(self.column(PEER, "password_salt"))
        self.assertEqual(self.column(PEER, "sync_origin"), "peer")
        self.assertIsNone(db_operations.get_ssh_credentials("materva"))

    def test_a_local_account_is_never_marked_as_learned(self):
        self.account(LOCAL, "bacon")
        self.assertEqual(self.column(LOCAL, "sync_origin"), "local")

    def test_an_id_that_is_not_an_account_id_is_refused(self):
        """It becomes a table key and the tail of an 'ssh:<id>' node id, so a
        peer choosing arbitrary text here could collide with those."""
        for bogus in ("../../etc", "ssh:" + PEER, "short", "", "z" * 32):
            with self.subTest(bogus=bogus):
                self.assertFalse(db_operations.apply_synced_account_identity(
                    bogus, "someone", stamp()))
        count = db_operations.get_db_connection().execute(
            "SELECT COUNT(*) FROM accounts").fetchone()[0]
        self.assertEqual(count, 0)

    def test_nothing_is_learned_while_account_sync_is_switched_off(self):
        with mock.patch.object(utils, "is_account_sync_enabled", return_value=False):
            self.assertFalse(db_operations.apply_synced_account_identity(
                PEER, "materva", stamp()))
        self.assertIsNone(self.column(PEER, "alias"))


class AliasOwnershipTests(_DbCase):
    def test_a_peer_cannot_take_a_name_a_local_account_holds(self):
        """Claiming someone's name is how you get their mail, so the local
        owner keeps it and the peer's claim is dropped."""
        self.account(LOCAL, "bacon")
        db_operations.apply_synced_account_identity(PEER, "bacon", stamp())
        self.assertEqual(self.column(LOCAL, "alias"), "bacon")
        self.assertEqual(self.column(PEER, "alias"), "")
        self.assertEqual(db_operations.alias_owner("bacon"), LOCAL)

    def test_a_newer_claim_renames_the_account_that_owns_it(self):
        db_operations.apply_synced_account_identity(PEER, "oldname", stamp(-600))
        db_operations.apply_synced_account_identity(PEER, "newname", stamp())
        self.assertEqual(self.column(PEER, "alias"), "newname")

    def test_a_stale_claim_does_not_undo_a_newer_one(self):
        db_operations.apply_synced_account_identity(PEER, "current", stamp())
        db_operations.apply_synced_account_identity(PEER, "stale", stamp(-600))
        self.assertEqual(self.column(PEER, "alias"), "current")

    def test_a_stamp_from_the_future_is_refused(self):
        """Otherwise one frame stamped next year wins every comparison from
        now on, and no local correction can ever beat it."""
        db_operations.apply_synced_account_identity(PEER, "honest", stamp(-60))
        db_operations.apply_synced_account_identity(PEER, "pinned", stamp(86400))
        self.assertEqual(self.column(PEER, "alias"), "honest")

    def test_ordinary_clock_skew_still_works(self):
        db_operations.apply_synced_account_identity(PEER, "skewed", stamp(60))
        self.assertEqual(self.column(PEER, "alias"), "skewed")


class DeviceLinkTests(_DbCase):
    def setUp(self):
        super().setUp()
        db_operations.apply_synced_account_identity(PEER, "materva", stamp())

    def test_a_device_is_attached_to_the_account_that_owns_it(self):
        self.assertTrue(db_operations.apply_synced_account_link(
            "!04058ac8", PEER, "meshtastic", stamp()))
        self.assertEqual(db_operations.get_account_id_for_node("!04058ac8"), PEER)

    def test_a_peer_cannot_move_a_device_that_is_already_someone_elses(self):
        """The link is what routes a person's mail to a radio. If a peer could
        reassign it, any node on the broker could redirect that mail."""
        self.account(LOCAL, "bacon")
        conn = db_operations.get_db_connection()
        conn.execute("INSERT INTO linked_nodes (node_id, account_id, network, linked_at)"
                     " VALUES (?, ?, 'meshtastic', ?)", ("!04058ac8", LOCAL, stamp(-60)))
        conn.commit()
        self.assertFalse(db_operations.apply_synced_account_link(
            "!04058ac8", PEER, "meshtastic", stamp()))
        self.assertEqual(db_operations.get_account_id_for_node("!04058ac8"), LOCAL)

    def test_a_device_for_an_account_we_have_never_heard_of_is_refused(self):
        self.assertFalse(db_operations.apply_synced_account_link(
            "!deadbeef", OTHER, "meshtastic", stamp()))
        self.assertIsNone(db_operations.get_account_id_for_node("!deadbeef"))


class RoleAndConsentTests(_DbCase):
    def setUp(self):
        super().setUp()
        db_operations.apply_synced_account_identity(PEER, "materva", stamp())

    def test_a_peer_may_not_promote_an_account_past_the_ceiling(self):
        """An account role covers every device linked to it, so accepting this
        unchecked would grant more than the per-node role it mirrors."""
        db_operations.apply_synced_account_meta(PEER, 0, "", "developer", stamp())
        self.assertEqual(db_operations.get_account_role(PEER), db_operations.ROLE_USER)

    def test_a_role_within_the_ceiling_is_accepted(self):
        db_operations.apply_synced_account_meta(PEER, 0, "", "mod", stamp())
        self.assertEqual(db_operations.get_account_role(PEER), db_operations.ROLE_MOD)

    def test_a_ban_travels(self):
        db_operations.apply_synced_account_meta(PEER, 0, "", "banned", stamp())
        self.assertEqual(db_operations.get_account_role(PEER), db_operations.ROLE_BANNED)

    def test_relay_consent_travels_with_the_account(self):
        db_operations.apply_synced_account_link("!04058ac8", PEER, "meshtastic", stamp())
        self.assertFalse(db_operations.get_mail_relay_preference("!04058ac8"))
        db_operations.apply_synced_account_meta(PEER, 1, stamp(), "user", stamp())
        self.assertTrue(db_operations.get_mail_relay_preference("!04058ac8"))

    def test_a_stale_consent_frame_does_not_re_enable_relay(self):
        db_operations.apply_synced_account_meta(PEER, 1, stamp(-600), "user", "")
        db_operations.apply_synced_account_meta(PEER, 0, stamp(), "user", "")
        db_operations.apply_synced_account_meta(PEER, 1, stamp(-300), "user", "")
        self.assertEqual(self.column(PEER, "mail_relay_enabled"), 0)

    def test_meta_for_an_unknown_account_creates_nothing(self):
        """Identity is refused on its own terms; consent and privilege must
        not arrive behind a frame that was rejected."""
        self.assertFalse(db_operations.apply_synced_account_meta(
            OTHER, 1, stamp(), "mod", stamp()))
        self.assertIsNone(self.column(OTHER, "role"))


class SenderNumberTests(_DbCase):
    def test_a_peers_number_is_adopted_so_one_person_has_one_identity(self):
        """Scores, Zork saves and profiles are all keyed by this number. If it
        differed per node the same person would have a separate history on
        each one."""
        db_operations.apply_synced_account_identity(PEER, "materva", stamp(), 3781338615)
        self.assertEqual(self.column(PEER, "sender_num"), 3781338615)

    def test_a_number_already_in_use_here_is_not_stolen(self):
        """Two accounts sharing a number would share their saves and scores."""
        self.account(LOCAL, "bacon")
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE accounts SET sender_num = ? WHERE account_id = ?",
                     (3781338615, LOCAL))
        conn.commit()
        db_operations.apply_synced_account_identity(PEER, "materva", stamp(), 3781338615)
        self.assertIsNone(self.column(PEER, "sender_num"))
        self.assertEqual(self.column(LOCAL, "sender_num"), 3781338615)


class WhatGoesOnTheWireTests(_DbCase):
    def test_an_exported_account_carries_no_password_columns(self):
        self.account(LOCAL, "bacon")
        exported = db_operations.get_accounts_for_sync()
        self.assertEqual(len(exported), 1)
        for forbidden in ("password_hash", "password_salt", "password_created_at"):
            self.assertNotIn(forbidden, exported[0])
        self.assertNotIn("localhash", repr(exported[0]))

    def test_every_frame_fits_a_meshcore_packet(self):
        """160 bytes on MeshCore. An account id is 32 characters and an alias
        is capped at 20, so the worst case has to be checked, not assumed."""
        self.account(LOCAL, "x" * 20, created_at=stamp(), alias_updated_at=stamp())
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE accounts SET sender_num = ?, role = 'mod',"
                     " role_updated_at = ?, mail_relay_updated_at = ?"
                     " WHERE account_id = ?",
                     (4294967295, stamp(), stamp(), LOCAL))
        conn.commit()
        account = db_operations.get_accounts_for_sync()[0]
        link = {'node_id': 'ssh:' + LOCAL, 'account_id': LOCAL,
                'network': 'meshtastic', 'linked_at': stamp()}
        sent = []
        with mock.patch.object(utils, "_send_one_sync",
                               side_effect=lambda msg, *a: sent.append(msg)), \
             mock.patch.object(db_operations, "peer_supports", return_value=True):
            utils.send_account_to_bbs_nodes(account, ["peer"], object())
            utils.send_account_meta_to_bbs_nodes(account, ["peer"], object())
            utils.send_account_link_to_bbs_nodes(link, ["peer"], object())
        self.assertEqual(len(sent), 3)
        for frame in sent:
            with self.subTest(frame=frame.split("|")[0]):
                self.assertLessEqual(len(frame.encode("utf-8")), 160)

    def test_this_node_does_not_tell_the_fleet_who_its_admins_are(self):
        self.account(LOCAL, "bacon")
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE accounts SET role = 'developer', role_updated_at = ?"
                     " WHERE account_id = ?", (stamp(), LOCAL))
        conn.commit()
        sent = []
        with mock.patch.object(utils, "_send_one_sync",
                               side_effect=lambda msg, *a: sent.append(msg)), \
             mock.patch.object(db_operations, "peer_supports", return_value=True):
            db_operations.sync_accounts_to_nodes(["peer"], object(), force=True)
        meta = [f for f in sent if f.startswith("ACCTMETA|")]
        self.assertTrue(meta)
        for frame in meta:
            self.assertNotIn("developer", frame)


class DeliveryReceiptTests(_DbCase):
    """Every node queues a delivery when mail arrives, so two nodes that can
    both hear one radio would both send. A receipt stands the others down."""

    def queue(self, unique_id="m1", target="!04058ac8", state="pending"):
        conn = db_operations.get_db_connection()
        conn.execute(
            "INSERT INTO mail_dm_deliveries (mail_unique_id, recipient_account_id,"
            " target_node_id, state, created_at) VALUES (?, ?, ?, ?, ?)",
            (unique_id, PEER, target, state, stamp()))
        conn.commit()

    def state(self, unique_id="m1"):
        return db_operations.get_db_connection().execute(
            "SELECT state FROM mail_dm_deliveries WHERE mail_unique_id = ?",
            (unique_id,)).fetchone()[0]

    def test_a_receipt_stands_our_copy_down(self):
        self.queue()
        self.assertEqual(db_operations.mark_mail_dm_delivered_elsewhere(
            "m1", "!04058ac8", stamp()), 1)
        self.assertEqual(self.state(), "delivered_elsewhere")

    def test_a_receipt_does_not_rewrite_a_delivery_we_already_made(self):
        """Losing that record would not un-send the message, only hide it."""
        self.queue(state="delivered")
        self.assertEqual(db_operations.mark_mail_dm_delivered_elsewhere(
            "m1", "!04058ac8", stamp()), 0)
        self.assertEqual(self.state(), "delivered")

    def test_a_receipt_for_another_radio_leaves_ours_pending(self):
        self.queue()
        db_operations.mark_mail_dm_delivered_elsewhere("m1", "!somebodyelse", stamp())
        self.assertEqual(self.state(), "pending")

    def test_a_stood_down_delivery_is_no_longer_due(self):
        self.queue()
        db_operations.mark_mail_dm_delivered_elsewhere("m1", "!04058ac8", stamp())
        due = [d['mail_unique_id'] for d in db_operations.get_due_mail_dm_deliveries()]
        self.assertNotIn("m1", due)


if __name__ == "__main__":
    unittest.main()
