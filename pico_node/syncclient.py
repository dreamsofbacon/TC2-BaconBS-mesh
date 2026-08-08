"""Ties the Pico's op_log discovery + record parsing into the cache.

handle_frame(frame) consumes one incoming sync frame and returns a list of
frames to transmit back to the gateway:

  HAVE                -> WANT(s) for scopes we're behind on
  EVENT               -> advance watermark; HASHMISS for a record we lack
  BULLETIN/MAIL/...    -> reassemble + upsert into the cache (no reply)
  *CONT / *META        -> continue reassembly
  DELETE_*             -> drop from the cache

It also tracks records it requested via HASHMISS but hasn't received yet, so a
caller can re-request them on the next wake (the Pico's stand-in for the
hash-repair self-heal the full nodes run). Pure logic; host-tested.
"""

import opsync
import records

_DELETE_PREFIXES = (
    ("DELETE_BULLETIN|", "bulletins"),
    ("DELETE_MAIL|", "mail"),
    ("DELETE_CHANNELCOMMENT|", "channels"),
)

_CACHE_TO_SYNC_SCOPE = {
    "bulletins": "bulletins",
    "mail": "mail",
    "channels": "channel_comments",
}


class SyncClient:
    def __init__(self, store):
        self.store = store
        self._ra = records.RecordAssembler()
        # scope -> set(uid) requested but not yet stored
        self._awaiting = {}

    def handle_frame(self, frame):
        if frame.startswith("HAVE|"):
            return opsync.wants_for_have(frame, self.store)
        if frame.startswith("EVENT|"):
            hm = opsync.apply_event(frame, self.store)
            if hm:
                # HASHMISS|scope|uid -> remember we're waiting for it
                p = hm.split("|")
                if len(p) == 3:
                    self._awaiting.setdefault(p[1], set()).add(p[2])
                return [hm]
            return []
        for prefix, scope in _DELETE_PREFIXES:
            if frame.startswith(prefix):
                parts = frame.split("|", 1)
                if len(parts) == 2 and parts[1]:
                    self.store.delete(scope, parts[1])
                return []
        res = self._ra.feed(frame)
        if res:
            scope, record, complete = res
            self.store.upsert(scope, record)
            if complete:
                self._clear_awaiting(record.get("unique_id"))
        repairs = []
        for cache_scope, uid in self._ra.pop_repairs():
            sync_scope = _CACHE_TO_SYNC_SCOPE.get(cache_scope, cache_scope)
            self._awaiting.setdefault(sync_scope, set()).add(uid)
            frame = opsync.build_hashmiss(sync_scope, uid)
            if frame not in repairs:
                repairs.append(frame)
        return repairs

    def _clear_awaiting(self, uid):
        for uids in self._awaiting.values():
            uids.discard(uid)

    def outstanding_hashmiss(self):
        """Re-request frames for records we asked for but never received. The
        caller sends these on the next wake to recover from a lost reply."""
        out = []
        # Keys are already op_log scope names (from the EVENT/HASHMISS that
        # created them: bulletins / mail / channel_comments).
        for scope, uids in self._awaiting.items():
            for uid in uids:
                out.append(opsync.build_hashmiss(scope, uid))
        return out
