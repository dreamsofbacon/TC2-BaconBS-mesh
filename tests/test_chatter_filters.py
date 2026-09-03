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
    def test_both_dimensions_have_a_group(self):
        for needle in ('id="legend-channels"', 'id="legend-nodes"'):
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

    def test_there_is_no_separate_network_control(self):
        """A channel key is already network-qualified, so selecting channels
        selects networks by implication. A second control for it would be a
        slower way to say the same thing, and another row of vertical space
        on a page whose point is the feed below it."""
        for needle in ("legend-networks", "legend-network-group",
                       "selected.networks", "anySelected(\"networks\")"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, HTML)
                self.assertNotIn(needle, JS)

    def test_the_network_is_still_visible_on_every_channel_chip(self):
        """Removing the control must not remove the information."""
        self.assertRegex(
            JS, r"function channelLabel[\s\S]*?entry\.network")

    def test_clearing_is_offered_only_while_something_is_selected(self):
        self.assertIn("clearButton.hidden = !filtering()", JS)
        self.assertIn('id="legend-clear"', HTML)

    def test_the_page_says_how_to_use_it(self):
        self.assertIn("Click to filter", HTML)


class LayoutTests(unittest.TestCase):
    """The legend is a control, so it belongs with the controls -- and the
    page exists to show the feed, so the controls must not eat the screen
    above it."""

    def controls_block(self):
        """The controls section, whole.

        Sliced up to the element that follows it rather than to the next
        closing tag: the legend is a nested <section>, so the first
        </section> after the start is the legend's own, not the row's.
        """
        start = HTML.index('<section class="chatter-controls"')
        return HTML[start:HTML.index('id="chatter-state"')]

    def test_the_legend_sits_in_the_controls_row(self):
        self.assertIn('id="chatter-legend"', self.controls_block())

    def test_search_is_in_the_header_not_the_controls(self):
        """Top right of the page, opposite the title."""
        header = HTML[HTML.index('<header class="chatter-header">'):
                      HTML.index("</header>")]
        self.assertIn('id="chatter-search"', header)
        self.assertNotIn('id="chatter-search"', self.controls_block())

    def test_search_comes_after_the_title_in_the_markup(self):
        """So it lands on the right of a space-between header, and reads in
        the order it appears."""
        header = HTML[HTML.index('<header class="chatter-header">'):
                      HTML.index("</header>")]
        self.assertLess(header.index("<h1>"), header.index('id="chatter-search"'))

    def test_it_is_no_longer_in_the_header(self):
        header = HTML[HTML.index('<header class="chatter-header">'):
                      HTML.index("</header>")]
        self.assertNotIn("chatter-legend", header)

    def test_the_legend_never_scrolls(self):
        """A scrollbar hides exactly the chip you were looking for, and the
        whole point of the block is to be readable at a glance."""
        style = HTML[HTML.index("<style>"):HTML.index("</style>")]
        legend = style[style.index(".chatter-legend {"):]
        legend = legend[:legend.index("}")]
        self.assertNotIn("overflow", legend)
        self.assertNotIn("max-height", legend)

    def test_the_history_presets_sit_beside_the_input(self):
        """Under it, they cost the row a second line for nothing."""
        self.assertIn('<div class="history-row">', HTML)
        self.assertRegex(HTML, r"\.history-row \{ display:flex")

    def test_the_legend_gets_three_quarters_of_the_width(self):
        """It holds every channel and node, which is what a person reads;
        the time filter beside it needs far less."""
        self.assertRegex(
            HTML,
            r"\.chatter-controls \{[^}]*grid-template-columns:"
            r"minmax\(0,3fr\) minmax\(0,1fr\)")

    def test_the_legend_is_left_of_the_time_filter(self):
        self.assertRegex(HTML, r"\.chatter-legend\s+\{ grid-column:1;")
        self.assertRegex(HTML, r"\.chatter-history-group \{ grid-column:2;")
        self.assertLess(self.controls_block().index('id="chatter-legend"'),
                        self.controls_block().index('id="chatter-hours"'))

    def test_search_is_one_line(self):
        """Label beside the box, not above it. Labels are display:block
        globally, so this has to say otherwise explicitly."""
        self.assertRegex(HTML, r"\.chatter-search-group \{[^}]*display:flex")
        self.assertRegex(
            HTML, r"\.chatter-search-group label \{[^}]*display:inline")
        self.assertRegex(
            HTML, r"\.chatter-search-group label \{[^}]*margin:0")

    def test_search_sits_in_the_corner_not_level_with_the_strapline(self):
        """The header is align-items:end, which would drop it to the bottom
        of the title block; the corner needs that overridden."""
        self.assertRegex(HTML, r"\.chatter-header \{[^}]*align-items:end")
        self.assertRegex(
            HTML, r"\.chatter-search-group \{[^}]*align-self:flex-start")

    def test_search_is_pushed_to_the_right(self):
        """Whatever width the title takes."""
        self.assertRegex(
            HTML, r"\.chatter-search-group \{[^}]*margin:0 0 0 auto")

    def test_it_gives_the_width_back_when_the_header_stacks(self):
        block = HTML[HTML.index("@media (max-width:720px)"):]
        block = block[:block.index("\n  }")]
        self.assertIn("margin-left:0", block)

    def test_the_time_filter_offers_six_presets(self):
        presets = re.search(r"for value, label in \[(.*?)\]", HTML).group(1)
        pairs = re.findall(r"\((\d+),'([^']+)'\)", presets)
        self.assertEqual(
            pairs,
            [("1", "1h"), ("3", "3h"), ("6", "6h"),
             ("24", "24h"), ("72", "3d"), ("168", "7d")])

    def test_the_presets_are_two_rows_of_three(self):
        """A fixed three-wide grid, not wrapping. Reflowing to whatever fits
        made the same six buttons rearrange with the window, so the one you
        were reaching for moved."""
        self.assertRegex(
            HTML,
            r"\.chatter-presets \{ display:grid; "
            r"grid-template-columns:repeat\(3, minmax\(0,1fr\)\)")
        self.assertNotIn(".chatter-presets { display:flex", HTML)

    def test_nothing_re_wraps_them_at_a_narrow_width(self):
        """A stray flex-wrap in a media query would undo the fixed grid."""
        for query in ("720px", "430px"):
            block = HTML[HTML.index("@media (max-width:%s)" % query):]
            block = block[:block.index("\n  }")]
            with self.subTest(query=query):
                self.assertNotIn("chatter-presets", block)

    def test_each_preset_maps_to_the_hours_it_names(self):
        """3d and 7d are the odd ones: the control is in hours."""
        presets = re.search(r"for value, label in \[(.*?)\]", HTML).group(1)
        pairs = dict((b, int(a)) for a, b in
                     re.findall(r"\((\d+),'([^']+)'\)", presets))
        self.assertEqual(pairs["3h"], 3)
        self.assertEqual(pairs["3d"], 72)
        self.assertEqual(pairs["7d"], 168)

    def test_every_preset_is_within_the_inputs_own_range(self):
        """The number box caps at 168, and the server clamps there too, so a
        preset beyond it would silently do nothing."""
        presets = re.search(r"for value, label in \[(.*?)\]", HTML).group(1)
        values = [int(v) for v, _ in re.findall(r"\((\d+),'([^']+)'\)", presets)]
        self.assertTrue(all(1 <= v <= 168 for v in values), values)
        self.assertIn('min="1" max="168"', HTML)

    def test_it_stacks_rather_than_squeezing_on_a_narrow_screen(self):
        self.assertRegex(
            HTML, r"@media \(max-width:720px\)[\s\S]*?"
                  r"\.chatter-controls \{ grid-template-columns:1fr; \}")


class SelectionSemanticsTests(unittest.TestCase):
    """The two rules that make a second click predictable."""

    def matches_body(self):
        start = JS.index("function matches(entry)")
        return JS[start:JS.index("\n  }", start)]

    def test_an_empty_group_imposes_no_constraint(self):
        """Guarded by anySelected before every membership test, so the feed
        starts full rather than empty."""
        body = self.matches_body()
        for group in ("channels", "nodes"):
            with self.subTest(group=group):
                self.assertIn('anySelected("%s")' % group, body)

    def test_a_selected_group_is_a_membership_test(self):
        """Membership is OR within the group."""
        body = self.matches_body()
        self.assertIn("selected.channels[channelKey(entry)]", body)
        self.assertIn("selected.nodes[key]", body)

    def test_the_groups_combine_as_and(self):
        """One early exit per dimension, then true. An or would let a row
        matching any single group through, so picking a channel AND a node
        would widen the feed instead of narrowing it."""
        body = self.matches_body()
        self.assertEqual(body.count("return false"), 2,
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
        for group in ("channels", "nodes"):
            with self.subTest(group=group, where="initial"):
                self.assertRegex(
                    selected_init, group + r":\s*Object\.create\(null\)")
        cleared = JS[JS.index("function clearFilters()"):]
        cleared = cleared[:cleared.index("\n  }")]
        for group in ("channels", "nodes"):
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
