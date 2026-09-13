"""Who a player is, in the two forms the BBS needs.

Scores, game saves and profiles are filed under a player id. For MeshCore
radios that used to be int(public_key[:8], 16): 32 bits of the key. Two
MeshCore players whose keys shared 8 hex characters, or a MeshCore player whose
number happened to equal a Meshtastic node number, were silently merged into
one score row, one save slot and one profile.

A MeshCore player is now identified by the 12-hex-character prefix of their
public key. That is exactly what MeshCore transmits with every message
(meshcore's reader.py: pubkey_prefix = dbuf.read(6).hex()), so it is the same
whether or not the sender is already in the radio's contact list. The full
64-character key is not: it is only known for existing contacts, and using it
would have split one person into two identities depending on that.

It exists in two forms that convert exactly into each other:

  runtime number  MESHCORE_NUM_BASE + int(prefix, 16)
                  What the radio layer hands the rest of the BBS as a sender.
                  Always above 2**32, so it can never equal a Meshtastic node
                  number, an SSH sender number, an emulator number or a
                  broadcast address. Identical on every node.

  stored key      "mc-" + prefix
                  What scores, saves and profiles are filed under. The
                  separator is "-" and never ":", because tombstone and sync
                  keys are "user_id:game_id" split on the first colon -- by
                  this code and by any peer still running older code.

Meshtastic, SSH and emulator players are unchanged: their stored key is the
decimal number they always had.

No imports, deliberately: both the MeshCore radio layer and the database layer
depend on this, and neither should have to import the other to do it.
"""

MESHCORE_NUM_BASE = 1 << 48
MESHCORE_PREFIX_HEX = 12
MESHCORE_PLAYER_PREFIX = "mc-"

_HEX = frozenset("0123456789abcdef")


def meshcore_prefix(public_key) -> str:
    """The 12-hex identity prefix of a MeshCore key, or '' if it has none.

    A key shorter than 12 hex characters is padded, which only happens for a
    malformed or hand-configured identifier: every real MeshCore message
    carries at least 12.
    """
    # Normalised exactly as meshcore_interface._clean_key does, so the old and
    # new numbers are derived from identical input.
    text = "".join(str(public_key or "").split()).lower()
    if text.startswith("!"):
        text = text[1:]
    if not text or any(ch not in _HEX for ch in text):
        return ""
    return (text + "0" * MESHCORE_PREFIX_HEX)[:MESHCORE_PREFIX_HEX]


def meshcore_player_number(public_key) -> int:
    """The runtime sender number for a MeshCore key."""
    prefix = meshcore_prefix(public_key)
    if prefix:
        return MESHCORE_NUM_BASE + int(prefix, 16)
    # Not a hex key at all. Still kept in MeshCore's own range, so a malformed
    # identifier cannot collide with a Meshtastic or SSH number either.
    value = 2166136261
    for byte in str(public_key or "").encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return MESHCORE_NUM_BASE + value


def is_meshcore_number(value) -> bool:
    try:
        return int(value) >= MESHCORE_NUM_BASE
    except (TypeError, ValueError):
        return False


def player_key(user_id) -> str:
    """The key a player's scores, saves and profile are stored under.

    Accepts a runtime number, a decimal string, or an existing "mc-" key, and
    always returns the same key for the same player -- which is what lets every
    database function call this on the way in without its callers changing.
    """
    if isinstance(user_id, str):
        text = user_id.strip()
        if text.lower().startswith(MESHCORE_PLAYER_PREFIX):
            return text.lower()
        if not text.isdigit():
            return text
        number = int(text)
    else:
        try:
            number = int(user_id)
        except (TypeError, ValueError):
            return str(user_id)
    if number >= MESHCORE_NUM_BASE:
        return MESHCORE_PLAYER_PREFIX + format(number - MESHCORE_NUM_BASE, "012x")
    return str(number)


def meshcore_player_key(public_key) -> str:
    """The stored key for a MeshCore key, or '' if it is not a MeshCore key."""
    prefix = meshcore_prefix(public_key)
    return MESHCORE_PLAYER_PREFIX + prefix if prefix else ""


def legacy_meshcore_number(public_key) -> str:
    """The pre-migration player id for a MeshCore key: int(key[:8], 16).

    Derived from the key rather than read from mesh_clients.node_num, because
    that column is rewritten with the new numbers as soon as the new code runs.
    """
    prefix = meshcore_prefix(public_key)
    return str(int(prefix[:8], 16)) if prefix else ""
