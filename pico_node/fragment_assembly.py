"""Loss-tolerant text Fragment Assembly for CPython and CircuitPython.

Wire offsets are Unicode character positions.  Radio adapters remain
responsible for fitting each Fragment to their UTF-8 byte ceiling.

The module intentionally has no database, clock, threading, or radio
dependencies so every receiver can share the same overlap and completion
rules.
"""

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
CONFLICT = "conflict"
INVALID = "invalid"


class FragmentAssembly:
    """Collect trustworthy text Fragments behind one small interface.

    ``accept`` rejects a Conflicting Overlap atomically: previously accepted
    text is retained, the incoming Fragment is discarded, and
    ``repair_required`` becomes true.  Completion requires exact continuous
    coverage from character zero through the declared expected length.
    """

    def __init__(self, max_characters=None):
        if max_characters is not None:
            if isinstance(max_characters, bool) or not isinstance(max_characters, int):
                raise ValueError("max_characters must be an integer or None")
            if max_characters < 0:
                raise ValueError("max_characters cannot be negative")
        self._max_characters = max_characters
        self.reset()

    def reset(self, expected=None):
        """Start a clean repair generation, optionally declaring its length."""
        if expected is not None and not self._valid_length(expected):
            raise ValueError("invalid expected length")
        self._parts = []
        self._expected = expected
        self._repair_required = False
        self._last_issue = None

    @property
    def expected(self):
        return self._expected

    @property
    def repair_required(self):
        return self._repair_required

    @property
    def last_issue(self):
        return self._last_issue

    @property
    def complete(self):
        gaps = self.gaps()
        return gaps == []

    def accept(self, offset=None, text=None, expected=None):
        """Accept one Fragment and/or an authoritative expected length.

        Returns ``accepted``, ``duplicate``, ``conflict``, or ``invalid``.
        Supplying only ``expected`` models a META frame.  A Fragment requires
        both ``offset`` and ``text``.
        """
        has_offset = offset is not None
        has_text = text is not None
        if has_offset != has_text:
            self._last_issue = "incomplete_fragment"
            return INVALID
        if expected is None and not has_offset:
            self._last_issue = "empty_operation"
            return INVALID
        if expected is not None and not self._valid_length(expected):
            self._last_issue = "invalid_length"
            return INVALID

        if expected is not None and self._expected is not None and expected != self._expected:
            self._last_issue = "length_conflict"
            self._repair_required = True
            return CONFLICT

        effective_expected = self._expected if self._expected is not None else expected
        if expected is not None:
            for part_offset, part_text in self._parts:
                if part_offset + len(part_text) > expected:
                    self._last_issue = "length_conflict"
                    self._repair_required = True
                    return CONFLICT

        if has_offset:
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                self._last_issue = "invalid_offset"
                return INVALID
            if not isinstance(text, str):
                self._last_issue = "invalid_text"
                return INVALID
            fragment_end = offset + len(text)
            if self._max_characters is not None and fragment_end > self._max_characters:
                self._last_issue = "limit_exceeded"
                return INVALID
            if effective_expected is not None and fragment_end > effective_expected:
                self._last_issue = "out_of_range"
                return INVALID

            for part_offset, part_text in self._parts:
                part_end = part_offset + len(part_text)
                overlap_start = max(offset, part_offset)
                overlap_end = min(fragment_end, part_end)
                if overlap_start >= overlap_end:
                    continue
                incoming = text[overlap_start - offset:overlap_end - offset]
                accepted = part_text[overlap_start - part_offset:overlap_end - part_offset]
                if incoming != accepted:
                    self._last_issue = "conflicting_overlap"
                    self._repair_required = True
                    return CONFLICT

        metadata_changed = expected is not None and self._expected is None
        if metadata_changed:
            self._expected = expected

        coverage_before = self._covered_characters()
        if has_offset and text:
            self._parts.append([offset, text])
            self._coalesce_parts()
        coverage_changed = self._covered_characters() > coverage_before

        self._last_issue = None
        if metadata_changed or coverage_changed:
            return ACCEPTED
        return DUPLICATE

    def prefix(self):
        """Return only trustworthy text continuously covered from character 0."""
        if not self._parts or self._parts[0][0] != 0:
            return ""
        text = self._parts[0][1]
        if self._expected is not None:
            return text[:self._expected]
        return text

    def complete_text(self):
        """Return the logical payload only after trustworthy completion."""
        if not self.complete:
            return None
        return self.prefix()

    def gaps(self):
        """Return missing half-open character ranges, or None for full repair.

        ``None`` means either the total is unknown or a conflict made targeted
        ranges untrustworthy.  Adapters render that state as a full resend.
        """
        if self._repair_required or self._expected is None:
            return None
        gaps = []
        cursor = 0
        for part_offset, part_text in self._parts:
            if part_offset >= self._expected:
                break
            if part_offset > cursor:
                gaps.append((cursor, min(part_offset, self._expected)))
            cursor = max(cursor, min(part_offset + len(part_text), self._expected))
            if cursor >= self._expected:
                break
        if cursor < self._expected:
            gaps.append((cursor, self._expected))
        return gaps

    def _valid_length(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
        return self._max_characters is None or value <= self._max_characters

    def _covered_characters(self):
        return sum(len(part_text) for _, part_text in self._parts)

    def _coalesce_parts(self):
        ordered = sorted(self._parts, key=lambda part: part[0])
        merged = []
        for part_offset, part_text in ordered:
            if not part_text:
                continue
            if not merged:
                merged.append([part_offset, part_text])
                continue
            previous_offset, previous_text = merged[-1]
            previous_end = previous_offset + len(previous_text)
            if part_offset > previous_end:
                merged.append([part_offset, part_text])
                continue
            part_end = part_offset + len(part_text)
            if part_end > previous_end:
                merged[-1][1] = previous_text + part_text[previous_end - part_offset:]
        self._parts = merged
