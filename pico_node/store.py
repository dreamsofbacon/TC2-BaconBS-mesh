"""Bounded local read-cache for the Pico node (Option B).

Stores a capped copy of BBS content (bulletins, the node-user's mail) as small
JSON files so a low-power node can serve "everything" instantly, online or off,
without running a database or the sync engine. Designed for an SD card mounted
at e.g. /sd, but works on any writable path (a tmp dir under the host tests).

Size is bounded: each scope keeps at most ``max_records`` of the newest items
(by record 'date', newest kept), oldest pruned — so the on-card footprint has a
hard ceiling no matter how long the node runs.

Pure Python (os + json only); runs on CircuitPython and under CPython tests.
"""

import os
import json

SCOPES = ("bulletins", "mail", "channels")


class CacheStore:
    def __init__(self, base_path, max_records=200):
        self.base_path = base_path
        self.max_records = max_records
        # scope -> {unique_id: record dict}
        self._data = {s: {} for s in SCOPES}
        # op_log sync watermarks: origin_node_id -> last applied seq
        self._watermarks = {}
        self._dirty = set()

    # --- persistence -------------------------------------------------------
    def _path(self, name):
        return self.base_path + "/" + name

    def _ensure_dir(self):
        try:
            os.stat(self.base_path)
            return
        except OSError:
            pass
        makedirs = getattr(os, "makedirs", None)
        if makedirs is not None:  # CPython
            try:
                makedirs(self.base_path, exist_ok=True)
                return
            except TypeError:
                try:
                    makedirs(self.base_path)
                    return
                except OSError:
                    pass
            except OSError:
                pass
        # CircuitPython fallback: create each path component in turn.
        norm = self.base_path.replace("\\", "/")
        cur = ""
        for part in norm.split("/"):
            if part == "":
                cur = "/"
                continue
            cur = part if cur == "" else (cur + part if cur.endswith("/") else cur + "/" + part)
            try:
                os.mkdir(cur)
            except OSError:
                pass

    def load(self):
        """Read cache + watermarks from disk. Missing files = empty (first run)."""
        for scope in SCOPES:
            try:
                with open(self._path(scope + ".json"), "r") as f:
                    self._data[scope] = json.load(f)
            except (OSError, ValueError):
                self._data[scope] = {}
        try:
            with open(self._path("state.json"), "r") as f:
                state = json.load(f)
                self._watermarks = state.get("watermarks", {})
        except (OSError, ValueError):
            self._watermarks = {}
        self._dirty = set()
        return self

    def save(self):
        """Write back any scope that changed, plus watermark state."""
        if self._dirty:
            self._ensure_dir()
        for scope in list(self._dirty):
            if scope in SCOPES:
                with open(self._path(scope + ".json"), "w") as f:
                    json.dump(self._data[scope], f)
        if "state" in self._dirty:
            with open(self._path("state.json"), "w") as f:
                json.dump({"watermarks": self._watermarks}, f)
        self._dirty = set()

    # --- records -----------------------------------------------------------
    def upsert(self, scope, record):
        """Insert/replace a record (must carry a 'unique_id'). Prunes to cap."""
        if scope not in SCOPES:
            return
        uid = record.get("unique_id")
        if not uid:
            return
        self._data[scope][uid] = record
        self._dirty.add(scope)
        self._prune(scope)

    def delete(self, scope, uid):
        if scope in SCOPES and uid in self._data[scope]:
            del self._data[scope][uid]
            self._dirty.add(scope)

    def get(self, scope, board=None):
        """Return records for a scope, newest first. For bulletins, optionally
        filter by board."""
        if scope not in SCOPES:
            return []
        rows = list(self._data[scope].values())
        if board is not None:
            rows = [r for r in rows if r.get("board") == board]
        rows.sort(key=self._sort_key, reverse=True)
        return rows

    def count(self, scope):
        return len(self._data.get(scope, {}))

    @staticmethod
    def _sort_key(record):
        # 'date' is a gateway-provided string; pair with uid for stable order.
        return (str(record.get("date", "")), str(record.get("unique_id", "")))

    def _prune(self, scope):
        items = self._data[scope]
        if len(items) <= self.max_records:
            return
        # Keep the newest max_records by sort key; drop the rest.
        ordered = sorted(items.values(), key=self._sort_key, reverse=True)
        keep = ordered[: self.max_records]
        keep_ids = set(r.get("unique_id") for r in keep)
        for uid in list(items.keys()):
            if uid not in keep_ids:
                del items[uid]
        self._dirty.add(scope)

    # --- sync watermarks ---------------------------------------------------
    # Keyed per (origin_node_id, scope) -> highest applied op_log seq, matching
    # the gateway's op_log HAVE/WANT/EVENT model.
    @staticmethod
    def _wm_key(origin, scope):
        return "%s|%s" % (origin, scope)

    def get_watermark(self, origin, scope):
        return int(self._watermarks.get(self._wm_key(origin, scope), 0))

    def set_watermark(self, origin, scope, seq):
        seq = int(seq)
        key = self._wm_key(origin, scope)
        if seq > int(self._watermarks.get(key, 0)):
            self._watermarks[key] = seq
            self._dirty.add("state")

    def watermarks(self):
        return dict(self._watermarks)
