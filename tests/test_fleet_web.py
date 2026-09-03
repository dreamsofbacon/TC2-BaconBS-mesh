"""The Fleet page and its paste box.

The box is the one place a human hands the node an instruction, and the only
thing in front of it is the web admin password -- which is plaintext in
config.ini and defaults to "change-me". So the paste is NOT trusted because
it arrived through an authenticated session: it is verified exactly as if it
had come off the air. This endpoint can relay authority, never mint it.
"""
import base64
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import db_operations
import fleet_update
import web_admin

GROUP = "baconbbsvt"
COMMIT = "1234abcd" * 5


class _Node:
    """A web admin whose node trusts one key, with a scratch database."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="fleetweb-")
        self.config = os.path.join(self.dir, "config.ini")
        self.trigger = os.path.join(self.dir, "apply_update.trigger")
        self.private, self.public, self.key_id = fleet_update.generate_keypair()
        self.entry = fleet_update.public_key_entry(self.public)
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(
                "[admin]\nusername = admin\npassword = pw\n\n"
                f"[fleet]\ngroup = {GROUP}\ntrusted_keys = {self.entry}\n"
                "updates = auto\n")
        self.env = mock.patch.dict(os.environ, {
            "BBS_FLEET_APPLY_TRIGGER_PATH": self.trigger,
            "BBS_CONFIG_PATH": self.config,
            "BBS_WEBGUI_SECRET": "test-secret",
        })
        self.env.start()
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        # create_app reads BBS_CONFIG_PATH from the environment, which the
        # patch above has already pointed at this scratch config.
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
        token = self._token(page.get_data(as_text=True))
        self.client.post("/login", data={
            "username": "admin", "password": "pw", "csrf_token": token})

    @staticmethod
    def _token(html):
        marker = 'name="csrf_token" value="'
        start = html.index(marker) + len(marker)
        return html[start:html.index('"', start)]

    def instruction(self, commit=COMMIT, group=GROUP, private=None):
        payload = fleet_update.build_payload(
            group, commit, "0.1.999", self.key_id)
        return fleet_update.encode_instruction(
            payload, fleet_update.sign_payload(payload, private or self.private))

    def paste(self, blob):
        page = self.client.get("/fleet")
        token = self._token(page.get_data(as_text=True))
        return self.client.post("/fleet/apply", data={
            "instruction": blob, "csrf_token": token}, follow_redirects=True)


class PageTests(unittest.TestCase):
    def test_the_page_renders(self):
        with _Node() as node:
            page = node.client.get("/fleet")
            self.assertEqual(page.status_code, 200)
            body = page.get_data(as_text=True)
            self.assertIn("Fleet", body)
            self.assertIn(node.key_id, body)

    def test_page_shows_key_generator_and_setup_directions(self):
        with _Node() as node:
            page = node.client.get("/fleet")
            body = page.get_data(as_text=True)
            self.assertIn("Generate and download key", body)
            self.assertIn("fleet-key", body)
            self.assertIn("scripts/fleet_sign.py", body)
            self.assertIn("js/fleet.js", body)
            self.assertIn("plain HTTP", body)

    def test_it_requires_a_login(self):
        with _Node() as node:
            node.client.get("/logout")
            page = node.client.get("/fleet", follow_redirects=False)
            self.assertIn(page.status_code, (301, 302))


class PasteBoxTests(unittest.TestCase):
    def test_a_genuine_instruction_is_stored(self):
        with _Node() as node:
            node.paste(node.instruction())
            target = db_operations.get_fleet_target(GROUP)
            self.assertIsNotNone(target)
            self.assertEqual(target["commit"], COMMIT)

    def test_storing_one_asks_the_server_to_apply_it(self):
        with _Node() as node:
            node.paste(node.instruction())
            self.assertTrue(os.path.exists(node.trigger))

    def test_a_forged_instruction_is_refused(self):
        """An authenticated session does not make a paste trustworthy."""
        with _Node() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            page = node.paste(node.instruction(private=attacker))
            self.assertIsNone(db_operations.get_fleet_target(GROUP))
            self.assertIn("rejected", page.get_data(as_text=True).lower())

    def test_a_tampered_commit_is_refused(self):
        with _Node() as node:
            payload, signature = fleet_update.decode_instruction(node.instruction())
            payload["c"] = "f" * 40
            node.paste(fleet_update.encode_instruction(payload, signature))
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_an_instruction_for_another_group_is_refused(self):
        with _Node() as node:
            node.paste(node.instruction(group="not-my-fleet"))
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_an_empty_paste_says_so_rather_than_failing(self):
        with _Node() as node:
            page = node.paste("   ")
            self.assertIn("paste a signed instruction",
                          page.get_data(as_text=True).lower())

    def test_garbage_does_not_raise(self):
        with _Node() as node:
            page = node.paste("this is not a blob")
            self.assertEqual(page.status_code, 200)
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_a_replay_is_refused(self):
        with _Node() as node:
            blob = node.instruction()
            node.paste(blob)
            first = db_operations.get_fleet_target(GROUP)["issued_at"]
            node.paste(blob)
            self.assertEqual(
                db_operations.get_fleet_target(GROUP)["issued_at"], first)

    def test_the_post_is_csrf_protected(self):
        """It is a remote-code-execution endpoint reachable from a browser."""
        with _Node() as node:
            response = node.client.post(
                "/fleet/apply", data={"instruction": node.instruction()})
            self.assertEqual(response.status_code, 403)
            self.assertIsNone(db_operations.get_fleet_target(GROUP))


class LocalOptOutTests(unittest.TestCase):
    def test_updates_off_refuses_even_a_genuine_paste(self):
        """The local override has to hold against the GUI too, or a node
        being debugged can be yanked by whoever is logged in."""
        with _Node() as node:
            with open(node.config, "w", encoding="utf-8") as handle:
                handle.write(
                    "[admin]\nusername = admin\npassword = pw\n\n"
                    f"[fleet]\ngroup = {GROUP}\ntrusted_keys = {node.entry}\n"
                    "updates = off\n")
            page = node.paste(node.instruction())
            self.assertIsNone(db_operations.get_fleet_target(GROUP))
            self.assertIn("off", page.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()


class ConfigFormTests(unittest.TestCase):
    """group, updates and pin can only scope or disable what an
    already-trusted key may do, so they save without ceremony."""

    def _post(self, node, path, data):
        page = node.client.get("/fleet")
        data = dict(data)
        data["csrf_token"] = node._token(page.get_data(as_text=True))
        return node.client.post(path, data=data, follow_redirects=True)

    def _config(self, node):
        import configparser
        parser = configparser.ConfigParser()
        parser.read(node.config)
        return parser

    def test_saving_group_and_mode_writes_config(self):
        with _Node() as node:
            self._post(node, "/fleet/config",
                       {"group": "newgroup", "updates": "notify",
                        "pin_commit": ""})
            config = self._config(node)
            self.assertEqual(config.get("fleet", "group"), "newgroup")
            self.assertEqual(config.get("fleet", "updates"), "notify")

    def test_an_invalid_mode_is_refused(self):
        with _Node() as node:
            self._post(node, "/fleet/config",
                       {"group": GROUP, "updates": "whenever", "pin_commit": ""})
            self.assertEqual(self._config(node).get("fleet", "updates"), "auto")

    def test_a_non_hex_pin_is_refused(self):
        with _Node() as node:
            self._post(node, "/fleet/config",
                       {"group": GROUP, "updates": "auto",
                        "pin_commit": "not-a-sha"})
            self.assertEqual(
                self._config(node).get("fleet", "pin_commit", fallback=""), "")

    def test_saving_does_not_disturb_the_trusted_keys(self):
        """The form does not post them, so a naive write would erase them."""
        with _Node() as node:
            self._post(node, "/fleet/config",
                       {"group": GROUP, "updates": "auto", "pin_commit": ""})
            self.assertIn(node.key_id,
                          self._config(node).get("fleet", "trusted_keys"))


class TrustedKeyFormTests(unittest.TestCase):
    """Adding a key is the one action here that MINTS authority rather than
    scoping it, so it is the one that needs a gate."""

    def _post(self, node, path, data):
        page = node.client.get("/fleet")
        data = dict(data)
        data["csrf_token"] = node._token(page.get_data(as_text=True))
        return node.client.post(path, data=data, follow_redirects=True)

    def _trusted(self, node):
        import configparser
        parser = configparser.ConfigParser()
        parser.read(node.config)
        return fleet_update.parse_trusted_keys(
            parser.get("fleet", "trusted_keys", fallback=""))

    def test_adding_a_key_without_confirming_is_refused(self):
        with _Node() as node:
            _, public, new_id = fleet_update.generate_keypair()
            entry = fleet_update.public_key_entry(public)
            page = self._post(node, "/fleet/keys/add", {"trusted_key": entry})
            self.assertNotIn(new_id, self._trusted(node))
            self.assertIn("confirmation", page.get_data(as_text=True).lower())

    def test_adding_a_key_with_confirmation_works(self):
        with _Node() as node:
            _, public, new_id = fleet_update.generate_keypair()
            entry = fleet_update.public_key_entry(public)
            self._post(node, "/fleet/keys/add",
                       {"trusted_key": entry, "confirm": "1"})
            self.assertIn(new_id, self._trusted(node))

    def test_adding_a_key_keeps_the_existing_ones(self):
        """Onboarding a second signer must not silently revoke the first."""
        with _Node() as node:
            _, public, new_id = fleet_update.generate_keypair()
            self._post(node, "/fleet/keys/add",
                       {"trusted_key": fleet_update.public_key_entry(public),
                        "confirm": "1"})
            trusted = self._trusted(node)
            self.assertIn(new_id, trusted)
            self.assertIn(node.key_id, trusted)

    def test_a_malformed_key_is_refused(self):
        with _Node() as node:
            for junk in ("nonsense", "fkabc123:notbase64!!", "fkabc123:c2hvcnQ"):
                with self.subTest(junk=junk):
                    self._post(node, "/fleet/keys/add",
                               {"trusted_key": junk, "confirm": "1"})
                    self.assertEqual(list(self._trusted(node)), [node.key_id])

    def test_a_private_key_paste_is_refused(self):
        """The one mistake that undoes the whole model."""
        with _Node() as node:
            self._post(node, "/fleet/keys/add",
                       {"trusted_key": "-----BEGIN PRIVATE KEY-----",
                        "confirm": "1"})
            self.assertEqual(list(self._trusted(node)), [node.key_id])

    def test_an_entry_whose_id_does_not_match_its_key_is_refused(self):
        """The id is derived from the key, so a mismatch means the entry was
        assembled or edited by hand. It is refused outright rather than
        silently rewritten, so the operator finds out their paste was wrong
        instead of trusting a key whose id is not what they were shown.

        Asserted against the raw config text: parse_trusted_keys drops a
        mismatched id on read, so checking through it would pass whatever the
        writer did and prove nothing.
        """
        import configparser
        with _Node() as node:
            _, public, real_id = fleet_update.generate_keypair()
            _, _, key_b64 = fleet_update.public_key_entry(public).partition(":")
            page = self._post(node, "/fleet/keys/add",
                              {"trusted_key": f"fkdead99:{key_b64}",
                               "confirm": "1"})
            parser = configparser.ConfigParser()
            parser.read(node.config)
            raw = parser.get("fleet", "trusted_keys", fallback="")
            self.assertNotIn("fkdead99", raw)
            self.assertNotIn(real_id, raw, "a mis-labelled entry was stored anyway")
            self.assertIn("not a usable public key", page.get_data(as_text=True).lower())

    def test_removing_a_key_needs_no_confirmation(self):
        """Reducing authority is always safe."""
        with _Node() as node:
            self._post(node, "/fleet/keys/remove", {"key_id": node.key_id})
            self.assertEqual(self._trusted(node), {})

    def test_key_changes_require_csrf(self):
        with _Node() as node:
            _, public, new_id = fleet_update.generate_keypair()
            response = node.client.post("/fleet/keys/add", data={
                "trusted_key": fleet_update.public_key_entry(public),
                "confirm": "1"})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(new_id, self._trusted(node))


class GeneratedKeyTests(unittest.TestCase):
    def _post(self, node, public_raw, *, confirm=True, csrf=True):
        page = node.client.get("/fleet")
        headers = {}
        if csrf:
            headers["X-CSRF-Token"] = node._token(page.get_data(as_text=True))
        encoded = base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")
        return node.client.post("/api/fleet/keys/generated", json={
            "public_key": encoded,
            "confirm": confirm,
        }, headers=headers)

    def _trusted(self, node):
        import configparser
        parser = configparser.ConfigParser()
        parser.read(node.config)
        return fleet_update.parse_trusted_keys(
            parser.get("fleet", "trusted_keys", fallback=""))

    def test_browser_generated_public_key_is_trusted(self):
        with _Node() as node:
            _, public_raw, key_id = fleet_update.generate_keypair()
            response = self._post(node, public_raw)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(response.get_json()["key_id"], key_id)
            self.assertIn(key_id, self._trusted(node))
            self.assertIn(node.key_id, self._trusted(node))

    def test_response_contains_only_the_public_entry(self):
        with _Node() as node:
            _, public_raw, _ = fleet_update.generate_keypair()
            payload = self._post(node, public_raw).get_json()
            self.assertIn("public_entry", payload)
            self.assertNotIn("private_key", payload)

    def test_invalid_public_key_is_refused(self):
        with _Node() as node:
            response = self._post(node, b"too short")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(list(self._trusted(node)), [node.key_id])

    def test_generation_requires_confirmation(self):
        with _Node() as node:
            _, public_raw, key_id = fleet_update.generate_keypair()
            response = self._post(node, public_raw, confirm=False)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(key_id, self._trusted(node))

    def test_generation_requires_csrf(self):
        with _Node() as node:
            _, public_raw, key_id = fleet_update.generate_keypair()
            response = self._post(node, public_raw, csrf=False)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(key_id, self._trusted(node))

    def test_http_fallback_returns_matching_private_key_without_storing_it(self):
        from cryptography.hazmat.primitives import serialization

        with _Node() as node:
            page = node.client.get("/fleet")
            response = node.client.post("/api/fleet/keys/create", json={
                "confirm": True,
            }, headers={
                "X-CSRF-Token": node._token(page.get_data(as_text=True)),
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            payload = response.get_json()
            private_key = serialization.load_pem_private_key(
                payload["private_key"].encode("ascii"), password=None)
            public_raw = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw)
            self.assertEqual(
                fleet_update.public_key_entry(public_raw),
                payload["public_entry"])
            self.assertIn(payload["key_id"], self._trusted(node))
            with open(node.config, "r", encoding="utf-8") as handle:
                self.assertNotIn("PRIVATE KEY", handle.read())

    def test_http_fallback_requires_confirmation_and_csrf(self):
        with _Node() as node:
            page = node.client.get("/fleet")
            token = node._token(page.get_data(as_text=True))
            response = node.client.post("/api/fleet/keys/create",
                                        json={"confirm": False},
                                        headers={"X-CSRF-Token": token})
            self.assertEqual(response.status_code, 400)
            response = node.client.post("/api/fleet/keys/create",
                                        json={"confirm": True})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(list(self._trusted(node)), [node.key_id])
