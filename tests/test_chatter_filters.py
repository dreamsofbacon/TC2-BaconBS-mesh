"""Filtering the chatter feed from the legend.

The Network and Channel dropdowns duplicated what the legend already listed,
and each offered one choice at a time. The legend entries are now the filter
control: click to narrow, pick several to combine.

Two rules the behaviour rests on, both easy to get backwards:

- Nothing selected means "no constraint", not "nothing shown". So the feed
  starts full and one click narrows it, rather than needing every other
  option switched off first.
- Within a group the choices are OR (these two channels), across groups they
  are AND (this channel, heard by that node). Any other combination makes
  the second click do something unpredictable.

Filtering is client-side because the endpoint already returns the whole time
window in one request, so it costs nothing and is instant.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "public-chatter.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "public_chatter.html").read_text(encoding="utf-8")
WEB_ADMIN = (ROOT / "web_admin.py").read_text(encoding="utf-8")


class TheDropdownsAreGoneTests(unittest.TestCase):
    def test_the_network_select_is_removed(self):
        self.assertNotIn('id="chatter-network"', HTML)
        self.assertNotIn("chatter-network", JS)

    def test_the_channel_select_is_removed(self):
        self.assertNotIn('id="chatter-channel"', HTML)
        self.assertNotIn("chatter-channel", JS)

    def test_the_controls_that_remain_are_still_wired(self):
        """History and search stay server-side; only the two the legend
        replaced were removed."""
        for needle in ('id="chatter-hours"', 'id="chatter-search"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, HTML)
        self.assertIn('result.set("q"', JS)
        self.assertIn("hours:", JS)

    def test_the_page_no_longer_asks_the_server_to_filter(self):
        """Those params only existed to feed the dropdowns."""
        self.assertNotIn('result.set("network"', JS)
        self.assertNotIn('result.set("channel"', JS)

    def test_the_route_no_longer_builds_the_dropdown_lists(self):
        """A database query per page load with nothing left to render."""
        self.assertNotIn("filters=get_public_chatter_filters()", WEB_ADMIN)
        self.assertNotIn("get_public_chatter_filters", WEB_ADMIN)


class TheLegendIsTheControlTests(unittest.TestCase):
    def test_all_three_dimensions_have_a_group(self):
        for needle in ('id="legend-networks"', 'id="legend-channels"',
                       'id="legend-nodes"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, HTML)

    def test_a_legend_entry_is_a_real_button(self):
        """Keyboard reachable, not a span with a click handler."""
        self.assertIn('button.type = "button"', JS)
        self.assertIn('document.createElement("button")', JS)

    def test_its_selected_state_is_announced_not_only_coloured(self):
        self.assertIn('button.setAttribute("aria-pressed"', JS)
        self.assertIn('[aria-pressed="true"]', HTML)

    def test_it_is_focusable_visibly(self):
        self.assertIn(".legend-chip:focus-visible", HTML)

    def test_the_network_group_hides_itself_when_there_is_one_network(self):
        """One network is not a choice, and the channel chips already say
        which network they are on."""
        self.assertIn("legendNetworkGroup.hidden = networkNames.length < 2", JS)

    def test_clearing_is_offered_only_while_something_is_selected(self):
        self.assertIn("clearButton.hidden = !filtering()", JS)
        self.assertIn('id="legend-clear"', HTML)

    def test_the_page_says_how_to_use_it(self):
        self.assertIn("Click to filter", HTML)


class SelectionSemanticsTests(unittest.TestCase):
    """The two rules that make a second click predictable."""

    def matches_body(self):
        start = JS.index("function matches(entry)")
        return JS[start:JS.index("\n  }", start)]

    def test_an_empty_group_imposes_no_constraint(self):
        """Guarded by anySelected before every membership test, so the feed
        starts full rather than empty."""
        body = self.matches_body()
        for group in ("networks", "channels", "nodes"):
            with self.subTest(group=group):
                self.assertIn('anySelected("%s")' % group, body)

    def test_a_selected_group_is_a_membership_test(self):
        """Membership is OR within the group."""
        body = self.matches_body()
        self.assertIn("selected.networks[networkKey(entry)]", body)
        self.assertIn("selected.channels[channelKey(entry)]", body)
        self.assertIn("selected.nodes[key]", body)

    def test_the_groups_combine_as_and(self):
        """One early exit per dimension, then true. An or would let a row
        matching any single group through, so picking a channel AND a node
        would widen the feed instead of narrowing it."""
        body = self.matches_body()
        self.assertEqual(body.count("return false"), 3,
                         "expected one bail-out per dimension")
        self.assertTrue(body.rstrip().endswith("return true;"))

    def test_a_row_with_no_capture_node_fails_a_node_filter(self):
        """It cannot be the node you asked for, so it must not slip through
        the way a null often does."""
        self.assertIn("if (key === null || !selected.nodes[key]) return false",
                      self.matches_body())

    def test_selection_keys_cannot_collide_with_object_prototype(self):
        """Keys are node ids and network names straight off the air. A
        station calling itself "constructor" would otherwise read as already
        selected, because a plain object inherits that name as truthy."""
        selected_init = JS[JS.index("var selected = {"):]
        selected_init = selected_init[:selected_init.index("};")]
        for group in ("networks", "channels", "nodes"):
            with self.subTest(group=group, where="initial"):
                self.assertRegex(
                    selected_init, group + r":\s*Object\.create\(null\)")
        cleared = JS[JS.index("function clearFilters()"):]
        cleared = cleared[:cleared.index("\n  }")]
        for group in ("networks", "channels", "nodes"):
            with self.subTest(group=group, where="cleared"):
                self.assertIn(
                    "selected.%s = Object.create(null)" % group, cleared)


class FilteringDoesNotDisturbTheRestTests(unittest.TestCase):
    def test_colours_are_assigned_from_every_entry(self):
        """Assigning from the visible ones would repaint the whole feed on
        every click, which is exactly when a stable colour matters most."""
        self.assertIn("var palette = buildPalette(allEntries)", JS)

    def test_the_legend_lists_every_entry_not_the_visible_ones(self):
        """The chip you just switched off is the one you need to click again
        to bring it back."""
        self.assertIn("renderLegend(allEntries, palette)", JS)

    def test_only_the_feed_is_narrowed(self):
        self.assertIn("var visible = allEntries.filter(matches)", JS)

    def test_a_filter_does_not_refetch(self):
        """The window is already in memory, so a click is instant and does
        not cost the node a request."""
        start = JS.index("function render()")
        body = JS[start:JS.index("\n  }", start)]
        self.assertNotIn("fetch(", body)
        self.assertNotIn("load()", body)

    def test_an_over_narrow_filter_explains_itself(self):
        """An empty feed after a click should not read like a dead node."""
        self.assertIn("No messages match the selected filters.", JS)
        self.assertIn("No public messages in this time window.", JS)

    def test_a_failed_request_does_not_leave_stale_messages_on_screen(self):
        self.assertRegex(
            JS, r"catch \(error\) \{[\s\S]*?allEntries = \[\][\s\S]*?feed\.replaceChildren\(\)")

    def test_nothing_about_the_filter_is_persisted(self):
        """Same call as the colour toggle: a transient view choice does not
        earn browser storage."""
        self.assertNotIn("localStorage", JS)


if __name__ == "__main__":
    unittest.main()
