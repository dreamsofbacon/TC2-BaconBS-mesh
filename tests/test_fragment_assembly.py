"""Behavior tests for the shared, CircuitPython-safe Fragment Assembly module."""

import unittest

from pico_node.fragment_assembly import (
    ACCEPTED,
    CONFLICT,
    DUPLICATE,
    INVALID,
    FragmentAssembly,
)


class FragmentAssemblyTests(unittest.TestCase):
    def test_completion_requires_continuous_coverage_not_summed_lengths(self):
        assembly = FragmentAssembly()

        self.assertEqual(assembly.accept(0, "ABCDE", expected=10), ACCEPTED)
        self.assertEqual(assembly.accept(3, "DEFGH"), ACCEPTED)

        self.assertFalse(assembly.complete)
        self.assertIsNone(assembly.complete_text())
        self.assertEqual(assembly.prefix(), "ABCDEFGH")
        self.assertEqual(assembly.gaps(), [(8, 10)])

    def test_matching_overlap_extends_coverage_and_duplicates_are_idempotent(self):
        assembly = FragmentAssembly()

        self.assertEqual(assembly.accept(0, "ABCDE"), ACCEPTED)
        self.assertEqual(assembly.accept(3, "DEFG"), ACCEPTED)
        self.assertEqual(assembly.accept(2, "CDE"), DUPLICATE)
        self.assertEqual(assembly.prefix(), "ABCDEFG")

    def test_conflicting_overlap_is_rejected_atomically_and_requires_full_repair(self):
        assembly = FragmentAssembly()
        assembly.accept(0, "ABCDE", expected=5)

        self.assertEqual(assembly.accept(3, "XX"), CONFLICT)

        self.assertEqual(assembly.prefix(), "ABCDE")
        self.assertFalse(assembly.complete)
        self.assertTrue(assembly.repair_required)
        self.assertIsNone(assembly.gaps())

    def test_out_of_order_fragments_complete_after_gap_arrives(self):
        assembly = FragmentAssembly()

        assembly.accept(5, "FGHIJ", expected=15)
        assembly.accept(10, "KLMNO")
        self.assertEqual(assembly.gaps(), [(0, 5)])
        self.assertEqual(assembly.prefix(), "")

        assembly.accept(0, "ABCDE")
        self.assertTrue(assembly.complete)
        self.assertEqual(assembly.complete_text(), "ABCDEFGHIJKLMNO")

    def test_offsets_are_unicode_characters_while_emoji_use_multiple_bytes(self):
        assembly = FragmentAssembly()
        first = "A🙂B"
        self.assertGreater(len(first.encode("utf-8")), len(first))

        assembly.accept(0, first, expected=4)
        assembly.accept(3, "C")

        self.assertTrue(assembly.complete)
        self.assertEqual(assembly.complete_text(), "A🙂BC")

    def test_conflicting_declared_lengths_require_full_repair(self):
        assembly = FragmentAssembly()
        assembly.accept(0, "hello", expected=10)

        self.assertEqual(assembly.accept(expected=11), CONFLICT)
        self.assertTrue(assembly.repair_required)
        self.assertEqual(assembly.expected, 10)

    def test_reset_starts_a_clean_repair_generation(self):
        assembly = FragmentAssembly()
        assembly.accept(0, "ABCDE", expected=5)
        assembly.accept(2, "XX")

        assembly.reset()
        self.assertFalse(assembly.repair_required)
        self.assertEqual(assembly.accept(0, "VWXYZ", expected=5), ACCEPTED)
        self.assertEqual(assembly.complete_text(), "VWXYZ")

    def test_invalid_input_does_not_mutate_accepted_content(self):
        assembly = FragmentAssembly(max_characters=5)
        assembly.accept(0, "abc", expected=5)

        self.assertEqual(assembly.accept(-1, "x"), INVALID)
        self.assertEqual(assembly.accept(3, "def"), INVALID)
        self.assertEqual(assembly.prefix(), "abc")
        self.assertFalse(assembly.repair_required)

    def test_zero_length_payload_can_complete_from_metadata(self):
        assembly = FragmentAssembly()

        self.assertEqual(assembly.accept(expected=0), ACCEPTED)
        self.assertTrue(assembly.complete)
        self.assertEqual(assembly.complete_text(), "")


if __name__ == "__main__":
    unittest.main()
