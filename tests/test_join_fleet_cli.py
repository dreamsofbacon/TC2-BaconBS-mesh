"""Joining a fleet from the command line, with one invite file.

The web admin has done this for a while, but a fresh node usually has no
browser pointed at it yet, and hand-copying a broker, TLS material,
credentials, a topic, a peer list and a key into config.ini is where this
fleet's worst deployment went wrong.

Two rules carried over from the web path, and both are refusals:
the signing key is never armed without being asked, and the guest keeps the
name the invite gave it unless told otherwise -- a different name leaves the
node asking peers that have never heard of it.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import invite as invite_mod
import join_fleet

TOPIC = "baconbbsvt"
PASSPHRASE = "a-long-enough-passphrase"
LINK = {"host": "mqtt.example.invalid", "port": "8884", "topic_prefix": TOPIC,
        "local_id": "bbs-main", "username": "client1", "password": "secret",
        "tls": "true"}
KEY = "fkec622a:0hGvExa6i9yRn-kdbW4Kn6FHMfurPdmYeTCoud4vbuc"


class _Join(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.config_path = self.folder / "config.ini"
        self.config_path.write_text("[bbs]\nname = Fresh\n", encoding="utf-8")
        self.invite_path = self.folder / "fleet.bbsinvite"

    def write_invite(self, guest="VPS-Public", fleet=True):
        payload = invite_mod.build_payload(
            LINK, inviter_local_id="bbs-main", guest_local_id=guest,
            sync_nodes=[f"mqtt:{TOPIC}:VT2"],
            fleet={"group": TOPIC, "updates": "auto",
                   "trusted_keys": [KEY]} if fleet else None)
        self.invite_path.write_bytes(
            invite_mod.encrypt_payload(payload, PASSPHRASE))

    def run_join(self, *extra):
        argv = ["join_fleet.py", str(self.invite_path),
                "--passphrase", PASSPHRASE, "--config", str(self.config_path)]
        argv.extend(extra)
        old = sys.argv
        sys.argv = argv
        try:
            return join_fleet.main()
        finally:
            sys.argv = old

    def config(self):
        import configparser
        parser = configparser.ConfigParser()
        parser.read(self.config_path)
        return parser


class JoiningTests(_Join):
    def test_the_link_is_written(self):
        self.write_invite()
        self.assertEqual(0, self.run_join())
        config = self.config()
        self.assertEqual("mqtt.example.invalid", config.get("mqtt1", "host"))
        self.assertEqual(TOPIC, config.get("mqtt1", "topic_prefix"))

    def test_the_name_comes_from_the_invite(self):
        self.write_invite(guest="VPS-Public")
        self.run_join()
        self.assertEqual("VPS-Public", self.config().get("mqtt1", "local_id"))

    def test_every_peer_is_written(self):
        self.write_invite()
        self.run_join()
        peers = self.config().get("sync_mqtt1", "bbs_nodes")
        self.assertIn(f"mqtt:{TOPIC}:bbs-main", peers)
        self.assertIn(f"mqtt:{TOPIC}:VT2", peers)

    def test_a_chosen_name_overrides_the_invite(self):
        self.write_invite(guest="VPS-Public")
        self.run_join("--name", "Other-Name")
        self.assertEqual("Other-Name", self.config().get("mqtt1", "local_id"))

    def test_an_unnamed_invite_without_a_name_is_refused(self):
        """Rather than invent one that no peer knows."""
        self.write_invite(guest="")
        self.assertEqual(1, self.run_join())
        self.assertFalse(self.config().has_section("mqtt1"))

    def test_the_old_config_is_kept(self):
        self.write_invite()
        self.run_join()
        backup = self.config_path.with_suffix(".ini.invite-backup")
        self.assertTrue(backup.exists())
        self.assertIn("Fresh", backup.read_text(encoding="utf-8"))

    def test_an_existing_link_is_not_overwritten(self):
        self.config_path.write_text(
            "[bbs]\nname = Fresh\n[mqtt1]\nhost = already.here\n",
            encoding="utf-8")
        self.write_invite()
        self.run_join()
        config = self.config()
        self.assertEqual("already.here", config.get("mqtt1", "host"))
        self.assertEqual("mqtt.example.invalid", config.get("mqtt2", "host"))


class TheJoinedConfigStartsTests(_Join):
    """A joined node has to boot. The first version wrote every link field,
    blank ones included, and `tls_insecure =` -- an empty value, which
    configparser's fallback does not cover -- raised ValueError while the
    server was reading its config. The BBS crash-looped at startup on a node
    that had just been joined to a fleet and had nobody watching it."""

    def test_no_blank_values_are_written(self):
        self.write_invite()
        self.run_join()
        for name, value in self.config().items("mqtt1"):
            with self.subTest(option=name):
                self.assertNotEqual("", value.strip(),
                                    f"{name} was written empty")

    def test_the_link_section_parses_the_way_the_server_reads_it(self):
        self.write_invite()
        self.run_join()
        import config_init
        settings = config_init._read_mqtt_settings(self.config()["mqtt1"])
        self.assertEqual("mqtt.example.invalid", settings["host"])
        self.assertEqual(8884, settings["port"])
        self.assertTrue(settings["tls"])
        self.assertFalse(settings["tls_insecure"])

    def test_a_hand_written_blank_is_survived_too(self):
        """Not only invites: anyone can leave a value empty in config.ini."""
        import configparser
        import config_init
        parser = configparser.ConfigParser()
        parser.read_string(
            "[mqtt1]\nhost = h\ntls_insecure =\nkeepalive =\npublish_status =\n")
        settings = config_init._read_mqtt_settings(parser["mqtt1"])
        self.assertFalse(settings["tls_insecure"])
        self.assertEqual(60, settings["keepalive"])
        self.assertTrue(settings["publish_status"])

    def test_junk_is_survived_and_reported(self):
        """A typo in a config file is not a reason to take the node off
        the air."""
        import configparser
        import config_init
        parser = configparser.ConfigParser()
        parser.read_string(
            "[mqtt1]\nhost = h\ntls = banana\nport = soon\n")
        settings = config_init._read_mqtt_settings(parser["mqtt1"])
        self.assertFalse(settings["tls"])
        self.assertEqual(1883, settings["port"])


class ArmingIsAlwaysAskedForTests(_Join):
    """The bundle carries a signing key. Whoever holds its private half can
    make this node run any commit."""

    def test_updates_are_not_armed_by_default(self):
        self.write_invite()
        self.run_join()
        config = self.config()
        self.assertNotIn(KEY, config.get("fleet", "trusted_keys", fallback=""))

    def test_they_are_armed_when_asked(self):
        self.write_invite()
        self.run_join("--arm-updates")
        config = self.config()
        self.assertIn(KEY.split(":")[0], config.get("fleet", "trusted_keys", fallback=""))
        self.assertEqual(TOPIC, config.get("fleet", "group", fallback=""))

    def test_a_wrong_passphrase_changes_nothing(self):
        self.write_invite()
        argv = ["join_fleet.py", str(self.invite_path), "--passphrase", "wrong",
                "--config", str(self.config_path)]
        old = sys.argv
        sys.argv = argv
        try:
            self.assertEqual(1, join_fleet.main())
        finally:
            sys.argv = old
        self.assertFalse(self.config().has_section("mqtt1"))


if __name__ == "__main__":
    unittest.main()
