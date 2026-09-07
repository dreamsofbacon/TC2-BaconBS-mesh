"""Invite export and import, driven through the actual web routes.

tests/test_invite.py covers the bundle logic. This file covers the part
that can only go wrong once Flask, config.ini and the filesystem are
involved: that the download really is encrypted, that importing writes a
usable link without touching an existing one, and that the fleet key is
armed only when the box is ticked.
"""

import configparser
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import invite
import web_admin

PASSPHRASE = "a good long passphrase"
BROKER_PASSWORD = "s3cr3t-broker-pw"


class _Node:
    """A web admin with one configured MQTT link and a scratch config."""

    CONFIG = f"""[admin]
username = admin
password = pw

[fleet]
group = baconbbsvt
trusted_keys = fkf5f136:EXISTINGKEY
updates = auto

[mqtt1]
enabled = true
host = mqtt.example.net
port = 8884
tls = true
tls_insecure = false
username = client1
password = {BROKER_PASSWORD}
topic_prefix = baconbbsvt
local_id = Burlington-NNE
keepalive = 60

[sync_mqtt1]
bbs_nodes = mqtt:baconbbsvt:BaconBBS-VT2

[allow_list_mqtt1]
allowed_nodes = !0408b778
"""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="inviteweb-")
        self.config = os.path.join(self.dir, "config.ini")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(self.CONFIG)
        self.certs = os.path.join(self.dir, "certs")
        self.env = mock.patch.dict(os.environ, {
            "BBS_CONFIG_PATH": self.config,
            "BBS_MQTT_CERT_DIR": self.certs,
            "BBS_WEBGUI_SECRET": "test-secret",
        })
        self.env.start()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.app = web_admin.create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self._login()
        return self

    def __exit__(self, *exc):
        self.env.stop()
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection
        return False

    def _login(self):
        page = self.client.get("/login")
        self.client.post("/login", data={
            "username": "admin", "password": "pw",
            "csrf_token": self._token(page.get_data(as_text=True))})

    @staticmethod
    def _token(html):
        marker = 'name="csrf_token" value="'
        start = html.index(marker) + len(marker)
        return html[start:html.index('"', start)]

    def csrf(self, path="/settings"):
        return self._token(self.client.get(path).get_data(as_text=True))

    def export(self, **overrides):
        data = {"link_index": "1", "passphrase": PASSPHRASE,
                "csrf_token": self.csrf()}
        data.update(overrides)
        return self.client.post("/invite/export", data=data)

    def upload(self, blob, passphrase=PASSPHRASE):
        return self.client.post(
            "/invite/import",
            data={"invite_file": (io.BytesIO(blob), "x.bbsinvite"),
                  "passphrase": passphrase, "csrf_token": self.csrf()},
            content_type="multipart/form-data", follow_redirects=False)

    def add_ca_file(self, text):
        """Point mqtt1 at a real CA file, so 'include certificates' has
        something to include -- without one, unticking the box and leaving
        it ticked produce the same empty result."""
        path = os.path.join(self.dir, "ca.pem")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        config = self.read_config()
        config.set("mqtt1", "tls_ca_certs", path)
        with open(self.config, "w", encoding="utf-8") as handle:
            config.write(handle)
        return path

    def read_config(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.config, encoding="utf-8")
        return parser


class ExportTests(unittest.TestCase):
    def test_the_download_is_encrypted(self):
        with _Node() as node:
            response = node.export()
            self.assertEqual(response.status_code, 200)
            body = response.get_data()
            self.assertNotIn(BROKER_PASSWORD.encode(), body)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertIn(".bbsinvite", response.headers["Content-Disposition"])

    def test_it_round_trips_through_the_passphrase(self):
        with _Node() as node:
            payload = invite.decrypt_payload(node.export().get_data(), PASSPHRASE)
            self.assertEqual(payload["link"]["host"], "mqtt.example.net")
            self.assertEqual(payload["link"]["password"], BROKER_PASSWORD)
            self.assertEqual(payload["inviter_local_id"], "Burlington-NNE")

    def test_the_fleet_key_is_left_out_unless_asked_for(self):
        with _Node() as node:
            payload = invite.decrypt_payload(node.export().get_data(), PASSPHRASE)
            self.assertNotIn("fleet", payload)

            payload = invite.decrypt_payload(
                node.export(include_fleet="true").get_data(), PASSPHRASE)
            self.assertEqual(payload["fleet"]["trusted_keys"],
                             ["fkf5f136:EXISTINGKEY"])

    def test_an_empty_passphrase_is_refused(self):
        with _Node() as node:
            response = node.export(passphrase="", **{})
            self.assertEqual(response.status_code, 302)
            self.assertIn("#invite", response.headers["Location"])


class ImportTests(unittest.TestCase):
    def _bundle(self, node, **overrides):
        return node.export(**overrides).get_data()

    def _review(self, node, blob):
        response = node.upload(blob)
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"]

    def test_a_wrong_passphrase_does_not_reach_the_review(self):
        with _Node() as node:
            response = node.upload(self._bundle(node), passphrase="nope")
            self.assertIn("#invite", response.headers["Location"])
            self.assertNotIn("/invite/review", response.headers["Location"])

    def test_the_review_names_what_will_change(self):
        with _Node() as node:
            page = node.client.get(self._review(node, self._bundle(node)))
            body = page.get_data(as_text=True)
            self.assertIn("mqtt.example.net", body)
            self.assertIn("baconbbsvt", body)
            self.assertIn("Nothing has changed yet", body)

    def test_the_review_never_shows_the_broker_password(self):
        with _Node() as node:
            page = node.client.get(self._review(node, self._bundle(node)))
            self.assertNotIn(BROKER_PASSWORD, page.get_data(as_text=True))

    def _apply(self, node, blob, **form):
        location = self._review(node, blob)
        token = location.rsplit("/", 1)[-1]
        data = {"local_id": "forgecam-2",
                "csrf_token": node._token(
                    node.client.get(location).get_data(as_text=True))}
        data.update(form)
        return node.client.post(f"/invite/apply/{token}", data=data,
                                follow_redirects=False)

    def test_importing_adds_a_new_link(self):
        with _Node() as node:
            # A second node's bundle: same broker, different topic, so it is
            # not seen as one this node already has.
            blob = self._bundle(node)
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            blob = invite.encrypt_payload(payload, PASSPHRASE)

            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                response = self._apply(node, blob)
            self.assertEqual(response.status_code, 302)
            config = node.read_config()
            self.assertTrue(config.has_section("mqtt2"))
            self.assertEqual(config.get("mqtt2", "host"), "mqtt.example.net")
            self.assertEqual(config.get("mqtt2", "local_id"), "forgecam-2")
            self.assertEqual(config.get("mqtt2", "password"), BROKER_PASSWORD)

    def test_the_existing_link_is_untouched(self):
        with _Node() as node:
            blob = self._bundle(node)
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            blob = invite.encrypt_payload(payload, PASSPHRASE)
            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                self._apply(node, blob)
            config = node.read_config()
            self.assertEqual(config.get("mqtt1", "local_id"), "Burlington-NNE")
            self.assertEqual(config.get("sync_mqtt1", "bbs_nodes"),
                             "mqtt:baconbbsvt:BaconBBS-VT2")

    def test_a_broker_and_topic_already_present_changes_nothing(self):
        with _Node() as node:
            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                self._apply(node, self._bundle(node))
            self.assertFalse(node.read_config().has_section("mqtt2"))

    def test_updates_are_not_armed_without_the_tick(self):
        """The whole security posture of the feature, through the real
        route: a file alone must not let its sender run code here."""
        with _Node() as node:
            blob = self._bundle(node, include_fleet="true")
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            payload["fleet"]["trusted_keys"] = ["fkNEW111:BRANDNEWKEY"]
            blob = invite.encrypt_payload(payload, PASSPHRASE)

            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                self._apply(node, blob)
            keys = node.read_config().get("fleet", "trusted_keys")
            self.assertNotIn("fkNEW111", keys)
            self.assertIn("fkf5f136", keys)

    def test_the_tick_arms_updates_and_keeps_the_existing_key(self):
        with _Node() as node:
            blob = self._bundle(node, include_fleet="true")
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            payload["fleet"]["trusted_keys"] = ["fkNEW111:BRANDNEWKEY"]
            blob = invite.encrypt_payload(payload, PASSPHRASE)

            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                self._apply(node, blob, arm_updates="true")
            keys = node.read_config().get("fleet", "trusted_keys")
            self.assertIn("fkNEW111", keys)
            self.assertIn("fkf5f136", keys)

    def test_the_link_is_brought_up_without_a_restart(self):
        with _Node() as node:
            blob = self._bundle(node)
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            blob = invite.encrypt_payload(payload, PASSPHRASE)
            called = []
            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: called.append(True)):
                self._apply(node, blob)
            self.assertTrue(called, "imported link would not connect until a restart")

    def test_a_bad_local_id_returns_to_the_review(self):
        with _Node() as node:
            blob = self._bundle(node)
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            blob = invite.encrypt_payload(payload, PASSPHRASE)
            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                response = self._apply(node, blob, local_id="not valid!")
            self.assertIn("/invite/review", response.headers["Location"])
            self.assertFalse(node.read_config().has_section("mqtt2"))

    def test_an_invite_cannot_be_applied_twice(self):
        """The pending invite holds a broker password and possibly a private
        key. Once used it must be gone, so a stale browser tab -- or a back
        button -- cannot replay it into another link."""
        with _Node() as node:
            blob = self._bundle(node)
            payload = invite.decrypt_payload(blob, PASSPHRASE)
            payload["link"]["topic_prefix"] = "another-fleet"
            blob = invite.encrypt_payload(payload, PASSPHRASE)

            location = self._review(node, blob)
            token = location.rsplit("/", 1)[-1]
            csrf = node._token(node.client.get(location).get_data(as_text=True))
            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                first = node.client.post(f"/invite/apply/{token}", data={
                    "local_id": "forgecam-2", "csrf_token": csrf})
                second = node.client.post(f"/invite/apply/{token}", data={
                    "local_id": "forgecam-3", "csrf_token": csrf})
            self.assertIn("#mqtt", first.headers["Location"])
            self.assertIn("#invite", second.headers["Location"])
            self.assertFalse(node.read_config().has_section("mqtt3"),
                             "a replayed invite added a second link")

    def test_an_expired_token_is_refused(self):
        with _Node() as node:
            response = node.client.post(
                "/invite/apply/nosuchtoken",
                data={"local_id": "x", "csrf_token": node.csrf()},
                follow_redirects=False)
            self.assertIn("#invite", response.headers["Location"])


class CertificateTests(unittest.TestCase):
    CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"

    def test_certificate_contents_travel_and_are_written(self):
        """config.ini stores paths, which mean nothing on another machine."""
        with _Node() as node:
            node.add_ca_file(self.CA)

            payload = invite.decrypt_payload(
                node.export(include_certs="true").get_data(), PASSPHRASE)
            self.assertEqual(payload["certs"]["tls_ca_certs"], self.CA)

            payload["link"]["topic_prefix"] = "another-fleet"
            blob = invite.encrypt_payload(payload, PASSPHRASE)
            location = node.upload(blob).headers["Location"]
            token = location.rsplit("/", 1)[-1]
            with mock.patch.object(web_admin, "request_links_reload_trigger",
                                   lambda *a, **k: None):
                node.client.post(f"/invite/apply/{token}", data={
                    "local_id": "forgecam-2",
                    "csrf_token": node._token(
                        node.client.get(location).get_data(as_text=True))})

            written = node.read_config().get("mqtt2", "tls_ca_certs")
            self.assertTrue(os.path.isfile(written), written)
            with open(written, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), self.CA)

    def test_certificates_can_be_left_out(self):
        with _Node() as node:
            node.add_ca_file(self.CA)
            with_certs = invite.decrypt_payload(
                node.export(include_certs="true").get_data(), PASSPHRASE)
            self.assertEqual(with_certs["certs"]["tls_ca_certs"], self.CA)

            without = invite.decrypt_payload(
                node.export(include_certs="").get_data(), PASSPHRASE)
            self.assertEqual(without["certs"], {})


if __name__ == "__main__":
    unittest.main()
