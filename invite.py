"""Invite bundles: hand someone a file, and their node joins your mesh.

Joining a BBS fleet by hand means copying a broker host, port, TLS
material, credentials, a topic prefix, a peer list and a signing key into
config.ini without a typo. That is the step where someone gives up. An
invite is all of it in one encrypted file.

Three decisions shape this module, and each is a refusal to make something
convenient at the cost of something that matters.

**The file is encrypted, because it carries a broker password.** These
files travel through Discord and email, where a plain-text password is a
password you have published. The passphrase goes by another route.

**Importing never overwrites.** A new [mqttN] section is allocated and
existing links are left exactly as they are. The person importing is
usually the less experienced end of the exchange, and an import that can
break a working node is worse than one that occasionally does nothing.

**The signing key is carried but never armed.** Whoever holds the private
half of a trusted key can make a node run any commit they choose. So the
bundle carries the public half and `apply_invite` will not enrol without
`arm_updates=True`, which the web layer only sets from an explicit tick.
Everything else applies on one click; this one thing is chosen.
"""

import base64
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

FORMAT = "baconbbs-invite"
VERSION = 1

# OWASP's floor for PBKDF2-HMAC-SHA256 at time of writing. The cost is paid
# once per import, by a human who is waiting for a page to load anyway.
KDF_ITERATIONS = 600_000
SALT_BYTES = 16

# A bundle is a few KB of PEM at most. This is here so a hostile or
# mistaken upload cannot make the node allocate hundreds of megabytes.
MAX_BUNDLE_BYTES = 512 * 1024

# Copied from the exporting link. NOT local_id or client_id: those name the
# node ON the link, and two nodes sharing one would collide on the broker,
# each seeing the other's traffic as its own. The importer supplies its own.
LINK_FIELDS = (
    "host", "port", "tls", "tls_insecure",
    "username", "password", "topic_prefix", "keepalive",
)

CERT_FIELDS = ("tls_ca_certs", "tls_certfile", "tls_keyfile")

_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class InviteError(Exception):
    """Anything wrong with a bundle, phrased for the person importing it."""


def _crypto():
    """Import the crypto pieces, or explain their absence in one sentence.

    `cryptography` is already a dependency for fleet signing, but a node can
    be missing it -- that is one of the three live theories for why
    Chattanooga never enrols. Failing here with a readable message beats a
    traceback on the settings page.
    """
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        return Fernet, InvalidToken, hashes, PBKDF2HMAC
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise InviteError(
            "This node is missing the 'cryptography' package, so invite files "
            "cannot be encrypted or opened. Install it and try again.") from exc


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    Fernet, _InvalidToken, hashes, PBKDF2HMAC = _crypto()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def build_payload(link: dict, *, inviter_local_id: str = "",
                  created_by: str = "", bbs_name: str = "",
                  certs: Optional[dict] = None,
                  sync_nodes=(), allowed_nodes=(),
                  fleet: Optional[dict] = None) -> dict:
    """Assemble what an invite carries, from one configured link.

    inviter_local_id is the exporting node's OWN name on this link. It is
    not copied into the importer's config -- it is what lets the importer
    work out the inviter's node id and sync with it.
    """
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "created_by": str(created_by or ""),
        "inviter_local_id": str(inviter_local_id or ""),
        "bbs_name": str(bbs_name or ""),
        "link": {field: link.get(field, "") for field in LINK_FIELDS},
        "certs": {role: text for role, text in (certs or {}).items()
                  if role in CERT_FIELDS and str(text or "").strip()},
        "sync": {
            "bbs_nodes": [str(n).strip() for n in sync_nodes if str(n).strip()],
            "allowed_nodes": [str(n).strip() for n in allowed_nodes if str(n).strip()],
        },
    }
    if fleet:
        payload["fleet"] = {
            "group": str(fleet.get("group", "")).strip(),
            "updates": str(fleet.get("updates", "auto")).strip() or "auto",
            # Public halves only. A private key here would hand the whole
            # fleet to anyone the file reaches; export must never read one.
            "trusted_keys": [str(k).strip() for k in fleet.get("trusted_keys", [])
                             if str(k).strip()],
        }
    return payload


def encrypt_payload(payload: dict, passphrase: str) -> bytes:
    """Wrap a payload in a readable envelope around an encrypted body.

    The envelope stays plain so a wrong file, or a bundle from a future
    version, is told apart from a wrong passphrase. Only the body -- which
    holds the password and any private key material -- is encrypted.
    """
    if not str(passphrase or "").strip():
        raise InviteError("Choose a passphrase. The file carries a broker password.")
    Fernet, _InvalidToken, _hashes, _PBKDF2HMAC = _crypto()
    salt = os.urandom(SALT_BYTES)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    envelope = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": {
            "name": "pbkdf2-sha256",
            "iterations": KDF_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "body": token.decode("ascii"),
    }
    return json.dumps(envelope, indent=2).encode("utf-8")


def decrypt_payload(blob: bytes, passphrase: str) -> dict:
    """Open a bundle, or say which of the several things went wrong."""
    if not blob:
        raise InviteError("That file is empty.")
    if len(blob) > MAX_BUNDLE_BYTES:
        raise InviteError("That file is too large to be an invite.")
    try:
        envelope = json.loads(blob.decode("utf-8"))
    except Exception:
        raise InviteError("That does not look like an invite file.")
    if not isinstance(envelope, dict) or envelope.get("format") != FORMAT:
        raise InviteError("That does not look like an invite file.")
    if int(envelope.get("version", 0) or 0) > VERSION:
        raise InviteError(
            "This invite was made by a newer version of Bacon BBS. "
            "Update this node and try again.")

    kdf = envelope.get("kdf") or {}
    try:
        salt = base64.b64decode(str(kdf.get("salt", "")), validate=True)
        iterations = int(kdf.get("iterations", KDF_ITERATIONS))
    except Exception:
        raise InviteError("This invite file is damaged.")
    if not salt or iterations < 1:
        raise InviteError("This invite file is damaged.")

    Fernet, InvalidToken, hashes, PBKDF2HMAC = _crypto()
    kdf_obj = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=iterations)
    key = base64.urlsafe_b64encode(kdf_obj.derive(str(passphrase or "").encode("utf-8")))
    try:
        raw = Fernet(key).decrypt(str(envelope.get("body", "")).encode("ascii"))
    except InvalidToken:
        raise InviteError("Wrong passphrase, or the file has been altered.")
    except Exception:
        raise InviteError("This invite file is damaged.")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise InviteError("This invite file is damaged.")
    if not isinstance(payload, dict) or not isinstance(payload.get("link"), dict):
        raise InviteError("This invite file is damaged.")
    if not str(payload["link"].get("host", "")).strip():
        raise InviteError("This invite has no broker address in it.")
    if not str(payload["link"].get("topic_prefix", "")).strip():
        raise InviteError("This invite has no topic prefix in it.")
    return payload


def validate_local_id(value: str) -> str:
    """This node's own name on the link, checked before it reaches config.

    It becomes part of a node id (mqtt:<topic>:<local_id>) and a config
    value, so it is kept to characters that cannot break either.
    """
    text = str(value or "").strip()
    if not text:
        raise InviteError("Give this node a name to use on the link.")
    if not _LOCAL_ID_RE.match(text):
        raise InviteError(
            "That name can only contain letters, numbers, dot, dash and "
            "underscore.")
    return text


def peer_ids_for_importer(payload: dict) -> list:
    """Who the importing node should sync with over this link.

    The inviter's own id plus everyone it already syncs with, so the new
    node joins a full mesh rather than a spoke that only talks to whoever
    invited it.
    """
    topic = str(payload.get("link", {}).get("topic_prefix", "")).strip()
    inviter = str(payload.get("inviter_local_id", "")).strip()
    peers = []
    if topic and inviter:
        peers.append(f"mqtt:{topic}:{inviter}")
    for node in (payload.get("sync") or {}).get("bbs_nodes", []):
        if node not in peers:
            peers.append(node)
    return peers


def next_free_index(existing_indexes) -> int:
    """The first unused [mqttN]. Add-only: never reuse a live slot."""
    taken = {int(i) for i in existing_indexes}
    index = 1
    while index in taken:
        index += 1
    return index


def find_matching_link(payload: dict, links) -> Optional[int]:
    """An existing link to the same broker AND topic, if there is one.

    Both, because one broker commonly carries several unrelated fleets on
    different topic prefixes -- example_config.ini says so explicitly. Host
    alone would refuse a legitimate second invite from the same broker.
    """
    link = payload.get("link", {})
    host = str(link.get("host", "")).strip().casefold()
    port = str(link.get("port", "")).strip()
    topic = str(link.get("topic_prefix", "")).strip()
    for existing in links or []:
        if (str(existing.get("host", "")).strip().casefold() == host
                and str(existing.get("port", "")).strip() == port
                and str(existing.get("topic_prefix", "")).strip() == topic):
            return int(existing.get("index"))
    return None


def summarize(payload: dict, *, links=(), current_group: str = "",
              trusted_keys=()) -> dict:
    """Everything the review screen needs to say what will happen.

    Built here rather than in the template so the summary is testable and
    so it cannot disagree with what apply_invite actually does.
    """
    link = payload.get("link", {})
    fleet = payload.get("fleet") or {}
    group = str(fleet.get("group", "")).strip()
    incoming = [k for k in fleet.get("trusted_keys", []) if str(k).strip()]
    known = {str(k).split(":", 1)[0] for k in trusted_keys}
    new_keys = [k for k in incoming if str(k).split(":", 1)[0] not in known]

    group_conflict = bool(group and current_group and group != current_group)
    return {
        "bbs_name": str(payload.get("bbs_name", "")).strip(),
        "created_by": str(payload.get("created_by", "")).strip(),
        "created_at": str(payload.get("created_at", "")).strip(),
        "host": str(link.get("host", "")).strip(),
        "port": str(link.get("port", "")).strip(),
        "tls": bool(link.get("tls")),
        "topic_prefix": str(link.get("topic_prefix", "")).strip(),
        "has_credentials": bool(str(link.get("username", "")).strip()
                                or str(link.get("password", "")).strip()),
        "certs": sorted((payload.get("certs") or {}).keys()),
        "shares_private_key": "tls_keyfile" in (payload.get("certs") or {}),
        "peers": peer_ids_for_importer(payload),
        "allowed_nodes": (payload.get("sync") or {}).get("allowed_nodes", []),
        "already_have": find_matching_link(payload, links),
        "fleet_group": group,
        "fleet_updates": str(fleet.get("updates", "")).strip(),
        "new_keys": new_keys,
        "known_keys": [k for k in incoming if k not in new_keys],
        # An invite for a different group cannot be armed without also
        # changing this node's group, which would silently move it to
        # someone else's fleet. Offered as a refusal, not a choice.
        "group_conflict": group_conflict,
        "current_group": str(current_group or ""),
        "can_arm_updates": bool(incoming and group and not group_conflict),
    }


def apply_invite(config, payload: dict, *, local_id: str, index: int,
                 cert_writer=None, arm_updates: bool = False,
                 summary: Optional[dict] = None) -> dict:
    """Write an invite into a ConfigParser. Returns what it changed.

    Add-only by construction: it writes [mqtt<index>] and its two companion
    sections and touches nothing else, except [fleet] -- and that only when
    `arm_updates` is true.

    The caller supplies `index` from next_free_index and `cert_writer` from
    the web layer, which keeps this function free of both Flask and the
    filesystem so it can be tested against a bare ConfigParser.
    """
    clean_local_id = validate_local_id(local_id)
    link = payload.get("link", {})
    section = f"mqtt{int(index)}"
    sync_section = f"sync_mqtt{int(index)}"
    allow_section = f"allow_list_mqtt{int(index)}"
    changed = {"section": section, "certs": [], "armed_updates": False,
               "keys_added": []}

    for name in (section, sync_section, allow_section):
        if not config.has_section(name):
            config.add_section(name)

    config.set(section, "enabled", "true")
    for field in LINK_FIELDS:
        value = link.get(field, "")
        if isinstance(value, bool):
            value = "true" if value else "false"
        config.set(section, field, str(value or ""))
    config.set(section, "local_id", clean_local_id)

    for role, text in (payload.get("certs") or {}).items():
        if role not in CERT_FIELDS or not cert_writer:
            continue
        path = cert_writer(int(index), role, text)
        config.set(section, role, str(path))
        changed["certs"].append(role)

    config.set(sync_section, "bbs_nodes", ",".join(peer_ids_for_importer(payload)))
    allowed = (payload.get("sync") or {}).get("allowed_nodes", [])
    config.set(allow_section, "allowed_nodes", ",".join(allowed))

    if arm_updates:
        info = summary if summary is not None else summarize(payload)
        if not info.get("can_arm_updates"):
            # Refused rather than silently skipped: the operator ticked a box
            # and is owed an answer about why it did not happen.
            raise InviteError(
                "This invite cannot arm updates on this node -- see the group "
                "note on the review page.")
        fleet = payload.get("fleet") or {}
        if not config.has_section("fleet"):
            config.add_section("fleet")
        existing_raw = config.get("fleet", "trusted_keys", fallback="")
        existing = [k.strip() for k in existing_raw.split(",") if k.strip()]
        known = {k.split(":", 1)[0] for k in existing}
        # Add, never replace. Wholesale rewriting of this list is the leading
        # theory for the day both live nodes stopped trusting a signing key.
        for entry in fleet.get("trusted_keys", []):
            if str(entry).split(":", 1)[0] not in known:
                existing.append(str(entry).strip())
                changed["keys_added"].append(str(entry).split(":", 1)[0])
        config.set("fleet", "trusted_keys", ",".join(existing))
        config.set("fleet", "group", str(fleet.get("group", "")).strip())
        config.set("fleet", "updates", str(fleet.get("updates", "auto")).strip() or "auto")
        changed["armed_updates"] = True

    return changed


def new_token() -> str:
    return secrets.token_urlsafe(24)
