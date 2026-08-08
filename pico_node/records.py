"""Parse BBS record frames into cache records (Pico side).

Faithfully mirrors the receive-side parsing in the full nodes' message_processing
for the three scopes the Pico caches, in the *legacy* encoding peers use when a
node advertises no wire capabilities (which the Pico doesn't):

  BULLETIN|board|sender|subject|content|uid[|date[|!src|src_ts]]
  MAIL|sender_id|sender|recipient|subject|content|uid[|date[|!src|src_ts]]
  CHANNELCOMMENT|channel_key|b64_sender|date|content|uid[|!src|src_ts]
  <PREFIX>CONT|uid|offset|chunk     and     <PREFIX>META|uid|total_len

Content is raw UTF-8 (no base64); only the channel-comment sender is base64
(legacy text encoding). rsplit-from-the-right handles '|' embedded in content,
exactly like the server. Multi-packet records are reassembled by char offset.

Pure Python (binascii only); host-tested, CircuitPython-safe.
"""

import binascii

try:
    from .fragment_assembly import CONFLICT, INVALID, FragmentAssembly
except ImportError:  # Files are copied flat onto CIRCUITPY.
    from fragment_assembly import CONFLICT, INVALID, FragmentAssembly

# CONT/META prefix -> (cache_scope, kind)
_CHUNK_PREFIXES = (
    ("BULLETINCONT|", "bulletins", "cont"),
    ("BULLETINMETA|", "bulletins", "meta"),
    ("MAILCONT|", "mail", "cont"),
    ("MAILMETA|", "mail", "meta"),
    ("CHANNELCOMMENTCONT|", "channels", "cont"),
    ("CHANNELCOMMENTMETA|", "channels", "meta"),
)

_MAX_INFLIGHT = 24  # cap reassembly buffers so memory stays bounded


def _is_date(s):
    if not s:
        return False
    if s[0] == "m" and s[1:].isdigit():
        return True
    if (len(s) == 16 and s[4] == "-" and s[7] == "-" and s[10] == " " and s[13] == ":"):
        return (s[0:4] + s[5:7] + s[8:10] + s[11:13] + s[14:16]).isdigit()
    return False


def _is_iso_ts(s):
    if not s:
        return False
    if s[0] == "s" and s[1:].isdigit():
        return True
    if (len(s) == 19 and s[4] == "-" and s[7] == "-" and s[10] == "T"
            and s[13] == ":" and s[16] == ":"):
        return (s[0:4] + s[5:7] + s[8:10] + s[11:13] + s[14:16] + s[17:19]).isdigit()
    return False


def _pipe_unescape(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            out.append("\\" if nxt == "\\" else ("|" if nxt == "p" else nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _decode_text(token):
    """Decode a wire text field: '~'-prefixed plain (nob64) or legacy base64."""
    if not token:
        return ""
    if token[0] == "~":
        return _pipe_unescape(token[1:])
    try:
        return binascii.a2b_base64(token).decode("utf-8")
    except Exception:
        return token  # best-effort: surface the raw token rather than fail


def _strip_provenance(body):
    """Remove an optional trailing |!src|src_ts (ISO/epoch second). Returns body."""
    tmp = body.rsplit("|", 1)
    if len(tmp) == 2 and _is_iso_ts(tmp[1]):
        body = tmp[0]
        tmp2 = body.rsplit("|", 1)
        if len(tmp2) == 2 and tmp2[1].startswith("!"):
            body = tmp2[0]
    return body


def parse_bulletin(message):
    body = _strip_provenance(message[len("BULLETIN|"):])
    date, uid, header_content = _split_uid_date(body)
    hparts = header_content.split("|", 3)
    if len(hparts) != 4:
        return None
    return {"unique_id": uid, "board": hparts[0], "sender": hparts[1],
            "subject": hparts[2], "date": date or "", "_content0": hparts[3]}


def parse_mail(message):
    body = _strip_provenance(message[len("MAIL|"):])
    date, uid, header_content = _split_uid_date(body)
    hparts = header_content.split("|", 4)
    if len(hparts) != 5:
        return None
    return {"unique_id": uid, "sender_id": hparts[0], "sender": hparts[1],
            "recipient": hparts[2], "subject": hparts[3], "date": date or "",
            "_content0": hparts[4]}


def parse_channel_comment(message):
    body = _strip_provenance(message[len("CHANNELCOMMENT|"):])
    tail = body.rsplit("|", 1)
    if len(tail) != 2 or not tail[1]:
        return None
    uid = tail[1]
    hparts = tail[0].split("|", 3)
    if len(hparts) != 4:
        return None
    return {"unique_id": uid, "channel_key": hparts[0],
            "sender": _decode_text(hparts[1]), "date": hparts[2] or "",
            "_content0": hparts[3]}


def _split_uid_date(body):
    """Shared bulletin/mail tail handling: returns (date_or_None, uid, header_content)."""
    tail = body.rsplit("|", 2)
    if len(tail) == 3 and _is_date(tail[2]):
        return tail[2], tail[1], tail[0]
    tail2 = body.rsplit("|", 1)
    if len(tail2) == 2:
        return None, tail2[1], tail2[0]
    return None, body, ""


class RecordAssembler:
    """Turns a stream of record + CONT/META frames into complete cache records.

    feed(frame) returns (cache_scope, record_dict, complete_bool) whenever a
    record is touched, else None. The caller upserts record_dict into the store;
    multi-packet records are re-emitted with more content as chunks arrive."""

    def __init__(self):
        self._buf = {}  # uid -> {scope, fields, assembly}
        self._repairs = []

    def feed(self, frame):
        if frame.startswith("BULLETIN|"):
            return self._base("bulletins", parse_bulletin(frame))
        if frame.startswith("MAIL|"):
            return self._base("mail", parse_mail(frame))
        if frame.startswith("CHANNELCOMMENT|") and not frame.startswith(
                ("CHANNELCOMMENTCONT|", "CHANNELCOMMENTMETA|")):
            return self._base("channels", parse_channel_comment(frame))
        for prefix, scope, kind in _CHUNK_PREFIXES:
            if frame.startswith(prefix):
                return self._chunk(frame, prefix, scope, kind)
        return None

    def _base(self, scope, parsed):
        if not parsed:
            return None
        uid = parsed["unique_id"]
        content0 = parsed.pop("_content0")
        buf = self._buf.get(uid)
        if buf is None:
            buf = {"scope": scope, "fields": {}, "assembly": FragmentAssembly()}
            self._evict_if_needed()
            self._buf[uid] = buf
        buf["scope"] = scope
        buf["fields"] = parsed
        assembly = buf["assembly"]
        # A base frame following a rejected conflict is the start of the full
        # record replay requested by SyncClient.
        if assembly.repair_required:
            assembly.reset(expected=assembly.expected)
        outcome = assembly.accept(0, content0)
        if outcome == CONFLICT:
            self._queue_repair(scope, uid)
        elif outcome == INVALID:
            return None
        return self._emit(uid)

    def _chunk(self, frame, prefix, scope, kind):
        if kind == "meta":
            parts = frame.split("|", 2)
            if len(parts) != 3 or not parts[1]:
                return None
            uid = parts[1]
            try:
                expected = int(parts[2])
            except ValueError:
                return None
            buf = self._buf.get(uid)
            if buf is None:
                return None  # nothing to attach to yet
            outcome = buf["assembly"].accept(expected=expected)
            if outcome == CONFLICT:
                self._queue_repair(scope, uid)
            elif outcome == INVALID:
                return None
            return self._emit(uid)
        # cont
        parts = frame.split("|", 3)
        if len(parts) < 3 or not parts[1]:
            return None
        uid = parts[1]
        buf = self._buf.get(uid)
        if buf is None:
            return None
        if len(parts) == 4:
            try:
                offset = int(parts[2])
            except ValueError:
                return None
            chunk = parts[3]
        else:  # legacy blind append
            offset = len(buf["assembly"].prefix())
            chunk = parts[2]
        outcome = buf["assembly"].accept(offset, chunk)
        if outcome == CONFLICT:
            self._queue_repair(scope, uid)
        elif outcome == INVALID:
            return None
        return self._emit(uid)

    def _emit(self, uid):
        buf = self._buf.get(uid)
        if buf is None or not buf["fields"]:
            return None
        assembly = buf["assembly"]
        record = dict(buf["fields"])
        record["content"] = assembly.prefix()
        expected = assembly.expected
        complete = ((expected is None and not assembly.repair_required)
                    or assembly.complete)
        # Once a multi-packet record is fully assembled, free its buffer.
        if expected is not None and complete:
            del self._buf[uid]
        return (buf["scope"], record, complete)

    def pop_repairs(self):
        """Return and clear records that need a trustworthy full replay."""
        repairs = list(self._repairs)
        self._repairs = []
        return repairs

    def _queue_repair(self, scope, uid):
        item = (scope, uid)
        if item not in self._repairs:
            self._repairs.append(item)

    def _evict_if_needed(self):
        if len(self._buf) >= _MAX_INFLIGHT:
            # Drop an arbitrary oldest-ish buffer; the record will be re-fetched
            # via HASHMISS if it was still needed.
            self._buf.pop(next(iter(self._buf)))
