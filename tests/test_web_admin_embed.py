"""The web admin running inside the mesh server.

It must stay off unless mesh-bbs.service asks for it: a fleet update gives a
node the new code but not the new unit file, and a node still running
bacon-web-admin.service has to carry on exactly as before. When it is on,
the page must actually answer, a port it cannot get must not stop the BBS,
and a BBS that fails to start must leave the page up to repair it with.
"""
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

# server.py reaches config_init, which imports the real radio libraries.
import radio_stubs
radio_stubs.install()

import web_admin
import web_admin_embed


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="embed-")
        self.config = os.path.join(self.dir, "config.ini")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("[admin]\nusername = admin\npassword = pw\n")
        self.port = _free_port()
        self.env = mock.patch.dict(os.environ, {
            "BBS_CONFIG_PATH": self.config,
            "BBS_DB_PATH": os.path.join(self.dir, "bulletins.db"),
            "BBS_WEBGUI_SECRET": "test-secret",
            "BBS_WEBGUI_HOST": "127.0.0.1",
            "BBS_WEBGUI_PORT": str(self.port),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self._was_started = web_admin_embed._started
        self._unit = web_admin.EMBEDDED_SERVICE_UNIT
        web_admin_embed._started = False

    def tearDown(self):
        web_admin_embed._started = self._was_started
        web_admin.EMBEDDED_SERVICE_UNIT = self._unit


class OffUnlessAskedTests(_Scratch):
    def test_it_does_nothing_without_the_setting(self):
        os.environ.pop(web_admin_embed.ENABLE_ENV, None)
        with mock.patch.object(web_admin, "create_app") as create:
            self.assertFalse(web_admin_embed.start())
        create.assert_not_called()
        self.assertFalse(web_admin_embed.running())

    def test_zero_means_off(self):
        os.environ[web_admin_embed.ENABLE_ENV] = "0"
        self.assertFalse(web_admin_embed.enabled())


class ServesTests(_Scratch):
    def test_the_login_page_answers_on_the_configured_port(self):
        os.environ[web_admin_embed.ENABLE_ENV] = "1"
        self.assertTrue(web_admin_embed.start())
        self.assertTrue(web_admin_embed.running())
        self.assertEqual(web_admin.EMBEDDED_SERVICE_UNIT, "mesh-bbs.service")
        url = f"http://127.0.0.1:{self.port}/login"
        for _ in range(50):
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    return
            except OSError:
                time.sleep(0.1)
        self.fail("the embedded web admin never answered")

    def test_a_port_already_taken_is_logged_not_raised(self):
        """bacon-web-admin.service still holding 8081 must not stop the BBS."""
        os.environ[web_admin_embed.ENABLE_ENV] = "1"
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", self.port))
            holder.listen()
            with self.assertLogs(level="ERROR") as logs:
                self.assertFalse(web_admin_embed.start())
        self.assertFalse(web_admin_embed.running())
        self.assertIn("install_services.sh", "\n".join(logs.output))


class RepairHoldTests(unittest.TestCase):
    """server.main keeps the page up when the BBS fails to start."""

    def setUp(self):
        import server
        self.server = server
        self.dir = tempfile.mkdtemp(prefix="repair-")
        self.config = os.path.join(self.dir, "config.ini")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write("[interface]\ntype = none\n")
        self.env = mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": self.config})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(setattr, server, "_bbs_started", False)

    def _main(self, embedded_running, started_before_crash=False):
        def crash():
            self.server._bbs_started = started_before_crash
            raise RuntimeError("bad config")
        with mock.patch.object(self.server, "_run_bbs", side_effect=crash), \
                mock.patch.object(web_admin_embed, "start"), \
                mock.patch.object(web_admin_embed, "running",
                                  return_value=embedded_running), \
                mock.patch.object(self.server, "_hold_for_repair") as hold:
            try:
                self.server.main()
            except RuntimeError:
                return "raised", hold
        return "held", hold

    def test_a_startup_failure_holds_for_repair_when_embedded(self):
        outcome, hold = self._main(embedded_running=True)
        self.assertEqual(outcome, "held")
        hold.assert_called_once()

    def test_without_the_embedded_page_it_exits_as_before(self):
        outcome, hold = self._main(embedded_running=False)
        self.assertEqual(outcome, "raised")
        hold.assert_not_called()

    def test_a_crash_after_startup_still_exits_so_the_radio_comes_back(self):
        outcome, hold = self._main(embedded_running=True, started_before_crash=True)
        self.assertEqual(outcome, "raised")
        hold.assert_not_called()

    def test_saving_the_config_ends_the_hold_and_retries(self):
        """The operator fixes config.ini in the page; the node tries again
        without waiting out the window."""
        def save_soon():
            time.sleep(0.3)
            os.utime(self.config, (time.time() + 5, time.time() + 5))
        import threading
        threading.Thread(target=save_soon, daemon=True).start()
        started = time.time()
        with mock.patch.object(self.server, "_REPAIR_WINDOW_SECONDS", 30), \
                self.assertLogs(level="WARNING"), \
                self.assertRaises(SystemExit) as ended:
            try:
                raise RuntimeError("bad config")
            except RuntimeError:
                self.server._hold_for_repair()
        self.assertEqual(ended.exception.code, 1)
        self.assertLess(time.time() - started, 10)


if __name__ == "__main__":
    unittest.main()
