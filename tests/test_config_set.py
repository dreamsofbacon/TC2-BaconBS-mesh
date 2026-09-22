"""scripts/config_set.py -- how install.sh writes config.ini.

It has to change values without losing the comments, because the comments
in example_config.ini are the documentation a new operator reads, and the
result has to be something the BBS's own configparser reads back.
"""

import configparser
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import config_set  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(text):
    parser = configparser.ConfigParser()
    parser.read_string(text)
    return parser


class ConfigSetTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "example_config.ini"), encoding="utf-8") as f:
            self.example = f.read().replace("\r\n", "\n")

    def test_an_active_value_is_replaced_in_place(self):
        text = config_set.set_values(self.example, "interface", {"type": "none"})
        self.assertEqual("none", _read(text).get("interface", "type"))
        self.assertEqual(1, text.count("\n[interface]\n"))

    def test_the_comments_survive(self):
        text = config_set.set_values(self.example, "interface", {"type": "none"})
        self.assertIn("# Meshtastic USB: type = serial", text)
        self.assertIn("# type = none  -- this node has NO radio", text)

    def test_a_missing_key_is_added_to_its_section(self):
        text = config_set.set_values(self.example, "interface",
                                     {"type": "tcp", "hostname": "10.0.0.5"})
        self.assertEqual("10.0.0.5", _read(text).get("interface", "hostname"))

    def test_a_commented_out_section_becomes_a_real_one(self):
        """[ssh] only exists as `# [ssh]` in the example."""
        text = config_set.set_values(self.example, "ssh",
                                     {"enabled": "true", "public_access": "true"})
        parser = _read(text)
        self.assertTrue(parser.getboolean("ssh", "enabled"))
        self.assertTrue(parser.getboolean("ssh", "public_access"))

    def test_setting_twice_does_not_duplicate(self):
        """Re-running the installer must leave a file configparser accepts:
        a duplicate key or section is a hard error there."""
        text = self.example
        for _ in range(2):
            text = config_set.set_values(text, "admin", {"password": "one"})
            text = config_set.set_values(text, "ssh", {"enabled": "false"})
        text = config_set.set_values(text, "admin", {"password": "two"})
        self.assertEqual("two", _read(text).get("admin", "password"))

    def test_a_percent_in_a_password_reads_back_when_escaped(self):
        """install.sh doubles %, which configparser would otherwise read
        as the start of a placeholder and refuse."""
        text = config_set.set_values(self.example, "admin",
                                     {"password": "50%%off=deal"})
        self.assertEqual("50%off=deal", _read(text).get("admin", "password"))

    def test_the_command_line_keeps_crlf_files_crlf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.ini")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("[interface]\r\ntype = serial\r\n")
            config_set.main(["config_set.py", path, "interface", "type=none",
                             "ssh", "enabled=true", "host=0.0.0.0"])
            with open(path, encoding="utf-8", newline="") as f:
                raw = f.read()
        self.assertNotIn("\n", raw.replace("\r\n", ""))
        parser = _read(raw)
        self.assertEqual("none", parser.get("interface", "type"))
        self.assertEqual("0.0.0.0", parser.get("ssh", "host"))


if __name__ == "__main__":
    unittest.main()
