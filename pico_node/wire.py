"""Portable BBS wire-grammar for the Pico node.

A tiny, dependency-free re-implementation of the pieces of the CPython BBS wire
protocol the Pico actually needs: build APIREQ/APIPOLL/APIRESPGAP frames and
reassemble a chunked APIRESP. Mirrors the offset-based reassembly in the main
repo (utils.py / message_processing.py) but with no DB, no threads, no radio —
just string in, string out — so it runs on CircuitPython and under CPython
tests alike.
"""

US = "\x1f"  # unit separator inside payloads (keeps '|' as the frame delimiter)


def node_id_to_num(node_id):
    """'!0408b778' -> 67403640 (int). Accepts with/without leading '!'."""
    s = node_id[1:] if node_id and node_id[0] == "!" else node_id
    return int(s, 16)


def num_to_node_id(num):
    """67403640 -> '!0408b778'."""
    return "!%08x" % (num & 0xFFFFFFFF)


def build_apireq(rid, requester_id, kind, payload):
    return "APIREQ|%s|%s|%s|%s" % (rid, requester_id, kind, payload)


def build_ai_request(rid, requester_id, prompt):
    """Convenience: an AI relay request (kind 'r', target 'ai')."""
    return build_apireq(rid, requester_id, "r", "ai" + US + prompt)


def build_http_request(rid, requester_id, method, url, body=""):
    return build_apireq(rid, requester_id, "h", method + US + url + US + body)


def build_apipoll(node_id):
    return "APIPOLL|%s" % node_id


def build_apirespgap(rid, ranges="*"):
    return "APIRESPGAP|%s|%s" % (rid, ranges)


class ResponseAssembler:
    """Reassembles chunked APIRESP frames keyed by rid.

    Feed each incoming API* frame string via feed(); when a response completes,
    feed() returns (rid, status, body) and forgets it, else None. compute_gaps()
    returns the missing byte ranges for a still-incomplete rid so the caller can
    send an APIRESPGAP."""

    def __init__(self):
        # rid -> {"status": str, "expected": int|None, "parts": {offset: chunk}}
        self._buffers = {}

    def feed(self, frame):
        if frame.startswith("APIRESP|"):
            parts = frame.split("|", 4)
            if len(parts) < 4 or not parts[1]:
                return None
            rid, status = parts[1], parts[2]
            try:
                expected = int(parts[3])
            except ValueError:
                return None
            chunk = parts[4] if len(parts) == 5 else ""
            return self._apply(rid, 0, chunk, status, expected)
        if frame.startswith("APIRESPMETA|"):
            parts = frame.split("|", 2)
            if len(parts) != 3 or not parts[1]:
                return None
            try:
                expected = int(parts[2])
            except ValueError:
                return None
            return self._apply(parts[1], None, None, None, expected)
        if frame.startswith("APIRESPCONT|"):
            parts = frame.split("|", 3)
            if len(parts) != 4 or not parts[1]:
                return None
            try:
                offset = int(parts[2])
            except ValueError:
                return None
            return self._apply(parts[1], offset, parts[3], None, None)
        return None

    def _apply(self, rid, offset, chunk, status, expected):
        buf = self._buffers.get(rid)
        if buf is None:
            buf = {"status": "", "expected": None, "parts": {}}
            self._buffers[rid] = buf
        if status is not None:
            buf["status"] = status
        if expected is not None:
            buf["expected"] = int(expected)
        if offset is not None and chunk is not None:
            buf["parts"][int(offset)] = chunk
        assembled = sum(len(c) for c in buf["parts"].values())
        if buf["expected"] is not None and assembled >= buf["expected"]:
            body = "".join(buf["parts"][o] for o in sorted(buf["parts"]))
            st = buf["status"]
            del self._buffers[rid]
            return (rid, st, body)
        return None

    def pending_rids(self):
        return list(self._buffers.keys())

    def compute_gaps(self, rid):
        """Return missing (start, end) ranges for rid, or [] if complete/unknown.
        Returns [(0, 0)] sentinel meaning 'ask for everything' when the total
        length isn't known yet."""
        buf = self._buffers.get(rid)
        if buf is None:
            return []
        expected = buf["expected"]
        if not expected:
            return None  # length unknown -> caller should request a full resend
        parts = buf["parts"]
        gaps = []
        cursor = 0
        for off in sorted(parts):
            if off > cursor:
                gaps.append((cursor, off))
            end = off + len(parts[off])
            if end > cursor:
                cursor = end
        if cursor < expected:
            gaps.append((cursor, expected))
        return gaps

    def gap_spec(self, rid):
        """Render the APIRESPGAP range spec for rid: '*' for a full resend, or
        'a-b,c-d', or None if nothing to request."""
        gaps = self.compute_gaps(rid)
        if gaps is None:
            return "*"
        if not gaps:
            return None
        return ",".join("%d-%d" % (a, b) for a, b in gaps)
