"""Colour coding in the public chatter feed.

Two dimensions are coloured, and neither is the message sender: the CAPTURE
NODE (which BBS node in the fleet heard this) carries the entry's left
stripe, and the CHANNEL carries a small swatch beside its own label. Sender
identity is answered in text by the name fields and does not need a colour.

Colour is an aid to scanning a busy feed, never the thing that carries the
information: every name, channel and hop count stays as text, and the whole
scheme switches off.

The silent failure these guard is drift between the palette in JavaScript and
the classes in CSS. If PALETTE_SIZE exceeds the defined classes, hashes land
on classes that do not exist and those entries render with no colour at all.
If it is smaller, the last colours are never used. Neither raises anything.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "public-chatter.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "public_chatter.html").read_text(encoding="utf-8")


def palette_size():
    return int(re.search(r"PALETTE_SIZE = (\d+)", JS).group(1))


def dark_classes():
    return {int(n) for n in re.findall(r"(?<!\] )\.cc-(\d+) \{", HTML)}


def light_classes():
    return {int(n) for n in re.findall(r'\[data-theme="light"\] \.cc-(\d+)', HTML)}


class PaletteConsistencyTests(unittest.TestCase):
    def test_every_palette_index_has_a_colour_defined(self):
        """The drift this file exists to catch: a hash landing on a class
        nobody wrote renders with no colour and reports no error."""
        missing = set(range(palette_size())) - dark_classes()
        self.assertEqual(missing, set(),
                         f"PALETTE_SIZE={palette_size()} but .cc-{sorted(missing)} "
                         "are not defined in the stylesheet")

    def test_no_colour_is_defined_that_can_never_be_reached(self):
        unreachable = {n for n in dark_classes() if n >= palette_size()}
        self.assertEqual(unreachable, set(),
                         f".cc-{sorted(unreachable)} can never be assigned "
                         f"with PALETTE_SIZE={palette_size()}")

    def test_every_colour_is_redefined_for_the_light_theme(self):
        """A hue picked to read on #0f141b is invisible on white."""
        self.assertEqual(dark_classes(), light_classes())

    def test_the_colours_are_actually_distinct(self):
        values = re.findall(r"\.cc-\d+ \{ --cc:(#[0-9a-fA-F]{6}); \}", HTML)
        self.assertEqual(len(values), len(set(values)),
                         "two palette entries share a hex value")


class WhatIsColouredTests(unittest.TestCase):
    """The capture node and the channel. Not the sender."""

    def test_the_stripe_is_keyed_to_the_capture_node(self):
        self.assertRegex(
            JS, r"function captureKey[\s\S]*?entry\.capture_node_id \|\| null")
        self.assertRegex(
            JS,
            r'article\.className = "chatter-entry "[\s\S]*?palette\.nodes\[capture\]')

    def test_the_channel_gets_its_own_swatch(self):
        self.assertRegex(
            JS, r'swatch\("cc-" \+ palette\.channels\[channelKey\(entry\)\]')

    def test_the_sender_is_not_coloured(self):
        """Deliberately dropped: who sent a message is answered in text, and
        colouring it competed with the two dimensions that matter."""
        self.assertNotIn("chatter-sender", JS)
        self.assertNotIn("chatter-sender", HTML)
        self.assertNotIn("paletteIndex(senderKey", JS)

    def test_the_two_dimensions_are_shaped_differently(self):
        """Round for a capture node, square for a channel, so they stay
        tellable apart without relying on where they sit in the row."""
        self.assertIn(".chatter-swatch.is-round", HTML)
        self.assertRegex(JS, r'swatch\("cc-" \+ palette\.nodes\[capture\], true')
        self.assertRegex(
            JS,
            r'swatch\("cc-" \+ palette\.channels\[channelKey\(entry\)\], false\)')


class NeutralWhenUnattributedTests(unittest.TestCase):
    def test_a_neutral_class_exists(self):
        self.assertIn(".cc-none", HTML)

    def test_it_is_not_a_palette_colour(self):
        neutral = re.search(r"\.cc-none \{ --cc:([^;]+); \}", HTML).group(1)
        self.assertIn("text-faint", neutral)

    def test_a_row_with_no_capture_node_gets_the_neutral_class(self):
        self.assertRegex(JS, r'capture === null \? "cc-none"')

    def test_the_legend_counts_them_rather_than_naming_one(self):
        """They are not one node, so they must not read as one."""
        self.assertIn('appendText(note, "span", "Not recorded")', JS)
        self.assertIn('"(" + unattributed + ")"', JS)

    def test_they_are_not_offered_as_a_filter(self):
        """"Not recorded" is an absence, not a station you could ask to see,
        so it renders as a static chip rather than a button."""
        self.assertIn('note.className = "legend-chip is-static"', JS)
        self.assertIn(".legend-chip.is-static { cursor:default", HTML)


class IdentityKeyTests(unittest.TestCase):
    def test_a_channel_colour_is_network_qualified(self):
        """meshcore channel 2 and meshtastic channel 2 are unrelated."""
        self.assertRegex(
            JS, r'function channelKey[\s\S]*?entry\.network[\s\S]*?channel_index')

    def test_collisions_among_present_keys_are_resolved(self):
        """Hash alone collides about half the time across a four-node fleet
        in ten buckets, which defeats the point of colouring at all."""
        self.assertIn("function assignColours", JS)
        self.assertRegex(JS, r"index = \(index \+ 1\) % PALETTE_SIZE")

    def test_the_assignment_does_not_depend_on_arrival_order(self):
        """Sorted before assigning, so the same fleet colours identically on
        every reload and on every node rather than shuffling per request."""
        self.assertRegex(JS, r"keys\.slice\(\)\.sort\(\)")

    def test_both_dimensions_are_assigned_from_one_pass(self):
        self.assertIn("function buildPalette", JS)
        self.assertIn("nodes: assignColours(", JS)
        self.assertIn("channels: assignColours(", JS)

    def test_the_probe_terminates_when_colours_run_out(self):
        """More nodes than colours must repeat, not loop forever."""
        self.assertRegex(JS, r"n < PALETTE_SIZE && taken\[index\]")

    def test_colours_are_derived_not_stored(self):
        """A node keeps its colour across reloads and across BBS nodes with
        nothing persisted, because it is a hash of the identity."""
        self.assertIn("function paletteIndex", JS)
        self.assertIn("% PALETTE_SIZE", JS)

    def test_a_long_node_key_is_shortened_for_display(self):
        """A MeshCore capture id is 64 hex characters and swamps the row."""
        self.assertIn("function shortNodeId", JS)

    def test_the_full_node_id_is_still_available(self):
        """Shortening must not lose it -- two nodes can share a head."""
        self.assertIn("label.title = capture", JS)


class ColourIsNeverTheOnlySignalTests(unittest.TestCase):
    def test_the_sender_name_is_still_rendered_as_text(self):
        self.assertIn('appendText(meta, "strong", name)', JS)

    def test_the_channel_is_still_rendered_as_text(self):
        self.assertIn('appendText(channelWrap, "span", channelLabel(entry))', JS)

    def test_the_capture_node_is_still_rendered_as_text(self):
        self.assertIn('"Heard by " + shortNodeId(capture)', JS)

    def test_swatches_are_hidden_from_assistive_technology(self):
        """They repeat the label beside them; announcing them would be noise."""
        self.assertIn('element.setAttribute("aria-hidden", "true")', JS)

    def test_there_is_no_switch_to_turn_colour_off(self):
        """Colour is always on. A toggle for it was one more control on a
        page that already has four, defending against a problem the plain
        text labels beside every swatch already solve."""
        for needle in ("chatter-colour", "chatter-toggle", "no-colour"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, HTML)
                self.assertNotIn(needle, JS)

    def test_no_preference_is_stored_for_it(self):
        """Nothing to remember, so nothing touches browser storage."""
        self.assertNotIn("localStorage", JS)


class LegendTests(unittest.TestCase):
    def test_the_legend_has_a_section_for_each_dimension(self):
        self.assertIn('id="legend-channels"', HTML)
        self.assertIn('id="legend-nodes"', HTML)
        self.assertIn("Heard by", HTML)

    def test_it_is_built_from_what_is_on_screen(self):
        """A legend of every channel that ever existed would be noise."""
        self.assertIn("function renderLegend(entries, palette)", JS)
        self.assertIn("legend.hidden = entries.length === 0", JS)

    def test_node_ids_cannot_collide_with_object_prototype(self):
        """Keys are ids straight off the air; one calling itself __proto__
        must get its own row, not corrupt the tally."""
        self.assertIn("Object.create(null)", JS)


class SourceIntegrityTests(unittest.TestCase):
    def test_the_script_holds_no_stray_control_characters(self):
        """A NUL smuggled in by an editing tool makes the file load as
        binary and the page silently loses its feed."""
        for name, text in (("js", JS), ("html", HTML)):
            with self.subTest(file=name):
                self.assertNotIn("\x00", text)


if __name__ == "__main__":
    unittest.main()
