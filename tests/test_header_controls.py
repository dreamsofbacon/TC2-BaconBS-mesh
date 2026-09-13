"""Buttons in the site header actually do something.

The Search button shipped with the new masthead and did nothing for a day.
Ctrl/Cmd+K opened the quick-search modal the whole time, so every test and
every automated check passed: the endpoint worked, the modal markup was
present, the script was loaded. The single missing line was the click
handler, and nothing was looking at whether the markup and the handler had
been introduced by the same change.

That is the failure this file guards. A control in base.html is only real if
something binds it, so these pair the markup against the JavaScript rather
than checking either on its own.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
SHORTCUTS = (ROOT / "static" / "js" / "shortcuts.js").read_text(encoding="utf-8")
NAV = (ROOT / "static" / "js" / "nav.js").read_text(encoding="utf-8")
APP = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
SCRIPTS = SHORTCUTS + NAV + APP


class SearchButtonTests(unittest.TestCase):
    def test_the_header_offers_a_search_button(self):
        self.assertIn('id="quick-search-button"', BASE)

    def test_something_binds_the_search_button(self):
        """The regression itself: markup present, handler absent."""
        self.assertIn("quick-search-button", SCRIPTS)

    def test_the_binding_opens_the_quick_search_modal(self):
        """Binding it to the wrong thing would pass the check above."""
        match = re.search(
            r"getElementById\('quick-search-button'\)(.|\n){0,200}?addEventListener\("
            r"'click',\s*(\w+)", SHORTCUTS)
        self.assertIsNotNone(match, "search button is not wired to a click handler")
        self.assertEqual(match.group(2), "openQS")

    def test_the_modal_the_button_opens_exists(self):
        self.assertIn('id="qs-modal"', BASE)
        self.assertIn('id="qs-input"', BASE)
        self.assertIn('id="qs-results"', BASE)

    def test_the_page_loads_the_script_that_does_the_binding(self):
        self.assertIn("js/shortcuts.js", BASE)


class HeaderControlsAreWiredTests(unittest.TestCase):
    """Every id-bearing <button> in the header has to be claimed by something.

    Written as a sweep rather than one assertion per button so a control
    added later is covered without anyone remembering to come back here.
    """

    HEADER_BUTTON = re.compile(r'<button[^>]*\bid="([a-z0-9-]+)"', re.I)

    def test_every_header_button_has_a_handler(self):
        # Dropdown toggles are opened by nav.js via a class/data attribute
        # sweep rather than by id, so an id lookup would not find them.
        handled_by_sweep = {"tools-dropdown-btn"}
        unbound = []
        for button_id in set(self.HEADER_BUTTON.findall(BASE)):
            if button_id in handled_by_sweep:
                continue
            if button_id not in SCRIPTS:
                unbound.append(button_id)
        self.assertEqual(unbound, [], f"header buttons with no handler: {unbound}")



class VersionChipTests(unittest.TestCase):
    """The running version stays visible in the header.

    It went missing when the new visual identity arrived: the markup was
    still in base.html and still rendered the right value, but operations.css
    set it to display:none, so nothing looked wrong in the template or the
    handler. An operator deploying fleet updates needs to see at a glance
    which version the page they are on is actually running.
    """

    OPERATIONS = (ROOT / "static" / "css" / "operations.css").read_text(encoding="utf-8")

    def test_the_header_renders_the_version(self):
        self.assertIn('class="version-chip"', BASE)
        self.assertIn("app_version_display", BASE)

    def test_no_stylesheet_hides_it_on_a_desktop(self):
        for name, css in (("operations.css", self.OPERATIONS),):
            with self.subTest(stylesheet=name):
                rules = re.findall(r"([^{}]*\.version-chip[^{}]*)\{([^}]*)\}", css)
                for selector, body in rules:
                    if "@media" in selector:
                        continue
                    self.assertNotRegex(body, r"display\s*:\s*none",
                                        f"{selector.strip()} hides the version chip")


if __name__ == "__main__":
    unittest.main()
