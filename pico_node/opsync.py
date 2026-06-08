"""op_log discovery client for the Pico (HAVE / WANT / EVENT / HASHMISS).

Speaks the *same* discovery protocol the full BBS nodes use (op_sync.py), but
as a pull-only subscriber: it can't run the HASHZ hash-repair layer, so it
fetches each record it's missing with a HASHMISS and parses the record frame
the owner sends back (see records.py).

Flow on the Pico:
  receive HAVE|origin|scope:seq|...      -> send WANT for any scope we're behind
  receive EVENT|scope|origin|seq|t|uid   -> advance watermark; if an upsert we
                                            don't have, send HASHMISS|scope|uid;
                                            if a delete, drop it from the cache
  (the record frame that follows is handled by records.py -> store.upsert)

Pure string/cache logic — no radio, no hardware — so the whole flow is unit
tested on the host. The scope names here are the long forms; the Pico does not
advertise the 'scc' capability, so peers send long scope names to it.
"""

SUPPORTED_SCOPES = ("bulletins", "mail", "channel_comments")

# op_log scope -> local cache scope (the cache lumps channel_comments under
# "channels" for the reader; mail/bulletins map 1:1).
SCOPE_TO_CACHE = {
    "bulletins": "bulletins",
    "mail": "mail",
    "channel_comments": "channels",
}

# Single-char scope codes a peer may use if it thinks we support 'scc'. We never
# advertise scc, but decode them defensively so a mis-encoded frame still works.
_CODE_TO_SCOPE = {"b": "bulletins", "m": "mail", "C": "channel_comments"}


def _decode_scope(token):
    if token in SUPPORTED_SCOPES:
        return token
    return _CODE_TO_SCOPE.get(token, token)


def parse_have(frame):
    """'HAVE|origin|scope:seq|...' -> (origin, {scope: seq}) or (None, {})."""
    parts = frame.split("|")
    if len(parts) < 3 or parts[0] != "HAVE" or not parts[1]:
        return None, {}
    origin = parts[1]
    heads = {}
    for field in parts[2:]:
        if ":" not in field:
            continue
        colon = field.rfind(":")
        scope = _decode_scope(field[:colon])
        if scope not in SUPPORTED_SCOPES:
            continue
        try:
            heads[scope] = int(field[colon + 1:])
        except ValueError:
            continue
    return origin, heads


def wants_for_have(frame, store):
    """Return WANT frames for every scope where the peer is ahead of us."""
    origin, heads = parse_have(frame)
    if not origin:
        return []
    out = []
    for scope, their_seq in heads.items():
        our_head = store.get_watermark(origin, scope)
        if their_seq > our_head:
            out.append(build_want(scope, origin, our_head + 1))
    return out


def build_want(scope, origin, from_seq):
    return "WANT|%s|%s|%d" % (scope, origin, from_seq)


def build_hashmiss(scope, uid):
    return "HASHMISS|%s|%s" % (scope, uid)


def apply_event(frame, store):
    """Process an EVENT frame. Advances the watermark and returns a HASHMISS
    frame to send when we're missing the upserted record, else None. Delete
    events are applied to the cache immediately."""
    parts = frame.split("|")
    if len(parts) != 6 or parts[0] != "EVENT":
        return None
    scope = _decode_scope(parts[1])
    origin = parts[2]
    if scope not in SUPPORTED_SCOPES or not origin:
        return None
    try:
        seq = int(parts[3])
    except ValueError:
        return None
    event_type = parts[4]
    uid = parts[5]
    if not uid:
        return None

    store.set_watermark(origin, scope, seq)
    cache_scope = SCOPE_TO_CACHE[scope]

    if event_type == "delete":
        store.delete(cache_scope, uid)
        return None
    if event_type == "upsert":
        # Already have it? Then the watermark bump above is all that's needed.
        have = any(r.get("unique_id") == uid for r in store.get(cache_scope))
        if not have:
            return build_hashmiss(scope, uid)
    return None
