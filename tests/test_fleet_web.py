"""The Fleet page and its paste box.

The box is the one place a human hands the node an instruction, and the only
thing in front of it is the web admin password -- which is plaintext in
config.ini and defaults to "change-me". So the paste is NOT trusted because
it arrived through an authenticated session: it is verified exactly as if it
had come off the air. This endpoint can relay authority, never mint it.
"""
import hashlib
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
API_TOKEN = "test-fleet-token"


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
                "updates = auto\n"
                f"api_token_hash = {hashlib.sha256(API_TOKEN.encode()).hexdigest()}\n")
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

    def test_page_shows_web_first_fleet_enrollment(self):
        with _Node() as node:
            page = node.client.get("/fleet")
            body = page.get_data(as_text=True)
            self.assertIn("Fleet enrollment", body)
            self.assertIn("signed releases arrive through the mesh", body)
            self.assertIn("supplied by your fleet administrator", body)
            self.assertNotIn("scripts/fleet_sign.py", body)
            self.assertNotIn("python ", body.lower())
            self.assertNotIn("Generate and download key", body)
            self.assertNotIn("js/fleet.js", body)

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


class FleetApiTests(unittest.TestCase):
    def test_api_accepts_a_signed_instruction(self):
        with _Node() as node:
            response = node.client.post(
                "/api/fleet/apply", json={"instruction": node.instruction()},
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["code"], "accepted")
            self.assertEqual(db_operations.get_fleet_target(GROUP)["commit"], COMMIT)

    def test_api_rejects_a_missing_token(self):
        with _Node() as node:
            response = node.client.post(
                "/api/fleet/apply", json={"instruction": node.instruction()})

            self.assertEqual(response.status_code, 401)
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_api_rejects_a_forged_instruction_even_with_a_valid_token(self):
        with _Node() as node:
            attacker, _, _ = fleet_update.generate_keypair()
            response = node.client.post(
                "/api/fleet/apply",
                json={"instruction": node.instruction(private=attacker)},
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], "rejected")
            self.assertIsNone(db_operations.get_fleet_target(GROUP))

    def test_status_api_reports_target_local_state_and_peers(self):
        with _Node() as node:
            node.paste(node.instruction())
            db_operations.record_node_version("!peer1", "0.1.999", COMMIT[:7])
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["target"]["commit"], COMMIT)
            self.assertEqual(body["history"][0]["commit"], COMMIT)
            self.assertEqual(body["nodes"][0]["node_id"], "!peer1")
            self.assertIn("update_state", body["local"])
            self.assertGreaterEqual(body["summary"]["healthy"], 1)

    def test_explicit_peer_failure_is_not_hidden_by_a_matching_commit(self):
        with _Node() as node:
            node.paste(node.instruction())
            db_operations.record_node_version(
                "!peer1", "0.1.999", COMMIT[:7], COMMIT, "failed")
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            peer = response.get_json()["nodes"][0]
            self.assertEqual(peer["fleet_state"], "failed")

    def test_a_peer_holding_no_target_reads_as_not_enrolled(self):
        """The live case. Chattanooga sat thirteen releases behind for a day
        reporting "pending" -- which is also what a node reports for the
        ninety seconds it takes to converge, so nothing distinguished a peer
        that was working through an update from one that had never accepted
        an instruction at all. It is reachable, it talks constantly, and it
        holds no target; only its own [fleet] settings can change that."""
        with _Node() as node:
            node.paste(node.instruction())
            db_operations.record_node_version(
                "!peer1", "0.1.500", "0bd9178", "", "pending")
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            peer = response.get_json()["nodes"][0]
            self.assertEqual(peer["fleet_state"], "not enrolled")

    def test_a_peer_working_through_an_update_still_reads_as_pending(self):
        """It has stored our target and is not on it yet. That resolves
        itself, and must not be reported as an operator problem."""
        with _Node() as node:
            node.paste(node.instruction())
            db_operations.record_node_version(
                "!peer1", "0.1.500", "0bd9178", COMMIT, "pending")
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertEqual(response.get_json()["nodes"][0]["fleet_state"], "pending")

    def test_no_target_of_our_own_means_no_peer_is_blamed(self):
        """Before anything is signed nobody holds a target, and that is not
        a peer being unenrolled."""
        with _Node() as node:
            db_operations.record_node_version(
                "!peer1", "0.1.500", "0bd9178", "", "pending")
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertNotEqual(
                response.get_json()["nodes"][0]["fleet_state"], "not enrolled")

    def test_an_explicit_failure_still_wins_over_the_new_state(self):
        with _Node() as node:
            node.paste(node.instruction())
            db_operations.record_node_version(
                "!peer1", "0.1.500", "0bd9178", "", "failed")
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertEqual(response.get_json()["nodes"][0]["fleet_state"], "failed")

    def test_a_peer_already_on_the_target_is_healthy_whatever_it_reports(self):
        """Caught by an existing test when the not-enrolled check first went
        in without this guard: a peer sitting on the target commit is fine,
        and whether it also reports holding a target is beside the point."""
        with _Node() as node:
            node.paste(node.instruction())
            db_operations.record_node_version("!peer1", "0.1.999", COMMIT[:7], "", "")
            response = node.client.get(
                "/api/fleet/status",
                headers={"Authorization": f"Bearer {API_TOKEN}"})

            self.assertEqual(response.get_json()["nodes"][0]["fleet_state"], "healthy")

    def test_status_api_requires_the_fleet_token(self):
        with _Node() as node:
            response = node.client.get("/api/fleet/status")

            self.assertEqual(response.status_code, 401)


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


class GeneratedKeyEndpointTests(unittest.TestCase):
    def test_private_key_generation_endpoints_do_not_exist(self):
        with _Node() as node:
            paths = {rule.rule for rule in node.app.url_map.iter_rules()}
            self.assertNotIn("/api/fleet/keys/generated", paths)
            self.assertNotIn("/api/fleet/keys/create", paths)
