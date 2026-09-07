"""Invite bundles: hand someone a file, and their node joins your mesh.

The three things that must hold no matter how convenient the feature gets:
the broker password is never in the clear, an import cannot break a working
node, and the fleet signing key is never armed without being chosen.
"""

import configparser
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import invite

LINK = {
    "host": "mqtt.example.net", "port": "8884", "tls": True,
    "tls_insecure": False, "username": "client1", "password": "s3cr3t-broker",
    "topic_prefix": "baconbbsvt", "keepalive": "60",
}
CERTS = {
    "tls_ca_certs": "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----",
    "tls_keyfile": "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
}
FLEET = {"group": "baconbbsvt", "updates": "auto",
         "trusted_keys": ["fkec622a:AAAApublic"]}


def make_payload(**overrides):
    payload = invite.build_payload(
        LINK, inviter_local_id="Burlington-NNE", created_by="Burlington",
        bbs_name="Bacon BBS", certs=CERTS,
        sync_nodes=["mqtt:baconbbsvt:BaconBBS-VT2"], allowed_nodes=["!0408b778"],
        fleet=FLEET)
    payload.update(overrides)
    return payload


class EncryptionTests(unittest.TestCase):
    def test_the_broker_password_is_not_in_the_file(self):
        """These files travel through Discord and email."""
        blob = invite.encrypt_payload(make_payload(), "correct horse")
        self.assertNotIn(b"s3cr3t-broker", blob)

    def test_the_private_key_is_not_in_the_file(self):
        blob = invite.encrypt_payload(make_payload(), "correct horse")
        self.assertNotIn(b"BEGIN PRIVATE KEY", blob)

    def test_a_round_trip_preserves_everything(self):
        blob = invite.encrypt_payload(make_payload(), "correct horse")
        back = invite.decrypt_payload(blob, "correct horse")
        self.assertEqual(back["link"], make_payload()["link"])
        self.assertEqual(back["certs"], CERTS)
        self.assertEqual(back["fleet"]["trusted_keys"], FLEET["trusted_keys"])

    def test_the_wrong_passphrase_is_told_apart_from_a_wrong_file(self):
        blob = invite.encrypt_payload(make_payload(), "correct horse")
        with self.assertRaises(invite.InviteError) as caught:
            invite.decrypt_payload(blob, "battery staple")
        self.assertIn("passphrase", str(caught.exception))

        with self.assertRaises(invite.InviteError) as caught:
            invite.decrypt_payload(b"just some text", "correct horse")
        self.assertIn("does not look like", str(caught.exception))

    def test_tampering_is_detected(self):
        """Fernet authenticates; a flipped byte must not decrypt."""
        blob = invite.encrypt_payload(make_payload(), "correct horse")
        envelope = json.loads(blob.decode())
        body = list(envelope["body"])
        body[40] = "A" if body[40] != "A" else "B"
        envelope["body"] = "".join(body)
        with self.assertRaises(invite.InviteError):
            invite.decrypt_payload(json.dumps(envelope).encode(), "correct horse")

    def test_an_empty_passphrase_is_refused(self):
        with self.assertRaises(invite.InviteError):
            invite.encrypt_payload(make_payload(), "   ")

    def test_a_newer_format_says_so_rather_than_failing_obscurely(self):
        blob = invite.encrypt_payload(make_payload(), "pw")
        envelope = json.loads(blob.decode())
        envelope["version"] = invite.VERSION + 1
        with self.assertRaises(invite.InviteError) as caught:
            invite.decrypt_payload(json.dumps(envelope).encode(), "pw")
        self.assertIn("newer version", str(caught.exception))

    def test_an_oversized_file_is_refused_before_it_is_parsed(self):
        """A VALID bundle, padded past the cap. Random bytes would be
        rejected by the JSON parse whether or not the cap exists, so the
        test would pass with the cap deleted."""
        blob = invite.encrypt_payload(make_payload(), "pw")
        envelope = json.loads(blob.decode())
        envelope["padding"] = "x" * invite.MAX_BUNDLE_BYTES
        padded = json.dumps(envelope).encode()
        self.assertGreater(len(padded), invite.MAX_BUNDLE_BYTES)
        with self.assertRaises(invite.InviteError) as caught:
            invite.decrypt_payload(padded, "pw")
        self.assertIn("too large", str(caught.exception))

    def test_a_bundle_with_no_broker_is_refused(self):
        payload = make_payload()
        payload["link"] = dict(payload["link"], host="")
        blob = invite.encrypt_payload(payload, "pw")
        with self.assertRaises(invite.InviteError) as caught:
            invite.decrypt_payload(blob, "pw")
        self.assertIn("broker address", str(caught.exception))

    def test_the_kdf_is_not_trivially_cheap(self):
        """A weak KDF makes the encryption decorative: broker passwords are
        short and these files are shared."""
        self.assertGreaterEqual(invite.KDF_ITERATIONS, 100_000)


class IdentityTests(unittest.TestCase):
    def test_the_inviters_own_name_is_not_copied(self):
        """Two nodes sharing a local_id collide on the broker, each reading
        the other's traffic as its own."""
        self.assertNotIn("local_id", invite.LINK_FIELDS)
        self.assertNotIn("client_id", invite.LINK_FIELDS)

    def test_the_importer_syncs_with_the_inviter_and_its_peers(self):
        peers = invite.peer_ids_for_importer(make_payload())
        self.assertEqual(peers, ["mqtt:baconbbsvt:Burlington-NNE",
                                 "mqtt:baconbbsvt:BaconBBS-VT2"])

    def test_a_local_id_that_could_break_config_is_refused(self):
        for bad in ("", "   ", "has space", "semi;colon", "a" * 65,
                    "new\nline", "bracket[]"):
            with self.subTest(value=bad):
                with self.assertRaises(invite.InviteError):
                    invite.validate_local_id(bad)

    def test_an_ordinary_name_is_accepted(self):
        self.assertEqual(invite.validate_local_id("  forge-cam_2.1  "),
                         "forge-cam_2.1")


class MergeTests(unittest.TestCase):
    EXISTING = [
        {"index": 1, "host": "192.168.1.134", "port": "1883",
         "topic_prefix": "baconbbs"},
        {"index": 2, "host": "mqtt.example.net", "port": "8884",
         "topic_prefix": "other-fleet"},
    ]

    def test_a_new_slot_is_allocated(self):
        self.assertEqual(invite.next_free_index([1, 2]), 3)
        self.assertEqual(invite.next_free_index([1, 3]), 2)
        self.assertEqual(invite.next_free_index([]), 1)

    def test_the_same_broker_on_another_topic_is_not_a_duplicate(self):
        """One broker commonly carries several unrelated fleets on different
        topic prefixes -- refusing on host alone would block a real invite."""
        self.assertIsNone(invite.find_matching_link(make_payload(), self.EXISTING))

    def test_the_same_broker_and_topic_is_reported(self):
        existing = self.EXISTING + [
            {"index": 3, "host": "MQTT.Example.NET", "port": "8884",
             "topic_prefix": "baconbbsvt"}]
        self.assertEqual(invite.find_matching_link(make_payload(), existing), 3)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.config = configparser.ConfigParser(interpolation=None)
        self.config.optionxform = str
        self.config.add_section("mqtt1")
        self.config.set("mqtt1", "host", "192.168.1.134")
        self.config.set("mqtt1", "local_id", "bbs-main")
        self.config.add_section("fleet")
        self.config.set("fleet", "group", "baconbbsvt")
        self.config.set("fleet", "trusted_keys", "fkf5f136:EXISTING")
        self.config.set("fleet", "updates", "auto")
        self.written = []

    def _writer(self, index, role, text):
        self.written.append((index, role))
        return f"/data/mqtt-certs/mqtt{index}/{role}.pem"

    def _apply(self, **kwargs):
        payload = kwargs.pop("payload", make_payload())
        return invite.apply_invite(
            self.config, payload, local_id="forgecam-2", index=2,
            cert_writer=self._writer, **kwargs)

    def test_it_writes_the_new_link(self):
        self._apply()
        self.assertEqual(self.config.get("mqtt2", "host"), "mqtt.example.net")
        self.assertEqual(self.config.get("mqtt2", "password"), "s3cr3t-broker")
        self.assertEqual(self.config.get("mqtt2", "enabled"), "true")

    def test_booleans_are_written_as_config_text_not_python(self):
        self._apply()
        self.assertEqual(self.config.get("mqtt2", "tls"), "true")
        self.assertEqual(self.config.get("mqtt2", "tls_insecure"), "false")

    def test_this_node_gets_its_own_name_on_the_link(self):
        self._apply()
        self.assertEqual(self.config.get("mqtt2", "local_id"), "forgecam-2")

    def test_an_existing_link_is_untouched(self):
        self._apply()
        self.assertEqual(self.config.get("mqtt1", "host"), "192.168.1.134")
        self.assertEqual(self.config.get("mqtt1", "local_id"), "bbs-main")

    def test_certificates_are_written_and_pointed_at(self):
        changed = self._apply()
        self.assertEqual(sorted(changed["certs"]),
                         ["tls_ca_certs", "tls_keyfile"])
        self.assertEqual(self.config.get("mqtt2", "tls_keyfile"),
                         "/data/mqtt-certs/mqtt2/tls_keyfile.pem")
        self.assertEqual([r for _, r in self.written],
                         ["tls_ca_certs", "tls_keyfile"])

    def test_the_peer_list_is_written(self):
        self._apply()
        self.assertEqual(self.config.get("sync_mqtt2", "bbs_nodes"),
                         "mqtt:baconbbsvt:Burlington-NNE,mqtt:baconbbsvt:BaconBBS-VT2")
        self.assertEqual(self.config.get("allow_list_mqtt2", "allowed_nodes"),
                         "!0408b778")

    def test_updates_are_not_armed_by_default(self):
        """The whole point. Importing a file must not, on its own, let the
        sender run any commit they choose on this node."""
        changed = self._apply()
        self.assertFalse(changed["armed_updates"])
        self.assertEqual(self.config.get("fleet", "trusted_keys"),
                         "fkf5f136:EXISTING")

    def test_arming_adds_the_key_without_replacing_the_existing_one(self):
        """Wholesale rewriting of trusted_keys is the leading theory for the
        day both live nodes stopped trusting a signing key."""
        changed = self._apply(arm_updates=True)
        self.assertTrue(changed["armed_updates"])
        self.assertEqual(changed["keys_added"], ["fkec622a"])
        keys = self.config.get("fleet", "trusted_keys")
        self.assertIn("fkf5f136:EXISTING", keys)
        self.assertIn("fkec622a:AAAApublic", keys)

    def test_a_key_already_trusted_is_not_added_twice(self):
        self.config.set("fleet", "trusted_keys",
                        "fkf5f136:EXISTING,fkec622a:AAAApublic")
        changed = self._apply(arm_updates=True)
        self.assertEqual(changed["keys_added"], [])
        self.assertEqual(self.config.get("fleet", "trusted_keys").count("fkec622a"), 1)

    def test_arming_for_a_different_group_is_refused(self):
        """Changing the group silently moves this node into someone else's
        fleet, where their signed target -- not yours -- decides what it
        runs."""
        self.config.set("fleet", "group", "someone-elses-fleet")
        summary = invite.summarize(
            make_payload(), current_group="someone-elses-fleet")
        self.assertTrue(summary["group_conflict"])
        self.assertFalse(summary["can_arm_updates"])
        with self.assertRaises(invite.InviteError):
            self._apply(arm_updates=True, summary=summary)
        self.assertEqual(self.config.get("fleet", "group"), "someone-elses-fleet")

    def test_a_bundle_with_no_fleet_block_cannot_arm(self):
        payload = make_payload()
        payload.pop("fleet")
        summary = invite.summarize(payload)
        self.assertFalse(summary["can_arm_updates"])
        with self.assertRaises(invite.InviteError):
            self._apply(payload=payload, arm_updates=True, summary=summary)

    def test_a_bad_local_id_never_reaches_config(self):
        with self.assertRaises(invite.InviteError):
            invite.apply_invite(self.config, make_payload(), local_id="bad id",
                                index=2, cert_writer=self._writer)
        self.assertFalse(self.config.has_section("mqtt2"))


class SummaryTests(unittest.TestCase):
    def test_it_names_what_the_operator_is_agreeing_to(self):
        summary = invite.summarize(make_payload(), current_group="baconbbsvt",
                                   trusted_keys=["fkf5f136:EXISTING"])
        self.assertEqual(summary["host"], "mqtt.example.net")
        self.assertEqual(summary["topic_prefix"], "baconbbsvt")
        self.assertTrue(summary["has_credentials"])
        self.assertEqual(summary["new_keys"], ["fkec622a:AAAApublic"])
        self.assertTrue(summary["can_arm_updates"])

    def test_it_flags_a_shared_private_key(self):
        """The recipient will authenticate to the broker as the sender."""
        self.assertTrue(invite.summarize(make_payload())["shares_private_key"])

    def test_a_key_already_trusted_is_not_shown_as_new(self):
        summary = invite.summarize(make_payload(),
                                   trusted_keys=["fkec622a:AAAApublic"])
        self.assertEqual(summary["new_keys"], [])
        self.assertEqual(summary["known_keys"], ["fkec622a:AAAApublic"])

    def test_an_unenrolled_node_can_arm(self):
        summary = invite.summarize(make_payload(), current_group="")
        self.assertFalse(summary["group_conflict"])
        self.assertTrue(summary["can_arm_updates"])


if __name__ == "__main__":
    unittest.main()
