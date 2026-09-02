"""Colour coding in the public chatter feed.

Colour is an aid to scanning a busy feed, never the thing that carries the
information: every station name, channel name and hop count stays as text,
and the whole scheme switches off. These tests hold that line, and guard the
two ways the scheme can break without anyone noticing.

The silent failure is drift between the palette in JavaScript and the classes
in CSS. If PALETTE_SIZE exceeds the defined classes, hashes land on classes
that do not exist and those entries render with no colour at all. If it is
smaller, the last colours are simply never used. Neither raises anything.
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


class NeutralForUnknownTests(unittest.TestCase):
    """MeshCore channel messages carry no sender identity, so many entries
    have nobody to colour. Painting them all one colour would say they are
    one station, which is a stronger and wronger claim than saying nothing."""

    def test_a_neutral_class_exists(self):
        self.assertIn(".cc-none", HTML)

    def test_it_is_not_a_palette_colour(self):
        neutral = re.search(r"\.cc-none \{ --cc:([^;]+); \}", HTML).group(1)
        self.assertIn("text-faint", neutral)

    def test_an_unidentifiable_sender_gets_the_neutral_class(self):
        self.assertIn('senderKey(entry)', JS)
        self.assertIn('key === null ? "cc-none"', JS)

    def test_the_legend_does_not_name_an_unknown_station(self):
        """It reports a count, not a name, so several strangers never look
        like one person."""
        self.assertIn('label: "Unknown sender", index: null', JS)


class IdentityKeyTests(unittest.TestCase):
    def test_a_channel_colour_is_network_qualified(self):
        """meshcore channel 2 and meshtastic channel 2 are unrelated."""
        self.assertRegex(
            JS, r'function channelKey[\s\S]*?entry\.network[\s\S]*?channel_index')

    def test_a_sender_prefers_the_node_id_over_a_parsed_name(self):
        """A node id is authoritative; a MeshCore name is a body prefix
        anyone could type."""
        self.assertRegex(
            JS,
            r"function senderKey[\s\S]*?entry\.sender_node_id \|\| "
            r"entry\.sender_name \|\| null")

    def test_colours_are_derived_not_stored(self):
        """A station keeps its colour across reloads and across nodes with
        nothing persisted, because it is a hash of the identity."""
        self.assertIn("function paletteIndex", JS)
        self.assertIn("% PALETTE_SIZE", JS)


class ColourIsNeverTheOnlySignalTests(unittest.TestCase):
    def test_the_station_name_is_still_rendered_as_text(self):
        self.assertIn("strong.textContent = name", JS)

    def test_the_channel_is_still_rendered_as_text(self):
        self.assertIn('appendText(meta, "span", channelLabel(entry))', JS)

    def test_the_dot_is_hidden_from_assistive_technology(self):
        """It repeats the name beside it; announcing it would be noise."""
        self.assertIn('dot.setAttribute("aria-hidden", "true")', JS)

    def test_colour_can_be_switched_off_entirely(self):
        self.assertIn('id="chatter-colour"', HTML)
        self.assertIn("no-colour", HTML)
        self.assertIn("no-colour", JS)

    def test_switching_it_off_restores_readable_text(self):
        """Not just 'remove the colour' -- the name must go back to full
        contrast rather than inheriting a stripe colour."""
        self.assertRegex(
            HTML, r"\.no-colour \.chatter-sender \{ color:var\(--text")

    def test_the_preference_survives_a_reload(self):
        self.assertIn("localStorage.setItem(COLOUR_KEY", JS)
        self.assertIn("localStorage.getItem(COLOUR_KEY)", JS)

    def test_storage_failure_does_not_break_the_page(self):
        """Private windows and blocked site data throw on access."""
        self.assertRegex(JS, r"try \{ localStorage\.setItem\(COLOUR_KEY[^}]*\} catch")


class LegendTests(unittest.TestCase):
    def test_the_legend_has_a_section_for_each_dimension(self):
        self.assertIn('id="legend-channels"', HTML)
        self.assertIn('id="legend-senders"', HTML)

    def test_it_is_built_from_what_is_on_screen(self):
        """A legend of every channel that ever existed would be noise."""
        self.assertIn("function renderLegend(entries)", JS)
        self.assertIn("legend.hidden = entries.length === 0", JS)

    def test_station_names_cannot_collide_with_object_prototype(self):
        """Keys are names straight off the air; a station calling itself
        __proto__ must get its own row, not corrupt the tally."""
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
