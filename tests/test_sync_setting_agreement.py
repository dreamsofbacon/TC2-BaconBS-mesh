"""What the Settings page says about game saves is what the node does.

The page kept its own default. `load_sync_settings` read
`fallback="true"` while the node itself treats an absent key as false
(`utils.is_zork_save_sync_enabled`). So a node that had never been told
either way showed "Sync game saves across nodes" ticked, and synced no
saves at all -- reported from the field as "it was switched on in the
webgui", and true from both sides at once.

A second opinion about a default is the whole bug, so the test is the
invariant rather than either value: for every way the setting can be
expressed, the page and the node agree.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils
import web_admin

CASES = {
    "absent": "[sync]\nbbs_nodes = !peer\n",
    "true": "[sync]\nbbs_nodes = !peer\nsync_zork_saves = true\n",
    "false": "[sync]\nbbs_nodes = !peer\nsync_zork_saves = false\n",
    "yes": "[sync]\nbbs_nodes = !peer\nsync_zork_saves = yes\n",
    "blank": "[sync]\nbbs_nodes = !peer\nsync_zork_saves =\n",
    "no section": "[bbs]\nname = Test\n",
}


class PageAndNodeAgreeTests(unittest.TestCase):
    def _write(self, body):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "config.ini")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def _page_says(self, path):
        return web_admin.load_sync_settings(path)[3]

    def _node_says(self, path):
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": path}):
            return utils.is_zork_save_sync_enabled()

    def test_they_agree_however_it_is_written(self):
        for name, body in CASES.items():
            path = self._write(body)
            with self.subTest(config=name):
                self.assertEqual(self._node_says(path), self._page_says(path))

    def test_an_absent_setting_reads_as_off(self):
        """Off is the node's long-standing default; the page now says so."""
        path = self._write(CASES["absent"])
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": path}):
            self.assertFalse(self._page_says(path))

    def test_an_explicit_true_is_honoured(self):
        path = self._write(CASES["true"])
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": path}):
            self.assertTrue(self._page_says(path))

    def test_the_environment_override_reaches_the_page(self):
        """BBS_SYNC_ZORK_SAVES changes what the node does, so it has to
        change what the page shows, or the page is lying again."""
        path = self._write(CASES["absent"])
        with mock.patch.dict(os.environ, {"BBS_CONFIG_PATH": path,
                                          "BBS_SYNC_ZORK_SAVES": "1"}):
            self.assertTrue(self._page_says(path))
            self.assertEqual(self._node_says(path), self._page_says(path))

    def test_a_file_value_beats_the_environment_on_the_page_being_edited(self):
        """The page edits this file; an explicit value in it is the answer
        it should show."""
        path = self._write(CASES["false"])
        with mock.patch.dict(os.environ, {"BBS_SYNC_ZORK_SAVES": "1"}):
            self.assertFalse(self._page_says(path))


if __name__ == "__main__":
    unittest.main()
