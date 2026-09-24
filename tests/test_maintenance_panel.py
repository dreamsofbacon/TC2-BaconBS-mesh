"""Apply an update, restart the services, reboot the node -- from the browser.

Three fixed actions, each spelled out where it is used. None of them takes a
unit name, a command or anything else from the form: the maintenance page is
a short list of things this node can be told to do, not a way to run
commands through a web form that happens to be behind a login.

Applying an update trusts nothing new -- the target was verified when it
arrived, and this only stops the node waiting for its next poll. Restart and
reboot need one passwordless sudo rule each, which install_services.sh
writes; where it is missing they say so and give the command to run by hand,
because a web request cannot answer a password prompt.
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_operations

ROOT = Path(__file__).resolve().parent.parent


class _Panel(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp()
        self.config_path = os.path.join(folder, "config.ini")
        self.db_path = os.path.join(folder, "bulletins.db")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("[bbs]\nname = Test\n[boards]\nbulletin_boards = General\n")
        db_operations.thread_local.connection = sqlite3.connect(self.db_path)
        db_operations.initialize_database()
        import web_admin
        self.web_admin = web_admin
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def _client(self):
        app = self.web_admin.create_app()
        app.config["CONFIG_PATH"] = self.config_path
        app.config["DB_PATH"] = self.db_path
        app.config["BULLETIN_BOARDS"] = ["General"]
        client = app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
        return client

    def post(self, section, **fields):
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": self.config_path}):
            client = self._client()
            token = client.get("/api/csrf-token").get_json()["csrf_token"]
            data = {"csrf_token": token, "settings_section": section}
            data.update(fields)
            return client.post("/settings", data=data, follow_redirects=True)


class ApplyUpdateTests(_Panel):
    def test_it_asks_the_mesh_server_to_apply_now(self):
        with mock.patch.object(self.web_admin, "request_fleet_apply_trigger") as ask:
            self.post("apply_update")
        ask.assert_called_once()

    def test_it_does_not_need_sudo(self):
        """It writes a trigger file the mesh server already watches."""
        with mock.patch.object(self.web_admin, "_sudo_systemctl") as sudo, \
                mock.patch.object(self.web_admin, "request_fleet_apply_trigger"):
            self.post("apply_update")
        sudo.assert_not_called()


class RestartTests(_Panel):
    def test_it_restarts_the_services(self):
        with mock.patch.object(self.web_admin, "restart_bbs_services",
                               return_value=(True, "mesh-bbs.service")) as restart, \
                mock.patch.object(self.web_admin, "_sudo_systemctl",
                                  return_value=(True, "done")) as sudo:
            self.post("restart_services")
        restart.assert_called_once()
        # The web admin goes last: restarting it ends this request.
        sudo.assert_called_once_with("restart", "bacon-web-admin.service")

    def test_a_missing_sudo_rule_is_explained_not_swallowed(self):
        with mock.patch.object(
                self.web_admin, "restart_bbs_services",
                return_value=(False, "mesh-bbs.service: may not run without a "
                                     "password")) as restart:
            response = self.post("restart_services")
        restart.assert_called_once()
        self.assertIn("password", response.get_data(as_text=True))


class RebootTests(_Panel):
    def test_it_refuses_without_the_typed_word(self):
        with mock.patch.object(self.web_admin, "reboot_node") as reboot:
            response = self.post("reboot_node", confirm="")
        reboot.assert_not_called()
        self.assertIn("REBOOT", response.get_data(as_text=True))

    def test_a_wrong_word_is_not_enough(self):
        with mock.patch.object(self.web_admin, "reboot_node") as reboot:
            self.post("reboot_node", confirm="yes")
        reboot.assert_not_called()

    def test_the_typed_word_reboots(self):
        with mock.patch.object(self.web_admin, "reboot_node",
                               return_value=(True, "done")) as reboot:
            self.post("reboot_node", confirm="REBOOT")
        reboot.assert_called_once()

    def test_it_is_not_case_sensitive(self):
        with mock.patch.object(self.web_admin, "reboot_node",
                               return_value=(True, "done")) as reboot:
            self.post("reboot_node", confirm="reboot")
        reboot.assert_called_once()

    def test_a_failure_is_reported(self):
        with mock.patch.object(self.web_admin, "reboot_node",
                               return_value=(False, "may not run systemctl reboot")):
            response = self.post("reboot_node", confirm="REBOOT")
        self.assertIn("may not run", response.get_data(as_text=True))


class TheCommandsAreFixedTests(unittest.TestCase):
    """Nothing request-controlled reaches a command line."""

    SOURCE = (ROOT / "web_admin.py").read_text(encoding="utf-8")

    def test_systemctl_is_only_ever_called_through_the_one_helper(self):
        block = self.SOURCE[self.SOURCE.index("def _sudo_systemctl"):]
        block = block[:block.index("def restart_bbs_services")]
        self.assertIn('["sudo", "-n", "systemctl", *arguments]', block)

    def test_the_reboot_takes_no_argument_from_the_request(self):
        block = self.SOURCE[self.SOURCE.index("def reboot_node"):]
        block = block[:400]
        self.assertIn('_sudo_systemctl("reboot")', block)

    def test_the_unit_names_are_a_fixed_tuple(self):
        self.assertIn('_SERVICE_UNITS = ("mesh-bbs.service", "bacon-web-admin.service",',
                      self.SOURCE)


class TheInstallerGrantsExactlyThoseTests(unittest.TestCase):
    INSTALLER = (ROOT / "install_services.sh").read_text(encoding="utf-8")

    def test_it_grants_the_service_restarts_and_reboot(self):
        self.assertIn("$SYSTEMCTL restart $UNIT", self.INSTALLER)
        self.assertIn("$SYSTEMCTL reboot", self.INSTALLER)

    def test_it_grants_no_wildcards(self):
        """A rule ending in a bare systemctl would be 'run anything as root'."""
        for line in self.INSTALLER.splitlines():
            if "NOPASSWD" in line:
                with self.subTest(line=line.strip()):
                    self.assertNotRegex(line, r"NOPASSWD:\s*\S*systemctl\s*\"?$")
                    self.assertNotIn("ALL$", line.strip())


if __name__ == "__main__":
    unittest.main()
