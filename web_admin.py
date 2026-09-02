import os
import json
import logging
import re
import sqlite3
import ssl
import time
import uuid
import secrets
import configparser
from datetime import datetime, timezone as dt_timezone
from functools import wraps
from typing import Optional
from app_paths import resolve_app_path

from flask import Flask, flash, jsonify, redirect, render_template, render_template_string, request, send_from_directory, session, url_for

from db_operations import (
    install_connection_log_handler,
    initialize_database,
  get_public_chatter_filters,
  get_public_chatter_history,
    get_peer_sync_states,
)
from utils import get_sync_runtime_settings
from version_info import get_display_version, get_version_resolution_note


TABLE_CONFIG = {
    "bulletins": {
        "title": "Bulletins",
    "columns": ["id", "board", "sender_short_name", "date", "subject", "content", "local_only", "unique_id", "source_node_id", "source_timestamp", "received_at"],
    "editable": ["board", "sender_short_name", "date", "subject", "content", "local_only"],
    "searchable": ["board", "sender_short_name", "subject", "content", "unique_id", "local_only"],
    },
    "mail": {
        "title": "Mail",
        "columns": ["id", "sender", "sender_short_name", "recipient", "date", "subject", "content", "unique_id", "source_node_id", "source_timestamp", "received_at"],
        "editable": ["sender", "sender_short_name", "recipient", "date", "subject", "content"],
        "searchable": ["sender", "sender_short_name", "recipient", "subject", "content", "unique_id"],
    },
    "channels": {
        "title": "Channels",
        "columns": ["id", "name", "url", "local_only"],
        "column_labels": {"url": "Description"},
        "editable": ["name", "url", "local_only"],
        "searchable": ["name", "url", "local_only"],
    },
}


TABLE_LIST_CONTENT = """
<div class=\"card\">
  <h2>{{ table_title }}</h2>
  <form class=\"search-bar\" method=\"get\">
    <input type=\"text\" name=\"q\" value=\"{{ search_query }}\" placeholder=\"Search {{ table_title }}\">
    <button class=\"btn\" type=\"submit\">Search</button>
    {% if search_query %}
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Clear</a>
    {% endif %}
    {% if create_url %}
    <a class=\"btn btn-primary\" href=\"{{ create_url }}\">{{ create_label }}</a>
    {% endif %}
  </form>
  <table data-draggable=\"true\" data-table-name=\"{{ table_name }}\">
    <thead>
      <tr>
        <th></th>
        {% for column in display_columns %}
        <th>{{ column }}</th>
        {% endfor %}
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr data-row-id=\"{{ row['id'] }}\">
        <td class=\"reorder-handle\">⋮⋮</td>
        {% for column in display_columns %}
        <td>
          {% if column == 'sync_status' %}
            {% if row.get('_sync_incomplete') %}
            <strong style=\"color:#b91c1c;\">Incomplete</strong><br>
            <span class=\"muted\">{{ row.get('_sync_status_text', '') }}</span>
            {% else %}
            <span class=\"muted\">{{ row.get('_sync_status_text', 'OK') }}</span>
            {% endif %}
          {% else %}
            {{ row.get(column, '') }}
          {% endif %}
        </td>
        {% endfor %}
        <td>
          <div class=\"row-actions\">
            <a class=\"btn btn-small\" href=\"{{ url_for('table_edit', table=table_name, row_id=row['id']) }}\">{{ edit_label }}</a>
            {% if comments_enabled %}
            <a class=\"btn btn-small\" href=\"{{ url_for('channel_comments', channel_id=row['id']) }}\">Comments</a>
            {% endif %}
            {% if row.get('_resolve_scope') and row.get('_resolve_key') %}
            <form method=\"post\" action=\"{{ url_for('resolve_record') }}\" class=\"inline\">
              <input type=\"hidden\" name=\"scope\" value=\"{{ row['_resolve_scope'] }}\">
              <input type=\"hidden\" name=\"key\" value=\"{{ row['_resolve_key'] }}\">
              <input type=\"hidden\" name=\"redirect_to\" value=\"{{ request.full_path if request.query_string else request.path }}\">
              <button type=\"submit\" class=\"btn btn-small\">Resolve</button>
            </form>
            {% endif %}
            <form method=\"post\" action=\"{{ url_for('table_delete', table=table_name, row_id=row['id']) }}\" class=\"inline\" onsubmit=\"return confirm('Delete this row?');\">
              <button type=\"submit\" class=\"btn btn-danger btn-small\">Delete</button>
            </form>
          </div>
        </td>
      </tr>
      {% endfor %}
      {% if not rows %}
      <tr>
        <td colspan=\"{{ display_columns|length + 2 }}\" class=\"muted\">No rows found.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>
</div>
"""



def read_config_file(config_path: str) -> configparser.ConfigParser:
  config = configparser.ConfigParser()
  config.read(config_path)
  return config


def write_config_file(config: configparser.ConfigParser, config_path: str) -> None:
  with open(config_path, "w", encoding="utf-8") as config_file:
    config.write(config_file)


def parse_list_input(raw_value: str) -> list[str]:
  normalized = raw_value.replace("\r", "\n").replace("\n", ",")
  values = []
  seen = set()
  for item in normalized.split(","):
    value = item.strip()
    if value and value not in seen:
      seen.add(value)
      values.append(value)
  return values


def load_bulletin_boards(config_path: str) -> list[str]:
  env_value = os.getenv("BBS_BULLETIN_BOARDS", "").strip()
  if env_value:
    boards = parse_list_input(env_value)
    if boards:
      return boards

  config = read_config_file(config_path)
  config_value = config.get("boards", "bulletin_boards", fallback="").strip()
  if config_value:
    boards = parse_list_input(config_value)
    if boards:
      return boards

  return ["General", "Info", "News", "Urgent"]


def load_admin_credentials(config_path: str) -> tuple[str, str, bool, bool]:
  config = read_config_file(config_path)
  env_user = os.getenv("BBS_WEBGUI_USER", "").strip()
  env_password = os.getenv("BBS_WEBGUI_PASSWORD", "").strip()

  username = env_user or config.get("admin", "username", fallback="admin").strip() or "admin"
  password = env_password or config.get("admin", "password", fallback="change-me")

  return username, password, bool(env_user), bool(env_password)

def load_sync_settings(config_path: str) -> tuple[list[str], list[str], int, bool]:
  config = read_config_file(config_path)
  bbs_nodes = parse_list_input(config.get("sync", "bbs_nodes", fallback=""))
  allowed_nodes = parse_list_input(config.get("allow_list", "allowed_nodes", fallback=""))
  interval_raw = config.get("sync", "sync_interval_minutes", fallback="5").strip()
  try:
    sync_interval_minutes = int(interval_raw)
  except ValueError:
    sync_interval_minutes = 5
  sync_interval_minutes = max(1, sync_interval_minutes)
  sync_zork_saves = _parse_bool_setting(config.get("sync", "sync_zork_saves", fallback="true"), True)
  return bbs_nodes, allowed_nodes, sync_interval_minutes, sync_zork_saves


def load_storage_settings(config_path: str) -> dict:
  """Read the [maintenance] storage cap for the web-admin form."""
  config = read_config_file(config_path)
  return {
    "max_db_size_mb": config.get("maintenance", "max_db_size_mb", fallback="0").strip() or "0",
  }


def load_fleet_settings(config_path: str) -> dict:
  """Read [fleet] the same way the server does, so the page cannot disagree
  with the process about whether updates are armed."""
  config = read_config_file(config_path)
  try:
    # fleet_update, NOT config_init: importing config_init here pulls in
    # meshtastic and pyserial, and where that fails the fallback below reads
    # as "updates are off" when they are actually armed.
    from fleet_update import read_fleet_settings
    return read_fleet_settings(config)
  except Exception:
    return {"group": "", "trusted_keys": "", "updates": "off",
            "pin_commit": "", "key_count": 0,
            "error": "fleet update support unavailable (cryptography missing)",
            "notes": []}


def request_fleet_apply_trigger() -> None:
  """Ask server.py to act on the stored target, via the existing channel."""
  trigger_file = resolve_app_path(
    os.getenv("BBS_FLEET_APPLY_TRIGGER_PATH"), "apply_update.trigger")
  tmp = f"{trigger_file}.tmp"
  with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(datetime.utcnow().isoformat())
  os.replace(tmp, trigger_file)


def build_fleet_view(config_path: str) -> dict:
  """Everything the Fleet page shows, including why it might be inert."""
  settings = load_fleet_settings(config_path)
  group = settings.get("group", "")

  local_version, local_commit = "", ""
  try:
    from version_info import get_app_version, get_git_commit_short
    local_version, local_commit = get_app_version(), get_git_commit_short()
  except Exception:
    pass

  key_ids = []
  try:
    import fleet_update
    key_ids = sorted(fleet_update.parse_trusted_keys(
      settings.get("trusted_keys", "")))
  except Exception:
    pass

  target, peers = None, []
  try:
    from db_operations import get_fleet_target, get_node_versions
    target = get_fleet_target(group) if group else None
    peers = get_node_versions()
  except Exception:
    pass

  on_target = bool(
    target and local_commit
    and str(target.get("commit", "")).startswith(local_commit[:7]))

  return {
    "local_version": local_version or "unknown",
    "local_commit": local_commit,
    "group": group,
    "mode": settings.get("updates", "off"),
    "pin_commit": settings.get("pin_commit", ""),
    "key_count": len(key_ids),
    "key_ids": key_ids,
    "config_error": settings.get("error", ""),
    "target": target,
    "on_target": on_target,
    "peers": peers,
  }


def load_public_chatter_settings(config_path: str) -> dict:
  config = read_config_file(config_path)
  return {
    "enabled": config.getboolean("public_chatter", "enabled", fallback=False),
    "primary_channels": config.get("public_chatter", "primary_channels", fallback="").strip(),
    "secondary_channels": config.get("public_chatter", "secondary_channels", fallback="").strip(),
  }


# Every place a sync peer can be configured. Peers are spread across the
# primary radio, the optional second radio, and one section per MQTT link,
# so there has never been a single view of "who does this node sync with" --
# which is how a decommissioned node stays configured for months.
_PEER_SECTION_LABELS = {
  "sync": "Primary radio",
  "sync2": "Secondary radio",
}


def _peer_section_label(section: str) -> str:
  if section in _PEER_SECTION_LABELS:
    return _PEER_SECTION_LABELS[section]
  match = re.match(r"^sync_mqtt(\d+)$", section)
  if match:
    return f"MQTT broker #{match.group(1)}"
  return section


def load_sync_peers(config_path: str) -> list[dict]:
  """Every configured sync peer, plus peers only the database still knows.

  The second group matters as much as the first: removing a peer from
  config stops the traffic but leaves its peer_sync_state row behind, so it
  keeps appearing in Diagnostics as a peer that is permanently behind.
  Listing both is what makes "clean this up" a single job.
  """
  config = read_config_file(config_path)
  peers: list[dict] = []
  configured: set = set()

  for section in config.sections():
    if section != "sync" and section != "sync2" and not re.match(r"^sync_mqtt\d+$", section):
      continue
    for node_id in parse_list_input(config.get(section, "bbs_nodes", fallback="")):
      configured.add(node_id)
      entry = {
        "node_id": node_id,
        "section": section,
        "section_label": _peer_section_label(section),
        "configured": True,
        "problem": "",
      }
      # An MQTT peer address embeds the topic it lives on. If that does not
      # match its own link's prefix the two are on different topics and can
      # never reach each other -- silently, which is the whole problem.
      mqtt_match = re.match(r"^sync_mqtt(\d+)$", section)
      if mqtt_match and node_id.startswith("mqtt:"):
        link_prefix = config.get(f"mqtt{mqtt_match.group(1)}", "topic_prefix", fallback="").strip()
        peer_prefix = node_id.split(":", 2)[1] if node_id.count(":") >= 2 else ""
        if link_prefix and peer_prefix and peer_prefix != link_prefix:
          entry["problem"] = (
            f"on topic '{peer_prefix}' but this link uses '{link_prefix}' — unreachable")
      elif mqtt_match and not node_id.startswith("mqtt:"):
        entry["problem"] = "not a valid MQTT peer address (expected mqtt:<topic>:<name>)"
      peers.append(entry)

  try:
    for row in get_peer_sync_states():
      node_id = str(row[0])
      if node_id in configured:
        continue
      peers.append({
        "node_id": node_id,
        "section": "",
        "section_label": "Not configured anywhere",
        "configured": False,
        "problem": "tracked in the database but no longer a configured peer",
      })
  except Exception:
    pass  # DB unavailable; the configured list is still worth showing.

  return peers


def load_peer_link_targets(config_path: str) -> list[dict]:
  """Sections a peer can be added to, with the address shape each expects.

  Built from what is actually configured -- offering "Secondary radio" on a
  single-radio node, or a broker that does not exist, would only invite an
  entry that can never be reached.
  """
  config = read_config_file(config_path)
  targets = [{"section": "sync", "label": "Primary radio", "hint": "e.g. !a1b2c3d4"}]
  if config.has_section("interface2") and config.get("interface2", "type", fallback="").strip():
    targets.append({"section": "sync2", "label": "Secondary radio", "hint": "e.g. !a1b2c3d4"})
  for section in config.sections():
    match = _MQTT_SECTION_RE.match(section)
    if not match:
      continue
    index = match.group(1)
    prefix = config.get(section, "topic_prefix", fallback="").strip()
    targets.append({
      "section": f"sync_mqtt{index}",
      "label": f"MQTT broker #{index}" + (f" ({prefix})" if prefix else ""),
      "hint": f"mqtt:{prefix}:<name>" if prefix else "mqtt:<topic>:<name>",
    })
  return targets


def load_subscriber_settings(config_path: str) -> dict:
  """Read the [sync] subscriber_nodes (pull-only nodes, e.g. a Pico cache)."""
  config = read_config_file(config_path)
  raw = config.get("sync", "subscriber_nodes", fallback="").strip()
  nodes = [n.strip() for n in raw.split(",") if n.strip()]
  return {"subscriber_nodes_text": "\n".join(nodes)}


def load_gateway_settings(config_path: str) -> dict:
  """Read the [gateway] section for the web-admin form (with defaults)."""
  config = read_config_file(config_path)
  g = lambda opt, d="": config.get("gateway", opt, fallback=d).strip()  # noqa: E731
  return {
    "enabled": _parse_bool_setting(config.get("gateway", "enabled", fallback="false"), False),
    "ai_base_url": g("ai_base_url"),
    "ai_dialect": g("ai_dialect", "ollama") or "ollama",
    "ai_api_key": g("ai_api_key"),
    "ai_model": g("ai_model", "llama3.2"),
    "ai_system_prompt": g("ai_system_prompt"),
    "allowed_hosts": g("allowed_hosts"),
    "allowed_schemes": g("allowed_schemes", "https") or "https",
    "allowed_nodes": g("allowed_nodes"),
    "request_timeout": g("request_timeout", "20") or "20",
    "max_response_bytes": g("max_response_bytes", "800") or "800",
    "rate_limit_per_node": g("rate_limit_per_node", "5") or "5",
  }


# "none" first: it is the only option that needs no hardware, and it is what
# an MQTT-only node -- one that mirrors another BBS's content over a broker
# with no radio of its own -- is supposed to be set to. Without it the only
# way to run radio-less was to name a serial port that does not exist and let
# the node retry it forever.
DEVICE_TYPES = ("none", "serial", "tcp", "meshcore_serial", "meshcore_tcp",
                "meshcore_ble")
DEVICE_TYPE_LABELS = {
  "none": "No radio — MQTT only",
  "serial": "Meshtastic — Serial (USB)",
  "tcp": "Meshtastic — TCP (WiFi)",
  "meshcore_serial": "MeshCore — Serial (USB)",
  "meshcore_tcp": "MeshCore — TCP (WiFi)",
  "meshcore_ble": "MeshCore — Bluetooth (BLE)",
}


def _load_device_section(config: configparser.ConfigParser, section: str) -> dict:
  return {
    "type": config.get(section, "type", fallback="").strip().lower(),
    "port": config.get(section, "port", fallback="").strip(),
    "hostname": config.get(section, "hostname", fallback="").strip(),
    "tcp_port": config.get(section, "tcp_port", fallback="5000").strip() or "5000",
    "baudrate": config.get(section, "baudrate", fallback="115200").strip() or "115200",
    "ble_address": config.get(section, "ble_address", fallback="").strip(),
    "ble_pin": config.get(section, "ble_pin", fallback="").strip(),
    "channel_index": config.get(section, "channel_index", fallback="0").strip() or "0",
  }


def load_device_settings(config_path: str) -> dict:
  """Read [interface] (primary) and [interface2] (optional secondary, dual-
  radio bridge mode) plus [sync2]/[allow_list2] peer lists for the web-admin
  Devices form.

  Device changes require restarting the mesh-bbs service to take effect --
  the running radio interface is opened once at startup from these same
  config keys (config_init.get_interface/get_secondary_interface), so unlike
  sync/gateway settings there is no live-apply path here.
  """
  config = read_config_file(config_path)
  primary = _load_device_section(config, "interface")
  if primary["type"] not in DEVICE_TYPES:
    primary["type"] = "serial"

  secondary = _load_device_section(config, "interface2")
  # "none" is meaningful only for the primary -- it is how a node says it has
  # no radio. A second radio that is "none" is simply not a second radio.
  secondary_configured = (
    bool(secondary["type"]) and secondary["type"] in DEVICE_TYPES
    and secondary["type"] != "none")
  secondary_enabled = secondary_configured and _parse_bool_setting(
    config.get("interface2", "enabled", fallback="true"), True
  )
  if not secondary_configured:
    secondary["type"] = ""

  bbs_nodes2 = parse_list_input(config.get("sync2", "bbs_nodes", fallback=""))
  allowed_nodes2 = parse_list_input(config.get("allow_list2", "allowed_nodes", fallback=""))

  return {
    "types": DEVICE_TYPES,
    "secondary_types": tuple(t for t in DEVICE_TYPES if t != "none"),
    "type_labels": DEVICE_TYPE_LABELS,
    "primary": primary,
    "secondary": secondary,
    "secondary_enabled": secondary_enabled,
    "bbs_nodes2_text": "\n".join(bbs_nodes2),
    "allowed_nodes2_text": "\n".join(allowed_nodes2),
  }


def load_account_settings(config_path: str) -> dict:
  """Read [accounts] tunables for the web-admin Settings form. Defaults
  match command_handlers.py's own defaults exactly -- these are the same
  numbers used at runtime via utils._config_int, this is purely the
  editable-in-the-GUI view of them."""
  config = read_config_file(config_path)
  return {
    "link_code_ttl_minutes": config.get("accounts", "link_code_ttl_minutes", fallback="10").strip() or "10",
    "link_requests_per_hour": config.get("accounts", "link_requests_per_hour", fallback="3").strip() or "3",
    "link_attempts_per_hour": config.get("accounts", "link_attempts_per_hour", fallback="5").strip() or "5",
    "max_linked_devices": config.get("accounts", "max_linked_devices", fallback="6").strip() or "6",
    "link_code_delay_minutes": config.get(
      "accounts", "link_code_delay_minutes", fallback="2").strip() or "2",
  }


_MQTT_SECTION_RE = re.compile(r"^mqtt(\d+)$")

# Uploaded TLS material for MQTT links. Lets an operator point a broker at a
# private CA (or set up mutual TLS) entirely from the browser instead of
# needing shell access to scp files onto the node.
MQTT_CERT_MAX_BYTES = 256 * 1024  # a PEM chain is a few KB; this only blocks abuse
_PEM_CERT_MARKER = "-----BEGIN CERTIFICATE-----"
_PEM_KEY_MARKERS = (
  "-----BEGIN PRIVATE KEY-----",
  "-----BEGIN RSA PRIVATE KEY-----",
  "-----BEGIN EC PRIVATE KEY-----",
  "-----BEGIN DSA PRIVATE KEY-----",
  "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)
# role -> (stored filename, is_secret)
_MQTT_CERT_ROLES = {
  "tls_ca_certs": ("ca.pem", False),
  "tls_certfile": ("client-cert.pem", False),
  "tls_keyfile": ("client-key.pem", True),
}


def get_mqtt_cert_dir() -> str:
  """Directory uploaded MQTT TLS material is stored under.

  Kept out of the repo tree's tracked files (see .gitignore) so a private
  key can never ride along in a commit -- these nodes are updated by
  `git pull`, which makes an accidentally-tracked key a real hazard.
  """
  return resolve_app_path(os.getenv("BBS_MQTT_CERT_DIR"), os.path.join("data", "mqtt-certs"))


def validate_pem_upload(raw: bytes, expect: str, label: str) -> tuple[Optional[str], Optional[str]]:
  """Validate an uploaded PEM file. Returns (text, error_message).

  Stdlib only -- deliberately no new dependency. This catches the mistake
  that actually happens: uploading the *wrong file*. Without it, a swapped
  cert/key or a DER-encoded file surfaces much later as an opaque SSL
  error at connect time, which reads as "the broker is down".
  """
  if not raw:
    return None, f"{label}: the uploaded file is empty."
  if len(raw) > MQTT_CERT_MAX_BYTES:
    return None, (
      f"{label}: file is too large ({len(raw)} bytes). A PEM certificate or key "
      f"is only a few KB -- is this the right file?"
    )
  try:
    text = raw.decode("utf-8")
  except UnicodeDecodeError:
    return None, (
      f"{label}: this looks like a binary (DER/PKCS#12) file, not PEM. Convert it "
      f"first, e.g. `openssl x509 -inform der -in cert.der -out cert.pem`."
    )

  has_cert = _PEM_CERT_MARKER in text
  has_key = any(marker in text for marker in _PEM_KEY_MARKERS)

  if expect == "cert":
    if not has_cert:
      if has_key:
        return None, (
          f"{label}: this file contains a PRIVATE KEY, not a certificate. "
          f"Upload it in the client private key field instead."
        )
      return None, f"{label}: no PEM certificate found (expected a {_PEM_CERT_MARKER} block)."
    # Structural check on the first block -- catches truncated/corrupt base64
    # that has the right headers but isn't a usable certificate.
    try:
      ssl.PEM_cert_to_DER_cert(text[text.index(_PEM_CERT_MARKER):])
    except Exception:
      return None, f"{label}: the PEM certificate could not be parsed (truncated or corrupt?)."
  elif expect == "key":
    if not has_key:
      if has_cert:
        return None, (
          f"{label}: this file contains a CERTIFICATE, not a private key. "
          f"Upload it in a certificate field instead."
        )
      return None, f"{label}: no PEM private key found in this file."

  return text, None


def store_mqtt_cert(index: int, role: str, text: str) -> str:
  """Write validated PEM material for one broker and return its path.

  The uploaded filename is deliberately ignored in favor of a fixed name
  per (broker, role) -- there is no way for a crafted filename to escape
  the directory, and re-uploading cleanly replaces the previous file.
  """
  filename, is_secret = _MQTT_CERT_ROLES[role]
  directory = os.path.join(get_mqtt_cert_dir(), f"mqtt{index}")
  os.makedirs(directory, exist_ok=True)
  try:
    os.chmod(directory, 0o700)
  except OSError:
    pass  # non-POSIX filesystem; content-level perms below still apply where supported
  path = os.path.join(directory, filename)
  with open(path, "w", encoding="utf-8", newline="\n") as handle:
    handle.write(text)
  try:
    os.chmod(path, 0o600 if is_secret else 0o644)
  except OSError:
    pass
  return path


def _peer_reported_age(updated_at) -> str:
  """How long ago a discovered peer last reported, from its own status.

  Status messages are RETAINED, so the broker replays the last one a node
  ever published -- including one from a node that no longer exists under
  that name. A node whose local_id was later changed leaves its old
  identity on the broker forever. Showing the node's own timestamp is what
  makes such a ghost obvious instead of it looking like a live peer.

  Parsed as UTC: the publisher writes an aware UTC timestamp, and
  comparing that against local time would report a node in a
  behind-UTC timezone as being in the future, i.e. always fresh.
  """
  text = str(updated_at or "").strip()
  if not text:
    return "never reported"
  try:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
  except ValueError:
    return "unknown"
  if when.tzinfo is None:
    when = when.replace(tzinfo=dt_timezone.utc)
  seconds = int((datetime.now(dt_timezone.utc) - when).total_seconds())
  if seconds < 0:
    return "just now"
  if seconds < 120:
    return "just now"
  if seconds < 3600:
    return f"{seconds // 60}m ago"
  if seconds < 86400:
    return f"{seconds // 3600}h ago"
  return f"{seconds // 86400}d ago"


def load_mqtt_settings(config_path: str) -> list[dict]:
  """Read every [mqttN] section (N = 1, 2, 3, ... -- open-ended, unlike
  [interface2]'s fixed single slot) for the web-admin MQTT Bridges form.

  Unlike config_init.discover_mqtt_link_names (which is runtime-only and
  skips 'enabled = false' links), this returns EVERY configured link
  regardless of its enabled state, so a disabled broker's connection
  details still show up in the form instead of disappearing.

  MQTT changes require restarting the mesh-bbs service to take effect --
  links are opened once at startup in server.py's main() via
  config_init.get_mqtt_interfaces(), so unlike sync/gateway settings there
  is no live-apply path here (same restart caveat as Device Configuration)."""
  config = read_config_file(config_path)
  found = []
  for section_name in config.sections():
    match = _MQTT_SECTION_RE.match(section_name)
    if match:
      found.append((int(match.group(1)), section_name))
  found.sort(key=lambda item: item[0])

  sync_section = lambda n: f"sync_mqtt{n}"  # noqa: E731
  allow_section = lambda n: f"allow_list_mqtt{n}"  # noqa: E731

  links = []
  for index, section_name in found:
    links.append({
      "index": index,
      "enabled": _parse_bool_setting(config.get(section_name, "enabled", fallback="true"), True),
      "host": config.get(section_name, "host", fallback="").strip(),
      "port": config.get(section_name, "port", fallback="1883").strip() or "1883",
      "tls": _parse_bool_setting(config.get(section_name, "tls", fallback="false"), False),
      "tls_ca_certs": config.get(section_name, "tls_ca_certs", fallback="").strip(),
      "tls_certfile": config.get(section_name, "tls_certfile", fallback="").strip(),
      "tls_keyfile": config.get(section_name, "tls_keyfile", fallback="").strip(),
      "tls_keyfile_password": config.get(section_name, "tls_keyfile_password", fallback="").strip(),
      "tls_insecure": _parse_bool_setting(config.get(section_name, "tls_insecure", fallback="false"), False),
      "username": config.get(section_name, "username", fallback="").strip(),
      "password": config.get(section_name, "password", fallback="").strip(),
      "topic_prefix": config.get(section_name, "topic_prefix", fallback="").strip(),
      "local_id": config.get(section_name, "local_id", fallback="").strip(),
      "client_id": config.get(section_name, "client_id", fallback="").strip(),
      "keepalive": config.get(section_name, "keepalive", fallback="60").strip() or "60",
      # publish_status defaults true (pre-existing behavior); the rest
      # default false so adding these options changes no existing traffic.
      "publish_status": _parse_bool_setting(
        config.get(section_name, "publish_status", fallback="true"), True),
      "publish_clients": _parse_bool_setting(
        config.get(section_name, "publish_clients", fallback="false"), False),
      "publish_telemetry": _parse_bool_setting(
        config.get(section_name, "publish_telemetry", fallback="false"), False),
      "publish_activity": _parse_bool_setting(
        config.get(section_name, "publish_activity", fallback="false"), False),
      "publish_sync_stats": _parse_bool_setting(
        config.get(section_name, "publish_sync_stats", fallback="false"), False),
      "publish_prefix": config.get(section_name, "publish_prefix", fallback="").strip(),
      "publish_clients_max_age_hours": config.get(
        section_name, "publish_clients_max_age_hours", fallback="24").strip() or "24",
      "bbs_nodes_text": "\n".join(parse_list_input(config.get(sync_section(index), "bbs_nodes", fallback=""))),
      "allowed_nodes_text": "\n".join(parse_list_input(config.get(allow_section(index), "allowed_nodes", fallback=""))),
    })
  return links


def _parse_bool_setting(raw_value: Optional[str], default: bool = False) -> bool:
  if raw_value is None:
    return default
  normalized = str(raw_value).strip().lower()
  if normalized == "":
    return default
  return normalized in {"1", "true", "yes", "on"}


def _parse_float_setting(raw_value: str, default: float) -> float:
  try:
    return max(0.0, float(str(raw_value or "").strip()))
  except ValueError:
    return default


def _parse_int_setting(raw_value: str, default: int, minimum: int = 0) -> int:
  try:
    return max(minimum, int(str(raw_value or "").strip()))
  except ValueError:
    return default


def load_sync_speed_settings(config_path: str) -> dict:
  config = read_config_file(config_path)
  return {
    "sync_turbo": _parse_bool_setting(config.get("sync", "sync_turbo", fallback="false"), False),
    "sync_pause_seconds": _parse_float_setting(config.get("sync", "sync_pause_seconds", fallback="0.75"), 0.75),
    "hash_repair_pause_seconds": _parse_float_setting(config.get("sync", "hash_repair_pause_seconds", fallback="0.1"), 0.1),
    "full_sync_delay_ms": _parse_int_setting(config.get("sync", "full_sync_delay_ms", fallback="500"), 500, minimum=0),
  }


def get_sync_env_override_flags() -> dict[str, bool]:
  return {
    "sync_turbo": os.getenv("BBS_SYNC_TURBO") is not None,
    "sync_pause_seconds": os.getenv("BBS_SYNC_PAUSE_SECONDS") is not None,
    "hash_repair_pause_seconds": os.getenv("BBS_HASH_REPAIR_PAUSE_SECONDS") is not None,
    "full_sync_delay_ms": os.getenv("BBS_FULL_SYNC_DELAY_MS") is not None,
  }


def get_manual_sync_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_MANUAL_SYNC_TRIGGER_PATH"), "manual_sync.trigger")


def request_manual_sync_trigger() -> None:
  trigger_path = get_manual_sync_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(datetime.utcnow().isoformat())
  os.replace(tmp_path, trigger_path)


def nudge_sync_after_content_change() -> None:
  """Ask the running BBS to sync now, after the web admin changes content.

  The web admin is a SEPARATE PROCESS from mesh-bbs, so
  get_runtime_interface() is None here and every create/edit/delete is
  written with no peers and no interface -- nothing is pushed. The change
  then sits until the next scheduled reconcile, which is why a post made
  in the browser took a full sync interval to appear on a peer while one
  sent over the radio propagated at once.

  The trigger file the Settings page already uses is read by the main loop
  every second, so this turns that wait into about a second. Best-effort:
  if it cannot be written the change still syncs on the normal schedule.
  """
  try:
    request_manual_sync_trigger()
  except Exception:
    logging.debug("could not request a sync after a content change", exc_info=True)


def get_force_check_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_FORCE_CHECK_TRIGGER_PATH"), "force_check.trigger")


def request_force_check_trigger() -> None:
  trigger_path = get_force_check_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(datetime.utcnow().isoformat())
  os.replace(tmp_path, trigger_path)


def get_peer_resync_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_PEER_RESYNC_TRIGGER_PATH"), "resync_peer.trigger")


def request_peer_resync_trigger(peer_node_id: str) -> None:
  """Write a trigger file containing the peer node ID so server.py clears it
  from its in-memory synced_nodes set and runs a fresh full sync for that peer."""
  trigger_path = get_peer_resync_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(str(peer_node_id).strip())
  os.replace(tmp_path, trigger_path)


def get_link_reconnect_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_LINK_RECONNECT_TRIGGER_PATH"), "reconnect_link.trigger")


def request_link_reconnect_trigger(link_name: str) -> None:
  """Ask server.py to drop and re-establish ONE link (or 'all').

  Reconnecting a single link is not a restart: server.py reuses the same
  reconnect path its automatic liveness check uses, so only that link's
  connection is torn down and rebuilt (with backoff). Every other link, the
  sync engine, and the BBS itself keep running untouched.
  """
  trigger_path = get_link_reconnect_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(str(link_name).strip())
  os.replace(tmp_path, trigger_path)


def get_links_reload_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_LINKS_RELOAD_TRIGGER_PATH"), "reload_links.trigger")


def request_links_reload_trigger() -> None:
  """Ask server.py to bring its live links in line with config.ini.

  Opens brokers added since startup, drops removed ones, and reconnects
  ones whose connection settings changed -- all without restarting the
  service. Requested automatically whenever MQTT settings are saved, since
  otherwise `links` stays frozen at whatever main() built at startup and a
  newly-added [mqttN] would not exist at all.
  """
  trigger_path = get_links_reload_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write("reload")
  os.replace(tmp_path, trigger_path)


def get_zork_save_resolve_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_ZORK_SAVE_RESOLVE_TRIGGER_PATH"), "resolve_zork_save.trigger")


def request_zork_save_resolve_trigger(user_id: str, game_id: str) -> None:
  normalized_user = str(user_id).strip()
  normalized_game = str(game_id).strip()
  if not normalized_user or not normalized_game:
    raise ValueError("user_id and game_id required")
  trigger_path = get_zork_save_resolve_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    json.dump({"user_id": normalized_user, "game_id": normalized_game}, trigger_file)
  os.replace(tmp_path, trigger_path)


def get_record_resolve_trigger_path() -> str:
  return resolve_app_path(os.getenv("BBS_RECORD_RESOLVE_TRIGGER_PATH"), "resolve_record.trigger")


def request_record_resolve_trigger(scope: str, key: str) -> None:
  normalized_scope = str(scope or "").strip().lower()
  normalized_key = str(key or "").strip()
  if not normalized_scope or not normalized_key:
    raise ValueError("scope and key required")
  trigger_path = get_record_resolve_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    json.dump({"scope": normalized_scope, "key": normalized_key}, trigger_file)
  os.replace(tmp_path, trigger_path)


def load_runtime_snapshot(snapshot_path: str) -> dict:
  if not os.path.exists(snapshot_path):
    return {}
  try:
    with open(snapshot_path, "r", encoding="utf-8") as snapshot_file:
      data = json.load(snapshot_file)
      if isinstance(data, dict):
        return data
  except Exception:
    return {}
  return {}


def classify_connection_event_display_type(message_type: str, event_text: str) -> str:
  normalized = str(message_type or "").strip().lower()
  text = str(event_text or "")
  if normalized in {"critical", "error"}:
    return "error"
  if normalized == "warning":
    return "warn"
  if normalized in {"info", "debug"}:
    return "log"
  if text.startswith("RX "):
    return "rx"
  if normalized in {"sync", "direct", "drop"}:
    return normalized
  if normalized == "user":
    return "rx"
  return "log"


def get_connection_event_label(message_type: str, event_text: str) -> str:
  display_type = classify_connection_event_display_type(message_type, event_text)
  return {
    "rx": "RX",
    "sync": "SYNC",
    "direct": "DIRECT",
    "drop": "DROP",
    "log": "LOG",
    "warn": "WARN",
    "error": "ERROR",
  }.get(display_type, display_type.upper())


def serialize_connection_event(row) -> dict:
  raw = dict(row)
  raw["display_type"] = classify_connection_event_display_type(raw.get("message_type", ""), raw.get("event_text", ""))
  raw["display_label"] = get_connection_event_label(raw.get("message_type", ""), raw.get("event_text", ""))
  raw["source_label"] = (
    raw.get("sender_short_name")
    or raw.get("sender_node_id")
    or raw.get("sender_num")
    or raw.get("message_type")
    or "system"
  )
  return raw


_UNIMPORTANT_SYNC_FRAMES = {"BULLETINCONT", "MAILCONT", "HASHREC", "HASHZ"}


def classify_sync_transmission_importance(frame_type: str, is_continuation: bool) -> bool:
  normalized = str(frame_type or "").strip().upper()
  if normalized in {"SYNCSTATE", "HASHREQ", "HASHMISS", "HASHEND", "DELETE_BULLETIN", "DELETE_MAIL", "DELETE_CHANNELCOMMENT", "DELETE_ZORKSAVE", "BULLETIN", "MAIL", "CHANNEL", "CHANNELCOMMENT", "PROFILESYNC", "SCORESYNC", "ZORKSAVE", "CANDREQ", "CANDRSP"}:
    return True
  if bool(is_continuation):
    return False
  return normalized not in _UNIMPORTANT_SYNC_FRAMES


def build_sync_transmission_preview(frame_text: str, max_len: int = 150) -> str:
  normalized = str(frame_text or "").replace("\r", " ").replace("\n", " ").strip()
  if len(normalized) <= max_len:
    return normalized
  return f"{normalized[:max_len - 3]}..."


def serialize_sync_transmission(row) -> dict:
  raw = dict(row)
  raw["direction"] = str(raw.get("direction") or "tx").lower()
  raw["peer_node_id"] = raw.get("destination_node_id") or "broadcast"
  raw["frame_type"] = str(raw.get("frame_type") or "")
  raw["frame_size_bytes"] = int(raw.get("frame_size_bytes") or 0)
  raw["is_continuation"] = bool(raw.get("is_continuation"))
  raw["frame_text"] = str(raw.get("frame_text") or "")
  raw["frame_preview"] = build_sync_transmission_preview(raw["frame_text"])
  raw["direction_label"] = "OUT" if raw["direction"] == "tx" else "IN"
  raw["is_important"] = classify_sync_transmission_importance(raw["frame_type"], raw["is_continuation"])
  raw["preview"] = raw["frame_preview"]
  raw["importance"] = "important" if raw["is_important"] else "normal"
  return raw


BASE_TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <meta name=\"csrf-token\" content=\"{{ csrf_token }}\">
  <title>{{ title }}</title>
  <style>
    :root {
      --bg: #0f141b;
      --text: #d7dde5;
      --card-bg: #161d27;
      --card-border: #2b3645;
      --link: #7ab2ff;
      --table-header-bg: #1f2937;
      --table-border: #2b3645;
      --input-bg: #0f141b;
      --input-text: #d7dde5;
      --input-border: #3b4a5f;
      --btn-bg: #1b2533;
      --btn-border: #3b4a5f;
      --muted: #9aa8ba;
      --flash-bg: #17202b;
      --code-bg: #0b1016;
      --drag-bg: #243044;
      --drag-over: #7ab2ff;
    }
    body[data-theme='light'] {
      --bg: #f6f7fb;
      --text: #222;
      --card-bg: #fff;
      --card-border: #ddd;
      --link: #0056d6;
      --table-header-bg: #f0f3fa;
      --table-border: #ddd;
      --input-bg: #fff;
      --input-text: #222;
      --input-border: #ccc;
      --btn-bg: #fff;
      --btn-border: #bbb;
      --muted: #666;
      --flash-bg: #fafafa;
      --code-bg: #f3f4f6;
      --drag-bg: #f9f9f9;
      --drag-over: #0056d6;
    }
    body { font-family: Arial, sans-serif; margin: 24px; background: var(--bg); color: var(--text); }
    .container { max-width: 1200px; margin: 0 auto; }
    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
    .nav a { text-decoration: none; color: var(--link); }
    .nav { margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border: 1px solid var(--table-border); padding: 8px; vertical-align: top; text-align: left; }
    th { background: var(--table-header-bg); }
    tr.dragging { opacity: 0.5; background: var(--drag-bg); }
    tr.drag-over { border-top: 3px solid var(--drag-over); }
    input[type=text], input[type=password], textarea, select {
      width: 100%;
      padding: 8px;
      border: 1px solid var(--input-border);
      border-radius: 6px;
      background: var(--input-bg);
      color: var(--input-text);
    }
    textarea { min-height: 180px; }
    .row-actions { display: flex; gap: 8px; }
    .btn { border: 1px solid var(--btn-border); border-radius: 6px; padding: 6px 10px; background: var(--btn-bg); color: var(--text); cursor: pointer; }
    .btn-primary { border-color: #0056d6; color: #fff; background: #0056d6; }
    .btn-danger { border-color: #b91c1c; color: #fff; background: #b91c1c; }
    .btn-small { padding: 4px 6px; font-size: 12px; }
    .nav-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
    .version-chip {
      border: 1px solid var(--btn-border);
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 11px;
      color: var(--muted);
      background: var(--btn-bg);
      white-space: nowrap;
    }
    .theme-toggle { margin-left: 0; }
    .sync-pill {
      border: 1px solid var(--btn-border);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      user-select: none;
      cursor: pointer;
      white-space: nowrap;
      min-width: 190px;
      text-align: center;
      background: var(--btn-bg);
      color: var(--text);
    }
    .sync-pill.active {
      border-color: #0f766e;
      box-shadow: 0 0 0 1px #0f766e inset;
    }
    .reorder-handle { cursor: grab; color: #999; padding: 4px 8px; }
    .reorder-handle:hover { color: #0056d6; }
    .reorder-handle:active { cursor: grabbing; }
    .muted { color: var(--muted); font-size: 12px; }
    .flash { padding: 10px; border-radius: 6px; margin-bottom: 12px; border: 1px solid var(--table-border); background: var(--flash-bg); }
    .flash-error { border-color: #b91c1c; background: #fff1f2; }
    .flash-success { border-color: #0f766e; background: #f0fdfa; }
    .search-bar { display: flex; gap: 8px; margin-bottom: 12px; }
    .inline { display: inline; }
    code { background: var(--code-bg); padding: 2px 6px; border-radius: 4px; }
    .flowchart-controls { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }
    .flowchart-controls .zoom-label { color: var(--muted); font-size: 12px; }
    .flowchart-viewport {
      overflow: hidden;
      border: 1px solid var(--table-border);
      border-radius: 8px;
      background: var(--bg);
      cursor: grab;
      touch-action: none;
      min-height: 540px;
      height: min(78vh, 980px);
      background-image:
        linear-gradient(rgba(148, 163, 184, 0.12) 1px, transparent 1px),
        linear-gradient(90deg, rgba(148, 163, 184, 0.12) 1px, transparent 1px);
      background-size: 28px 28px;
    }
    .flowchart-viewport.dragging { cursor: grabbing; }
    .flowchart-svg {
      width: 100%;
      height: 100%;
      display: block;
    }
    .terminal-window {
      background: #070b10;
      border: 1px solid #2b3645;
      border-radius: 8px;
      color: #9fe870;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.4;
      height: 320px;
      overflow-y: auto;
      padding: 10px;
      white-space: pre-wrap;
    }
    .terminal-line { margin-bottom: 2px; }
    .terminal-time { color: #7ab2ff; }
    .terminal-type { color: #f4c95d; }
    .terminal-controls {
      display: flex;
      gap: 6px;
      margin-bottom: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .terminal-btn {
      background: #1a2535;
      border: 1px solid #2b3645;
      border-radius: 4px;
      color: #9fe870;
      cursor: pointer;
      font-family: Consolas, "Courier New", monospace;
      font-size: 11px;
      padding: 3px 8px;
      transition: background 0.15s;
    }
    .terminal-btn:hover { background: #243040; }
    .terminal-btn.active { background: #2b4a2b; border-color: #9fe870; color: #fff; }
    .terminal-btn.btn-pause.active { background: #4a3b1a; border-color: #f4c95d; color: #f4c95d; }
    .terminal-btn.btn-clear { color: #ff7d7d; border-color: #4a2020; }
    .terminal-btn.btn-clear:hover { background: #3a1515; }
    .filter-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-bottom: 12px;
    }
    .log-grid {
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }
    .log-pane {
      border: 1px solid var(--card-border);
      border-radius: 8px;
      background: var(--card-bg);
      padding: 12px;
    }
    .log-pane h4 { margin-top: 0; margin-bottom: 8px; }
    .log-meta { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
    .log-window {
      background: #070b10;
      border: 1px solid #2b3645;
      border-radius: 8px;
      color: #d7dde5;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.4;
      height: 360px;
      overflow-y: auto;
      padding: 10px;
      white-space: pre-wrap;
    }
    .log-line { border-bottom: 1px solid rgba(122, 178, 255, 0.08); padding: 6px 0; }
    .log-line:last-child { border-bottom: 0; }
    .log-time { color: #7ab2ff; }
    .log-type { color: #f4c95d; font-weight: 700; }
    .log-peer { color: #9fe870; }
    .log-size { color: #9aa8ba; }
    .log-preview { color: #d7dde5; }
    .log-badge {
      display: inline-block;
      margin-left: 6px;
      padding: 1px 6px;
      border-radius: 999px;
      font-size: 10px;
      letter-spacing: 0.02em;
      border: 1px solid #3b4a5f;
      color: #d7dde5;
      background: #1b2533;
    }
    .log-badge.important { border-color: #0f766e; color: #b8ffef; }
    .channel-grid {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }
    .activity-list { margin: 0; padding-left: 18px; }
    .activity-list li { margin-bottom: 8px; }
    .peer-graph-grid {
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }
    .peer-graph-card {
      border: 1px solid var(--card-border);
      border-radius: 8px;
      background: var(--card-bg);
      padding: 12px;
    }
    .peer-graph-card h4 { margin: 0 0 6px 0; }
    .scope-bar-row { margin-top: 10px; }
    .scope-bar-header {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
      margin-bottom: 4px;
    }
    .scope-status-ok { color: #0f766e; }
    .scope-status-bad { color: #b91c1c; }
    .scope-bar-track {
      height: 10px;
      border-radius: 999px;
      overflow: hidden;
      background: var(--table-header-bg);
      display: flex;
    }
    .scope-bar-local { background: #2563eb; }
    .scope-bar-peer { background: #f59e0b; }
    .scope-bar-meta {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-top: 4px;
      font-size: 11px;
      color: var(--muted);
    }
    .pipeline-flow {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-top: 10px;
    }
    .pipeline-node {
      flex: 1 1 150px;
      min-width: 140px;
      border: 1px solid var(--card-border);
      border-radius: 8px;
      background: var(--card-bg);
      padding: 10px;
    }
    .pipeline-arrow {
      color: var(--muted);
      font-size: 18px;
      line-height: 1;
    }
  </style>
</head>
<body data-theme="dark">
  <div class=\"container\">
    {% if show_nav %}
    <div class=\"nav\">
      <a href=\"{{ url_for('table_list', table='bulletins') }}\">Bulletins</a>
      <a href=\"{{ url_for('table_list', table='channels') }}\">Channels</a>
      <a href=\"{{ url_for('clients_summary') }}\">Clients</a>
      <a href=\"{{ url_for('settings_page') }}\">Settings</a>
      <a href=\"{{ url_for('system_flowchart') }}\">Documentation</a>
      <a href=\"{{ url_for('system_transmissions') }}\">Transmission Stats</a>
      <a href=\"{{ url_for('meshtastic_device') }}\">Radio Device</a>
      <a href=\"{{ url_for('mesh_ui_index') }}\" target=\"_blank\">Mesh UI</a>
      <a href=\"{{ url_for('logout') }}\">Logout</a>
      <div class="nav-right">
        <div class="version-chip" title="Running version">{{ app_version_display }}</div>
        <div id="sync-status-pill" class="sync-pill" title="Hold for 1.2 seconds to force manual sync">Sync 0% | --:--</div>
        <button id="theme-toggle" class="btn btn-small theme-toggle" type="button">Switch to Light</button>
      </div>
    </div>
    {% endif %}

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class=\"flash {% if category == 'error' %}flash-error{% else %}flash-success{% endif %}\">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    {{ content|safe }}
  </div>
  <script>
    // Compatibility shim for client-side helpers that may call mgt.clearMarks.
    (function ensureMgtClearMarks() {
      const root = typeof globalThis !== 'undefined' ? globalThis : window;
      if (!root.mgt || typeof root.mgt !== 'object') {
        root.mgt = {};
      }
      if (typeof root.mgt.clearMarks !== 'function') {
        root.mgt.clearMarks = function () {};
      }
    })();

    function applyTheme(theme) {
      document.body.setAttribute('data-theme', theme);
      const toggle = document.getElementById('theme-toggle');
      if (toggle) {
        toggle.textContent = theme === 'dark' ? 'Switch to Light' : 'Switch to Dark';
      }
    }

    function initializeTheme() {
      const savedTheme = localStorage.getItem('bbs_theme');
      const theme = savedTheme === 'light' ? 'light' : 'dark';
      applyTheme(theme);

      const toggle = document.getElementById('theme-toggle');
      if (toggle) {
        toggle.addEventListener('click', () => {
          const nextTheme = document.body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
          localStorage.setItem('bbs_theme', nextTheme);
          applyTheme(nextTheme);
        });
      }
    }

    function getCsrfToken() {
      const meta = document.querySelector('meta[name="csrf-token"]');
      return meta ? String(meta.content || '') : '';
    }

    function withCsrfHeaders(headers) {
      const merged = Object.assign({}, headers || {});
      const csrf = getCsrfToken();
      if (csrf) {
        merged['X-CSRF-Token'] = csrf;
      }
      return merged;
    }

    function injectCsrfIntoForms() {
      const csrf = getCsrfToken();
      if (!csrf) {
        return;
      }
      document.querySelectorAll('form').forEach((form) => {
        const method = String(form.getAttribute('method') || 'get').toLowerCase();
        if (method !== 'post') {
          return;
        }
        let input = form.querySelector('input[name="csrf_token"]');
        if (!input) {
          input = document.createElement('input');
          input.type = 'hidden';
          input.name = 'csrf_token';
          form.appendChild(input);
        }
        input.value = csrf;
      });
    }

    function enableDragAndDrop(tableName) {
      const table = document.querySelector('table tbody');
      if (!table) return;
      
      let draggedRow = null;
      
      const rows = table.querySelectorAll('tr');
      rows.forEach(row => {
        row.draggable = true;
        
        row.addEventListener('dragstart', (e) => {
          draggedRow = row;
          row.classList.add('dragging');
          e.dataTransfer.effectAllowed = 'move';
        });
        
        row.addEventListener('dragend', () => {
          draggedRow = null;
          row.classList.remove('dragging');
          rows.forEach(r => r.classList.remove('drag-over'));
        });
        
        row.addEventListener('dragover', (e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          if (row !== draggedRow) {
            row.classList.add('drag-over');
          }
        });
        
        row.addEventListener('dragleave', () => {
          row.classList.remove('drag-over');
        });
        
        row.addEventListener('drop', async (e) => {
          e.preventDefault();
          row.classList.remove('drag-over');
          
          if (row !== draggedRow && draggedRow) {
            const draggedId = draggedRow.dataset.rowId;
            const targetId = row.dataset.rowId;
            
            try {
              const response = await fetch(`/api/reorder/${tableName}`, {
                method: 'POST',
                headers: withCsrfHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ from_id: draggedId, to_id: targetId })
              });
              
              if (response.ok) {
                const data = await response.json();
                if (data.success) {
                  location.reload();
                }
              }
            } catch (err) {
              console.error('Reorder failed:', err);
            }
          }
        });
      });
    }

    function initializeFlowchartNavigation() {
      const viewport = document.getElementById('flowchart-viewport');
      const svg = document.getElementById('flowchart-svg');
      const contentGroup = document.getElementById('flowchart-content-group');
      if (!viewport || !svg || !contentGroup) {
        return;
      }

      const zoomIn = document.getElementById('flowchart-zoom-in');
      const zoomOut = document.getElementById('flowchart-zoom-out');
      const reset = document.getElementById('flowchart-reset');
      const zoomLabel = document.getElementById('flowchart-zoom-label');

      let scale = 1;
      let panX = 0;
      let panY = 0;
      let activePointerId = null;
      let lastX = 0;
      let lastY = 0;

      function updateTransform() {
        contentGroup.setAttribute('transform', `matrix(${scale} 0 0 ${scale} ${panX} ${panY})`);
        if (zoomLabel) {
          zoomLabel.textContent = `${Math.round(scale * 100)}%`;
        }
      }

      function clampScale(value) {
        return Math.min(4.5, Math.max(0.65, value));
      }

      function getViewBox() {
        return svg.viewBox.baseVal;
      }

      function getViewportCenter() {
        const rect = svg.getBoundingClientRect();
        return {
          x: rect.left + (rect.width / 2),
          y: rect.top + (rect.height / 2),
        };
      }

      function screenToSvg(clientX, clientY) {
        const rect = svg.getBoundingClientRect();
        const viewBox = getViewBox();
        return {
          x: viewBox.x + ((clientX - rect.left) / rect.width) * viewBox.width,
          y: viewBox.y + ((clientY - rect.top) / rect.height) * viewBox.height,
        };
      }

      function zoomAt(nextScale, anchorClientX, anchorClientY) {
        const clampedScale = clampScale(nextScale);
        if (clampedScale === scale) {
          return;
        }
        const anchor = screenToSvg(anchorClientX, anchorClientY);
        panX += (scale - clampedScale) * anchor.x;
        panY += (scale - clampedScale) * anchor.y;
        scale = clampedScale;
        updateTransform();
      }

      function fitToViewport() {
        const bbox = contentGroup.getBBox();
        const padding = 80;
        svg.setAttribute(
          'viewBox',
          `${bbox.x - padding} ${bbox.y - padding} ${bbox.width + (padding * 2)} ${bbox.height + (padding * 2)}`,
        );
        scale = 1;
        panX = 0;
        panY = 0;
        updateTransform();
      }

      viewport.addEventListener('wheel', (event) => {
        event.preventDefault();
        const factor = event.deltaY < 0 ? 1.12 : 0.88;
        zoomAt(scale * factor, event.clientX, event.clientY);
      }, { passive: false });

      viewport.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) {
          return;
        }
        activePointerId = event.pointerId;
        lastX = event.clientX;
        lastY = event.clientY;
        viewport.classList.add('dragging');
        viewport.setPointerCapture(event.pointerId);
      });

      viewport.addEventListener('pointermove', (event) => {
        if (activePointerId !== event.pointerId) {
          return;
        }
        const rect = svg.getBoundingClientRect();
        const viewBox = getViewBox();
        const deltaX = (event.clientX - lastX) * (viewBox.width / rect.width);
        const deltaY = (event.clientY - lastY) * (viewBox.height / rect.height);
        panX += deltaX;
        panY += deltaY;
        lastX = event.clientX;
        lastY = event.clientY;
        updateTransform();
      });

      function endDrag(event) {
        if (activePointerId === null) {
          return;
        }
        if (event && event.pointerId !== undefined && event.pointerId !== activePointerId) {
          return;
        }
        try {
          viewport.releasePointerCapture(activePointerId);
        } catch (err) {
          // Ignore capture cleanup errors.
        }
        activePointerId = null;
        viewport.classList.remove('dragging');
      }

      viewport.addEventListener('pointerup', endDrag);
      viewport.addEventListener('pointercancel', endDrag);
      viewport.addEventListener('pointerleave', (event) => {
        if ((event.buttons & 1) === 0) {
          endDrag(event);
        }
      });

      if (zoomIn) {
        zoomIn.addEventListener('click', () => {
          const center = getViewportCenter();
          zoomAt(scale * 1.15, center.x, center.y);
        });
      }

      if (zoomOut) {
        zoomOut.addEventListener('click', () => {
          const center = getViewportCenter();
          zoomAt(scale * 0.87, center.x, center.y);
        });
      }

      if (reset) {
        reset.addEventListener('click', () => {
          fitToViewport();
        });
      }

      fitToViewport();
    }

    function formatCountdown(seconds) {
      const clamped = Math.max(0, Number(seconds || 0));
      const mm = String(Math.floor(clamped / 60)).padStart(2, '0');
      const ss = String(clamped % 60).padStart(2, '0');
      return `${mm}:${ss}`;
    }

    function initializeSyncNavStatus() {
      const pill = document.getElementById('sync-status-pill');
      if (!pill) {
        return;
      }

      let secondsUntilNext = 0;
      let holdTimer = null;
      let currentProgressPercent = 0;
      let currentInProgress = false;
      let currentPeerStatus = 'no peer reports';

      function renderStatus(inProgress, progressPercent) {
        currentInProgress = inProgress;
        currentProgressPercent = progressPercent;
        const left = `Sync ${progressPercent}%`;
        const mismatchRetrying = (!inProgress) && currentPeerStatus.startsWith('mismatch') && String(window._syncLastTriggerReason || '') === 'mismatch';
        const right = inProgress
          ? 'running'
          : (mismatchRetrying ? `retrying | ${currentPeerStatus}` : `${formatCountdown(secondsUntilNext)} | ${currentPeerStatus}`);
        pill.textContent = `${left} | ${right}`;
        pill.classList.toggle('active', inProgress);
      }

      async function refreshStatus() {
        try {
          const resp = await fetch('/api/sync/status', { headers: { 'Accept': 'application/json' } });
          if (!resp.ok) {
            return;
          }
          const data = await resp.json();
          secondsUntilNext = Number(data.seconds_until_next || 0);
          currentPeerStatus = String(data.peer_status_text || 'no peer reports');
          window._syncLastTriggerReason = String(data.last_trigger_reason || 'scheduled');
          renderStatus(Boolean(data.in_progress), Number(data.progress_percent || 0));
        } catch (err) {
          // Keep existing display if one poll fails.
        }
      }

      async function forceManualSync() {
        try {
          const resp = await fetch('/api/sync/manual', {
            method: 'POST',
            headers: withCsrfHeaders(),
          });
          if (resp.ok) {
            pill.textContent = 'Sync requested';
            setTimeout(refreshStatus, 1000);
          }
        } catch (err) {
          // Ignore transient API failures.
        }
      }

      function startHold() {
        clearTimeout(holdTimer);
        holdTimer = setTimeout(() => {
          forceManualSync();
          holdTimer = null;
        }, 1200);
      }

      function cancelHold() {
        if (holdTimer) {
          clearTimeout(holdTimer);
          holdTimer = null;
        }
      }

      pill.addEventListener('mousedown', startHold);
      pill.addEventListener('touchstart', startHold, { passive: true });
      pill.addEventListener('mouseup', cancelHold);
      pill.addEventListener('mouseleave', cancelHold);
      pill.addEventListener('touchend', cancelHold);
      pill.addEventListener('touchcancel', cancelHold);

      setInterval(() => {
        if (secondsUntilNext > 0) {
          secondsUntilNext -= 1;
        }
        renderStatus(currentInProgress, currentProgressPercent);
      }, 1000);

      setInterval(refreshStatus, 5000);
      refreshStatus();
    }
    
    document.addEventListener('DOMContentLoaded', () => {
      injectCsrfIntoForms();
      initializeTheme();
      initializeSyncNavStatus();
      initializeFlowchartNavigation();
      const table = document.querySelector('table');
      if (table && table.dataset.draggable) {
        enableDragAndDrop(table.dataset.tableName);
      }
    });
  </script>
</body>
</html>
"""


LOGIN_CONTENT = """
<div class=\"card\" style=\"max-width: 420px; margin: 60px auto;\">
  <h2>Bacon BBS Database Admin</h2>
  <p class=\"muted\">Standalone moderation interface for local SQLite data.</p>
  <form method=\"post\">
    <label>Username</label><br>
    <input type=\"text\" name=\"username\" required><br><br>
    <label>Password</label><br>
    <input type=\"password\" name=\"password\" required><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Sign in</button>
  </form>
</div>
"""


NEW_BULLETIN_CONTENT = """
<div class=\"card\" style=\"max-width: 760px;\">
  <h2>New Bulletin Post</h2>
  <form method=\"post\">
    <label>Board</label><br>
    <select name=\"board\" required>
      {% for board in bulletin_boards %}
      <option value=\"{{ board }}\" {% if board == selected_board %}selected{% endif %}>{{ board }}</option>
      {% endfor %}
    </select><br><br>
    <label>Sender Short Name</label><br>
    <input type=\"text\" name=\"sender_short_name\" required><br><br>
    <label>Subject</label><br>
    <input type=\"text\" name=\"subject\" required><br><br>
    <label>Content</label><br>
    <textarea name=\"content\" required></textarea><br><br>
    <label><input type=\"checkbox\" name=\"local_only\" value=\"1\"> Local only</label><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Create Post</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='bulletins') }}\">Back</a>
  </form>
</div>
"""


NEW_CHANNEL_CONTENT = """
<div class=\"card\" style=\"max-width: 760px;\">
  <h2>New Channel Entry</h2>
  <form method=\"post\">
    <label>Name</label><br>
    <input type=\"text\" name=\"name\" required><br><br>
    <label>URL / PSK</label><br>
    <input type=\"text\" name=\"url\" required><br><br>
    <label><input type=\"checkbox\" name=\"local_only\" value=\"1\"> Local only</label><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Create Channel</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='channels') }}\">Back</a>
  </form>
</div>
"""


EDIT_CONTENT = """
<div class=\"card\" style=\"max-width: 760px;\">
  <h2>Edit {{ table_title }}</h2>
  <form method=\"post\">
    {% for field in editable_fields %}
    <label>{{ field }}</label><br>
    {% if field == 'content' %}
    <textarea name=\"{{ field }}\" required>{{ row[field] }}</textarea><br><br>
    {% else %}
    <input type=\"text\" name=\"{{ field }}\" value=\"{{ row[field] }}\" required><br><br>
    {% endif %}
    {% endfor %}
    <button class=\"btn btn-primary\" type=\"submit\">Save Changes</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Back</a>
  </form>
</div>
"""


EDIT_BULLETIN_CONTENT = """
<div class=\"card\" style=\"max-width: 760px;\">
  <h2>Edit {{ table_title }}</h2>
  <form method=\"post\">
    <label>board</label><br>
    <select name=\"board\" required>
      {% for board in bulletin_boards %}
      <option value=\"{{ board }}\" {% if row['board'] == board %}selected{% endif %}>{{ board }}</option>
      {% endfor %}
    </select><br><br>
    <label>sender_short_name</label><br>
    <input type=\"text\" name=\"sender_short_name\" value=\"{{ row['sender_short_name'] }}\" required><br><br>
    <label>date</label><br>
    <input type=\"text\" name=\"date\" value=\"{{ row['date'] }}\" required><br><br>
    <label>subject</label><br>
    <input type=\"text\" name=\"subject\" value=\"{{ row['subject'] }}\" required><br><br>
    <label>content</label><br>
    <textarea name=\"content\" required>{{ row['content'] }}</textarea><br><br>
    <label>local_only</label><br>
    <input type=\"text\" name=\"local_only\" value=\"{{ row['local_only'] }}\" required><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Save Changes</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Back</a>
  </form>
</div>
"""


UPDATED_FLOWCHART_CONTENT = """
<div class=\"card\">
  <h2>Documentation</h2>
  <p class=\"muted\">Reference for this BBS instance: project overview, the runtime sync pipeline, sync protocol frames, BBS user commands, web admin pages, configuration, and database schema. The flowchart, snapshot, and tables on this page reflect the current code.</p>
</div>

<div class=\"card\">
  <h3>Project Overview</h3>
  <p class=\"muted\">TC&sup2;-BBS Meshtastic is a Python BBS that runs over the Meshtastic LoRa mesh radio network. Each node owns a local SQLite database (<code>bulletins.db</code>) and exchanges only what is needed with peers using a low-bandwidth, hash-repair-driven sync protocol. Two long-running processes are typical: <code>server.py</code> (mesh I/O, command handling, sync) and <code>web_admin.py</code> (this Flask web UI for moderation and diagnostics). A standalone Meshtastic Web bundle is also served at the <strong>Mesh UI</strong> link.</p>
  <ul class=\"muted\">
    <li><strong>Mail</strong> &mdash; per-recipient inbox messages.</li>
    <li><strong>Bulletins</strong> &mdash; per-board posts (default boards: General, Info, News, Urgent).</li>
    <li><strong>Channels</strong> &mdash; shared Meshtastic channel directory + per-channel comments.</li>
    <li><strong>Profiles</strong> &mdash; per-user profile records synced across nodes.</li>
    <li><strong>Game data</strong> &mdash; Hall of Fame scores and Zork (dfrotz) interactive-fiction saves.</li>
    <li><strong>Tombstones</strong> &mdash; deletes are replayed as tombstones so removed records cannot be resurrected by a peer.</li>
  </ul>
</div>

<div class=\"card\">
  <h3>Five-Phase Mesh Sync</h3>
  <p class=\"muted\">The scheduler pushes user-visible content first, then profile metadata, and only then game data. Each phase is checked against the peer's last advertised SYNCSTATE; matching scopes are skipped and only mismatched scopes are repaired. Repair stays record-scoped instead of forcing a full replay.</p>
  <div class=\"pipeline-flow\">
    <div class=\"pipeline-node\"><strong>Triggers</strong><br><span class=\"muted\">scheduled sync, manual sync, peer resync, force mismatch check, resolve save key, resolve record key, inbound peer traffic</span></div>
    <div class=\"pipeline-arrow\">→</div>
    <div class=\"pipeline-node\"><strong>Phase 1</strong><br><span class=\"muted\">mail</span></div>
    <div class=\"pipeline-arrow\">→</div>
    <div class=\"pipeline-node\"><strong>Phase 2</strong><br><span class=\"muted\">bulletins</span></div>
    <div class=\"pipeline-arrow\">→</div>
    <div class=\"pipeline-node\"><strong>Phase 3</strong><br><span class=\"muted\">channels</span></div>
    <div class=\"pipeline-arrow\">→</div>
    <div class=\"pipeline-node\"><strong>Phase 4</strong><br><span class=\"muted\">profiles</span></div>
    <div class=\"pipeline-arrow\">→</div>
    <div class=\"pipeline-node\"><strong>Phase 5</strong><br><span class=\"muted\">game scores + zork saves</span></div>
  </div>
  <div class=\"pipeline-flow\">
    <div class=\"pipeline-node\"><strong>Selective hash repair</strong><br><span class=\"muted\">SYNCSTATE → HASHREQ → HASHREC/HASHEND or HASHZ/HASHZGAP (compressed) → HASHMISS replay</span></div>
    <div class=\"pipeline-node\"><strong>Chunk healing</strong><br><span class=\"muted\">MAILMETA / BULLMETA / CHANNELCOMMENTMETA + CONT frames; incomplete markers stay visible until repaired. Compact channel keys avoid 220-byte radio packet overflow.</span></div>
    <div class=\"pipeline-node\"><strong>Tombstones</strong><br><span class=\"muted\">DELETE_BULLETIN, DELETE_MAIL, DELETE_ZORKSAVE flow through deleted_sync_tombstones so older deletes cannot wipe newer records.</span></div>
    <div class=\"pipeline-node\"><strong>Zork conflict assist</strong><br><span class=\"muted\">CANDREQ / CANDRSP asks every peer for one save key, ranks candidates by newest timestamp / largest payload / stable hash, then replays the winner.</span></div>
  </div>
</div>

<div class=\"card\" style=\"background: transparent;\">
  <div class=\"flowchart-controls\">
    <button id=\"flowchart-zoom-in\" class=\"btn btn-small\" type=\"button\">Zoom In</button>
    <button id=\"flowchart-zoom-out\" class=\"btn btn-small\" type=\"button\">Zoom Out</button>
    <button id=\"flowchart-reset\" class=\"btn btn-small\" type=\"button\">Reset View</button>
    <span id=\"flowchart-zoom-label\" class=\"zoom-label\">100%</span>
    <span class=\"zoom-label\">Wheel to zoom and drag to pan.</span>
  </div>
  <div id=\"flowchart-viewport\" class=\"flowchart-viewport\">
    <svg id=\"flowchart-svg\" class=\"flowchart-svg\" viewBox=\"0 0 1600 1500\" preserveAspectRatio=\"xMidYMid meet\">
      <g id=\"flowchart-content-group\">
        <text x=\"800\" y=\"42\" font-size=\"28\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#1f2937\">Current Mesh Sync, Repair, and Save Resolution Flow</text>
        <text x=\"800\" y=\"68\" font-size=\"13\" text-anchor=\"middle\" fill=\"#475569\">This view reflects the current runtime instead of the earlier command-tree sketch.</text>

        <rect x=\"70\" y=\"110\" width=\"1460\" height=\"150\" rx=\"16\" fill=\"#e0f2fe\" stroke=\"#0284c7\" stroke-width=\"2\"/>
        <text x=\"120\" y=\"145\" font-size=\"18\" font-weight=\"bold\" fill=\"#0f172a\">1. Triggers</text>
        <rect x=\"120\" y=\"170\" width=\"240\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#38bdf8\" stroke-width=\"1.5\"/>
        <text x=\"240\" y=\"194\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Scheduled Loop</text>
        <text x=\"240\" y=\"213\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">normal peer sync cadence</text>
        <rect x=\"400\" y=\"170\" width=\"240\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#38bdf8\" stroke-width=\"1.5\"/>
        <text x=\"520\" y=\"194\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Manual Actions</text>
        <text x=\"520\" y=\"213\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">sync now, peer full resync</text>
        <rect x=\"680\" y=\"170\" width=\"240\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#38bdf8\" stroke-width=\"1.5\"/>
        <text x=\"800\" y=\"194\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Force Mismatch Check</text>
        <text x=\"800\" y=\"213\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">deliberately re-compare hashes</text>
        <rect x=\"960\" y=\"170\" width=\"240\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#38bdf8\" stroke-width=\"1.5\"/>
        <text x=\"1080\" y=\"194\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Resolve Zork Save</text>
        <text x=\"1080\" y=\"213\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">manual key-specific resolver</text>
        <rect x=\"1240\" y=\"170\" width=\"240\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#38bdf8\" stroke-width=\"1.5\"/>
        <text x=\"1360\" y=\"194\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Inbound Peer Traffic</text>
        <text x=\"1360\" y=\"213\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">received sync, repair, tombstones</text>

        <line x1=\"800\" y1=\"260\" x2=\"800\" y2=\"315\" stroke=\"#0f172a\" stroke-width=\"2.5\"/>
        <polygon points=\"790,305 810,305 800,320\" fill=\"#0f172a\"/>

        <rect x=\"120\" y=\"320\" width=\"1360\" height=\"184\" rx=\"16\" fill=\"#ecfccb\" stroke=\"#65a30d\" stroke-width=\"2\"/>
        <text x=\"170\" y=\"355\" font-size=\"18\" font-weight=\"bold\" fill=\"#365314\">2. Five-phase outbound sync order</text>
        <text x=\"170\" y=\"378\" font-size=\"12\" fill=\"#4d7c0f\">Each peer pass sends scope hashes in a stable order so user-visible content settles before game data.</text>

        <rect x=\"150\" y=\"410\" width=\"175\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#84cc16\" stroke-width=\"1.5\"/>
        <text x=\"237\" y=\"434\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Phase 1</text>
        <text x=\"237\" y=\"453\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">mail</text>
        <rect x=\"365\" y=\"410\" width=\"175\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#84cc16\" stroke-width=\"1.5\"/>
        <text x=\"452\" y=\"434\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Phase 2</text>
        <text x=\"452\" y=\"453\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">bulletins</text>
        <rect x=\"580\" y=\"410\" width=\"175\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#84cc16\" stroke-width=\"1.5\"/>
        <text x=\"667\" y=\"434\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Phase 3</text>
        <text x=\"667\" y=\"453\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">channels</text>
        <rect x=\"795\" y=\"410\" width=\"175\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#84cc16\" stroke-width=\"1.5\"/>
        <text x=\"882\" y=\"434\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Phase 4</text>
        <text x=\"882\" y=\"453\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">profiles</text>
        <rect x=\"1010\" y=\"410\" width=\"300\" height=\"58\" rx=\"12\" fill=\"#ffffff\" stroke=\"#84cc16\" stroke-width=\"1.5\"/>
        <text x=\"1160\" y=\"434\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Phase 5</text>
        <text x=\"1160\" y=\"453\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">game scores + zork saves</text>
        <line x1=\"325\" y1=\"439\" x2=\"365\" y2=\"439\" stroke=\"#65a30d\" stroke-width=\"2\"/>
        <line x1=\"540\" y1=\"439\" x2=\"580\" y2=\"439\" stroke=\"#65a30d\" stroke-width=\"2\"/>
        <line x1=\"755\" y1=\"439\" x2=\"795\" y2=\"439\" stroke=\"#65a30d\" stroke-width=\"2\"/>
        <line x1=\"970\" y1=\"439\" x2=\"1010\" y2=\"439\" stroke=\"#65a30d\" stroke-width=\"2\"/>

        <line x1=\"800\" y1=\"504\" x2=\"800\" y2=\"560\" stroke=\"#0f172a\" stroke-width=\"2.5\"/>
        <polygon points=\"790,550 810,550 800,565\" fill=\"#0f172a\"/>

        <rect x=\"80\" y=\"565\" width=\"1440\" height=\"300\" rx=\"16\" fill=\"#ede9fe\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <text x=\"130\" y=\"600\" font-size=\"18\" font-weight=\"bold\" fill=\"#4c1d95\">3. Selective hash repair</text>
        <text x=\"130\" y=\"623\" font-size=\"12\" fill=\"#6d28d9\">Repairs now stay scoped: the peer declares hashes, the receiver asks only for mismatched records, and replay uses the native frame type.</text>

        <rect x=\"130\" y=\"660\" width=\"250\" height=\"72\" rx=\"12\" fill=\"#ffffff\" stroke=\"#8b5cf6\" stroke-width=\"1.5\"/>
        <text x=\"255\" y=\"690\" font-size=\"14\" text-anchor=\"middle\" font-weight=\"bold\">SYNCSTATE</text>
        <text x=\"255\" y=\"711\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">peer advertises counts + hashes</text>

        <polygon points=\"520,695 610,640 700,695 610,750\" fill=\"#ffffff\" stroke=\"#8b5cf6\" stroke-width=\"1.5\"/>
        <text x=\"610\" y=\"690\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Hashes</text>
        <text x=\"610\" y=\"708\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">match?</text>

        <rect x=\"820\" y=\"640\" width=\"260\" height=\"72\" rx=\"12\" fill=\"#ffffff\" stroke=\"#8b5cf6\" stroke-width=\"1.5\"/>
        <text x=\"950\" y=\"670\" font-size=\"14\" text-anchor=\"middle\" font-weight=\"bold\">HASHREQ</text>
        <text x=\"950\" y=\"691\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">request per-scope record hashes</text>

        <rect x=\"1160\" y=\"640\" width=\"300\" height=\"72\" rx=\"12\" fill=\"#ffffff\" stroke=\"#8b5cf6\" stroke-width=\"1.5\"/>
        <text x=\"1310\" y=\"666\" font-size=\"14\" text-anchor=\"middle\" font-weight=\"bold\">HASHREC / HASHEND</text>
        <text x=\"1310\" y=\"684\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">peer streams hashes; HASHZ/HASHZGAP</text>
        <text x=\"1310\" y=\"700\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">used when compression is enabled</text>

        <rect x=\"820\" y=\"760\" width=\"300\" height=\"72\" rx=\"12\" fill=\"#ffffff\" stroke=\"#8b5cf6\" stroke-width=\"1.5\"/>
        <text x=\"970\" y=\"790\" font-size=\"14\" text-anchor=\"middle\" font-weight=\"bold\">HASHMISS</text>
        <text x=\"970\" y=\"811\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">request only missing records or tombstones</text>

        <rect x=\"1160\" y=\"760\" width=\"300\" height=\"72\" rx=\"12\" fill=\"#ffffff\" stroke=\"#8b5cf6\" stroke-width=\"1.5\"/>
        <text x=\"1310\" y=\"790\" font-size=\"14\" text-anchor=\"middle\" font-weight=\"bold\">Replay Native Frame</text>
        <text x=\"1310\" y=\"811\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">BULLETIN, MAIL, CHANNEL, PROFILESYNC, SCORESYNC, ZORKSAVE</text>

        <rect x=\"130\" y=\"760\" width=\"250\" height=\"72\" rx=\"12\" fill=\"#f0fdf4\" stroke=\"#16a34a\" stroke-width=\"1.5\"/>
        <text x=\"255\" y=\"790\" font-size=\"14\" text-anchor=\"middle\" font-weight=\"bold\">Hashes Match</text>
        <text x=\"255\" y=\"811\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">scope is clean, continue forward</text>

        <line x1=\"380\" y1=\"696\" x2=\"520\" y2=\"696\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"700\" y1=\"676\" x2=\"820\" y2=\"676\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"1080\" y1=\"676\" x2=\"1160\" y2=\"676\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"1310\" y1=\"712\" x2=\"1310\" y2=\"760\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"1120\" y1=\"796\" x2=\"1160\" y2=\"796\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"610\" y1=\"750\" x2=\"610\" y2=\"796\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"610\" y1=\"796\" x2=\"820\" y2=\"796\" stroke=\"#7c3aed\" stroke-width=\"2\"/>
        <line x1=\"520\" y1=\"715\" x2=\"380\" y2=\"796\" stroke=\"#16a34a\" stroke-width=\"2\"/>
        <text x=\"440\" y=\"764\" font-size=\"11\" fill=\"#166534\">yes</text>
        <text x=\"716\" y=\"657\" font-size=\"11\" fill=\"#6d28d9\">no</text>

        <rect x=\"80\" y=\"915\" width=\"1440\" height=\"250\" rx=\"16\" fill=\"#fff7ed\" stroke=\"#ea580c\" stroke-width=\"2\"/>
        <text x=\"130\" y=\"950\" font-size=\"18\" font-weight=\"bold\" fill=\"#9a3412\">4. Special cases that changed recently</text>
        <rect x=\"130\" y=\"985\" width=\"400\" height=\"135\" rx=\"12\" fill=\"#ffffff\" stroke=\"#fb923c\" stroke-width=\"1.5\"/>
        <text x=\"165\" y=\"1015\" font-size=\"15\" font-weight=\"bold\" fill=\"#7c2d12\">Long bulletins, mail, channel comments</text>
        <text x=\"165\" y=\"1040\" font-size=\"12\" fill=\"#475569\">MAILMETA / BULLMETA / CHANNELCOMMENTMETA</text>
        <text x=\"165\" y=\"1060\" font-size=\"12\" fill=\"#475569\">declare chunk counts; CONT frames fill gaps.</text>
        <text x=\"165\" y=\"1080\" font-size=\"12\" fill=\"#475569\">Compact channel keys avoid 220-byte overflow.</text>

        <rect x=\"600\" y=\"985\" width=\"400\" height=\"135\" rx=\"12\" fill=\"#ffffff\" stroke=\"#fb923c\" stroke-width=\"1.5\"/>
        <text x=\"635\" y=\"1015\" font-size=\"15\" font-weight=\"bold\" fill=\"#7c2d12\">Delete replay uses tombstones</text>
        <text x=\"635\" y=\"1040\" font-size=\"12\" fill=\"#475569\">DELETE_BULLETIN, DELETE_MAIL, DELETE_ZORKSAVE</text>
        <text x=\"635\" y=\"1060\" font-size=\"12\" fill=\"#475569\">flow through deleted_sync_tombstones.</text>
        <text x=\"635\" y=\"1080\" font-size=\"12\" fill=\"#475569\">Older deletes cannot wipe newer zork saves.</text>

        <rect x=\"1070\" y=\"985\" width=\"400\" height=\"135\" rx=\"12\" fill=\"#ffffff\" stroke=\"#fb923c\" stroke-width=\"1.5\"/>
        <text x=\"1105\" y=\"1015\" font-size=\"15\" font-weight=\"bold\" fill=\"#7c2d12\">Manual zork save best-candidate resolver</text>
        <text x=\"1105\" y=\"1040\" font-size=\"12\" fill=\"#475569\">CANDREQ asks every peer for one save key.</text>
        <text x=\"1105\" y=\"1060\" font-size=\"12\" fill=\"#475569\">CANDRSP ranks save vs tombstone metadata.</text>
        <text x=\"1105\" y=\"1080\" font-size=\"12\" fill=\"#475569\">Winner is replayed through HASHMISS/native delete.</text>

        <line x1=\"800\" y1=\"1165\" x2=\"800\" y2=\"1220\" stroke=\"#0f172a\" stroke-width=\"2.5\"/>
        <polygon points=\"790,1210 810,1210 800,1225\" fill=\"#0f172a\"/>

        <rect x=\"180\" y=\"1225\" width=\"1240\" height=\"170\" rx=\"16\" fill=\"#dcfce7\" stroke=\"#16a34a\" stroke-width=\"2\"/>
        <text x=\"230\" y=\"1260\" font-size=\"18\" font-weight=\"bold\" fill=\"#166534\">5. Admin visibility</text>
        <rect x=\"230\" y=\"1288\" width=\"280\" height=\"68\" rx=\"12\" fill=\"#ffffff\" stroke=\"#4ade80\" stroke-width=\"1.5\"/>
        <text x=\"370\" y=\"1316\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Peer Hash Graph</text>
        <text x=\"370\" y=\"1336\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">scope-level mismatch visibility</text>
        <rect x=\"560\" y=\"1288\" width=\"280\" height=\"68\" rx=\"12\" fill=\"#ffffff\" stroke=\"#4ade80\" stroke-width=\"1.5\"/>
        <text x=\"700\" y=\"1316\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Zork Save Tombstones</text>
        <text x=\"700\" y=\"1336\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">recent DELETE_ZORKSAVE history</text>
        <rect x=\"890\" y=\"1288\" width=\"280\" height=\"68\" rx=\"12\" fill=\"#ffffff\" stroke=\"#4ade80\" stroke-width=\"1.5\"/>
        <text x=\"1030\" y=\"1316\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Best-Candidate Resolver</text>
        <text x=\"1030\" y=\"1336\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">active and recent candidate requests</text>
        <rect x=\"1220\" y=\"1288\" width=\"150\" height=\"68\" rx=\"12\" fill=\"#ffffff\" stroke=\"#4ade80\" stroke-width=\"1.5\"/>
        <text x=\"1295\" y=\"1316\" font-size=\"13\" text-anchor=\"middle\" font-weight=\"bold\">Sync API</text>
        <text x=\"1295\" y=\"1336\" font-size=\"11\" text-anchor=\"middle\" fill=\"#475569\">manual repair triggers</text>
      </g>
    </svg>
  </div>
</div>

<div class=\"card\">
  <h3>Sync Protocol Frame Reference</h3>
  <p class=\"muted\">All frames travel as Meshtastic text messages on the channel where the BBS node is listening. Maximum payload per frame is approximately 220 bytes; larger records are split into base + META + CONT frames.</p>
  <table>
    <thead><tr><th>Frame</th><th>Direction</th><th>Purpose</th></tr></thead>
    <tbody>
      <tr><td><code>SYNCSTATE</code></td><td>broadcast</td><td>Advertise local per-scope record counts and content hashes so peers can detect drift.</td></tr>
      <tr><td><code>HASHREQ</code></td><td>peer&rarr;peer</td><td>Request the per-record hash manifest for one scope.</td></tr>
      <tr><td><code>HASHREC</code> / <code>HASHEND</code></td><td>response</td><td>Stream record hashes and close out the scope (uncompressed manifest).</td></tr>
      <tr><td><code>HASHZ</code> / <code>HASHZGAP</code></td><td>response</td><td>Compressed manifest variant (enabled by <code>BBS_HASH_MANIFEST_COMPRESSION=1</code>); HASHZGAP requests a re-send of any missing chunks.</td></tr>
      <tr><td><code>HASHMISS</code></td><td>peer&rarr;peer</td><td>Request replay of a specific scope+unique_id that the receiver is missing or has truncated.</td></tr>
      <tr><td><code>BULLETIN</code> / <code>BULLMETA</code> / <code>BULLCONT</code></td><td>peer&rarr;peer</td><td>Bulletin replay; META declares total length and CONT carries payload chunks.</td></tr>
      <tr><td><code>MAIL</code> / <code>MAILMETA</code> / <code>MAILCONT</code></td><td>peer&rarr;peer</td><td>Mail replay with chunked payload support.</td></tr>
      <tr><td><code>CHANNEL</code></td><td>peer&rarr;peer</td><td>Channel directory entry replay (name + URL).</td></tr>
      <tr><td><code>CHANNELCOMMENT</code> / <code>CHANNELCOMMENTMETA</code> / <code>CHANNELCOMMENTCONT</code></td><td>peer&rarr;peer</td><td>Per-channel comment replay. The channel manifest key is shortened to a compact <code>~</code>-prefixed hash when the full key would leave fewer than 8 content bytes in the base packet.</td></tr>
      <tr><td><code>PROFILESYNC</code></td><td>peer&rarr;peer</td><td>User profile record replay.</td></tr>
      <tr><td><code>SCORESYNC</code></td><td>peer&rarr;peer</td><td>Hall of Fame score replay.</td></tr>
      <tr><td><code>ZORKSAVE</code></td><td>peer&rarr;peer</td><td>Zork (dfrotz) save replay; supports chunking via the same META/CONT pattern.</td></tr>
      <tr><td><code>DELETE_BULLETIN</code> / <code>DELETE_MAIL</code> / <code>DELETE_ZORKSAVE</code></td><td>peer&rarr;peer</td><td>Tombstone replay so a delete on one node propagates and cannot be silently undone by an older copy.</td></tr>
      <tr><td><code>CANDREQ</code> / <code>CANDRSP</code></td><td>peer&rarr;peer</td><td>Best-candidate resolver: ask all peers for one save key, rank candidates by newest timestamp / largest payload / stable hash, then replay the winner.</td></tr>
    </tbody>
  </table>
  <p class=\"muted\">Pacing knobs (env or Settings &rarr; Sync Settings): <code>BBS_SYNC_TURBO</code>, <code>BBS_SYNC_PAUSE_SECONDS</code>, <code>BBS_HASH_REPAIR_PAUSE_SECONDS</code>, <code>BBS_FULL_SYNC_DELAY_MS</code>, <code>BBS_HASHMISS_REQUEST_TTL_SECONDS</code>.</p>
</div>

<div class=\"card\">
  <h3>BBS Commands (over the mesh)</h3>
  <p class=\"muted\">Send a direct message to the BBS node. Any message returns the main menu. Make selections by sending the bracketed letter or number.</p>
  <table>
    <thead><tr><th>Menu</th><th>Selections</th></tr></thead>
    <tbody>
      <tr><td>Main Menu (💾Bacon BBS💾)</td><td><strong>Q</strong> BBS Menu, <strong>B</strong> BBS Submenu, <strong>U</strong> Utilities Menu, <strong>P</strong> Profile, <strong>X</strong> Exit (configurable via <code>[menu] main_menu_items</code>)</td></tr>
      <tr><td>BBS Menu (📰)</td><td><strong>M</strong> Mail, <strong>B</strong> Bulletins, <strong>C</strong> Channel Directory, <strong>J</strong> JS8Call gateway, <strong>X</strong> Exit</td></tr>
      <tr><td>Utilities Menu (🛠️)</td><td><strong>S</strong> Stats, <strong>F</strong> Fortune, <strong>W</strong> Wall of Shame, <strong>G</strong> Games (Zork + Hall of Fame), <strong>X</strong> Exit</td></tr>
      <tr><td>Mail</td><td>Read inbox, send to a node short name, delete; long messages chunk automatically.</td></tr>
      <tr><td>Bulletins</td><td>Browse by board, read post, post new (Urgent board may require an allow-listed node).</td></tr>
      <tr><td>Channels</td><td>List directory, view a channel, read &amp; add comments.</td></tr>
      <tr><td>Games</td><td>Launch Zork (dfrotz), continue saved game, view Hall of Fame.</td></tr>
      <tr><td>Profile</td><td>View / set short name, long name, callsign, location.</td></tr>
    </tbody>
  </table>
</div>

<div class=\"card\">
  <h3>Web Admin Pages</h3>
  <table>
    <thead><tr><th>Page</th><th>Purpose</th></tr></thead>
    <tbody>
      <tr><td><a href=\"{{ url_for('table_list', table='bulletins') }}\">Bulletins</a></td><td>Moderate bulletins; create new posts; edit/delete; per-board filter; tombstone deletes propagate via sync.</td></tr>
      <tr><td><a href=\"{{ url_for('table_list', table='channels') }}\">Channels</a></td><td>Moderate channel directory entries and per-channel comments.</td></tr>
      <tr><td><a href=\"{{ url_for('clients_summary') }}\">Clients</a></td><td>Connected mesh clients, last-seen, hardware, role, battery, recent activity.</td></tr>
      <tr><td><a href=\"{{ url_for('settings_page') }}\">Settings</a></td><td>Boards, Sync (peers, allow-list, interval, pacing, manual triggers, force resync, save resolver), Diagnostics (peer hash graph, mismatch attempts), Admin credentials.</td></tr>
      <tr><td>Documentation (this page)</td><td>Project reference, runtime sync flowchart, protocol frames, commands, configuration, schema, and live snapshot.</td></tr>
      <tr><td><a href=\"{{ url_for('system_transmissions') }}\">Transmission Stats</a></td><td>Recent <code>sync_transmissions</code> rows: timestamp, frame type, destination, direction, frame size, continuation flag.</td></tr>
      <tr><td><a href=\"{{ url_for('meshtastic_device') }}\">Meshtastic Device</a></td><td>Live snapshot of the local Meshtastic interface (serial/TCP, hardware, role, channels, neighbors).</td></tr>
      <tr><td><a href=\"{{ url_for('mesh_ui_index') }}\" target=\"_blank\">Mesh UI</a></td><td>Embedded build of the official Meshtastic Web client served from <code>meshtastic-web-dist/</code>.</td></tr>
    </tbody>
  </table>
</div>

<div class=\"card\">
  <h3>Configuration</h3>
  <p class=\"muted\">Effective values come from environment variables first (when set) and then from <code>config.ini</code> in the application root. Environment overrides for credentials and pacing remain authoritative until removed.</p>
  <table>
    <thead><tr><th>Section / Variable</th><th>Description</th></tr></thead>
    <tbody>
      <tr><td><code>[interface] type</code></td><td><code>serial</code> or <code>tcp</code>. With serial, set <code>port</code> (e.g. <code>/dev/ttyUSB0</code> or <code>COM3</code>). With TCP, set <code>hostname</code>.</td></tr>
      <tr><td><code>[sync] bbs_nodes</code></td><td>Comma-separated peer node IDs (e.g. <code>!a1b2c3d4,!f5e6d7c8</code>) that receive bulletin, mail, channel, profile, score, and zork-save sync traffic.</td></tr>
      <tr><td><code>[sync] allowed_nodes</code></td><td>Optional allow-list for posting to the Urgent bulletin board. Blank = anyone allowed.</td></tr>
      <tr><td><code>[sync] sync_interval_minutes</code></td><td>How often a full peer sync re-runs (default 5).</td></tr>
      <tr><td><code>[sync] sync_zork_saves</code></td><td>When false, Zork saves stay local to that node and do not appear on peers.</td></tr>
      <tr><td><code>[boards] bulletin_boards</code></td><td>Bulletin board categories (default <code>General,Info,News,Urgent</code>).</td></tr>
      <tr><td><code>[menu] main_menu_items</code> / <code>bbs_menu_items</code> / <code>utilities_menu_items</code></td><td>Comma-separated menu letters (G is auto-injected for the Games entry).</td></tr>
      <tr><td><code>BBS_BULLETIN_BOARDS</code></td><td>Env override for boards.</td></tr>
      <tr><td><code>BBS_WEBGUI_USER</code> / <code>BBS_WEBGUI_PASSWORD</code> / <code>BBS_WEBGUI_SECRET</code></td><td>Web admin credentials and Flask session secret.</td></tr>
      <tr><td><code>BBS_WEBGUI_HOST</code> / <code>BBS_WEBGUI_PORT</code></td><td>Bind address (default <code>127.0.0.1:8081</code>).</td></tr>
      <tr><td><code>BBS_DB_PATH</code> / <code>BBS_CONFIG_PATH</code></td><td>Override default <code>bulletins.db</code> and <code>config.ini</code> locations.</td></tr>
      <tr><td><code>BBS_ZORK_INTERPRETER</code></td><td>Path to the dfrotz binary (typical: <code>/usr/games/dfrotz</code>).</td></tr>
      <tr><td><code>BBS_HASH_MANIFEST_COMPRESSION</code></td><td>Set to <code>1</code> to enable compressed HASHZ/HASHZGAP manifest transport.</td></tr>
      <tr><td><code>BBS_SYNC_TURBO</code></td><td>Aggressive pacing defaults. Only safe on small meshes (2&ndash;3 BBS nodes) &mdash; on busy meshes turbo can <em>worsen</em> convergence by causing packet collisions.</td></tr>
      <tr><td><code>BBS_SYNC_PAUSE_SECONDS</code></td><td>Inter-frame pause after normal sync frames (default 0.75, turbo 0.02).</td></tr>
      <tr><td><code>BBS_HASH_REPAIR_PAUSE_SECONDS</code></td><td>Pause between HASHREQ/HASHREC/HASHMISS frames (default 0.1, turbo 0).</td></tr>
      <tr><td><code>BBS_FULL_SYNC_DELAY_MS</code></td><td>Per-record delay during full database push (default 500, turbo 0).</td></tr>
      <tr><td><code>BBS_HASHMISS_REQUEST_TTL_SECONDS</code></td><td>How long an outstanding HASHMISS suppresses duplicates (default 30s).</td></tr>
    </tbody>
  </table>
</div>

<div class=\"card\">
  <h3>Setup, Install, and Service</h3>
  <ul class=\"muted\">
    <li><strong>Quick setup:</strong> <code>setup.ps1</code> (Windows) or <code>bash setup.sh</code> (Linux/macOS) creates a venv, installs <code>requirements.txt</code>, and seeds <code>config.ini</code> from <code>example_config.ini</code>.</li>
    <li><strong>Run server:</strong> <code>./run_server.sh</code>, <code>run_server.bat</code>, or <code>./venv/bin/python server.py</code>.</li>
    <li><strong>Run web admin:</strong> <code>./run_web_admin.sh</code>, <code>run_web_admin.bat</code>, or <code>python web_admin.py</code> (defaults to <code>127.0.0.1:8081</code>).</li>
    <li><strong>systemd:</strong> <code>bash install_services.sh</code> installs <code>mesh-bbs.service</code> and <code>bacon-web-admin.service</code>. Use <code>--yes --user $USER --dir $HOME/TC2-BaconBS-mesh</code> for non-interactive installs.</li>
    <li><strong>Zork dependency:</strong> <code>sudo apt install frotz</code> and ensure <code>BBS_ZORK_INTERPRETER=/usr/games/dfrotz</code> is exported in the service environment.</li>
    <li><strong>Two-node remote update:</strong> from Windows, <code>scripts/update-two-nodes.ps1</code> uses Posh-SSH and <code>scripts/node-update-config.json</code> to <code>git pull</code> + restart both services on each node. Pass <code>-ResetCredential</code> to re-prompt.</li>
    <li><strong>Smoke test:</strong> <code>python tests/smoke_test.py</code> exercises sync parsing and menu input without a radio.</li>
    <li><strong>Logs:</strong> <code>journalctl -u mesh-bbs.service -u bacon-web-admin.service -f</code>.</li>
  </ul>
  <p class=\"muted\">Recommended Meshtastic device roles for the BBS node: <strong>Client</strong> or <strong>Router_Client</strong>. Other roles have shown sporadic responsiveness loss on long links.</p>
</div>

<div class=\"card\">
  <h3>Database Schema (highlights)</h3>
  <p class=\"muted\">SQLite at <code>BBS_DB_PATH</code> (default <code>bulletins.db</code>). The web admin opens it in WAL mode with a busy timeout; both processes can run concurrently.</p>
  <table>
    <thead><tr><th>Table</th><th>Notes</th></tr></thead>
    <tbody>
      <tr><td><code>bulletins</code></td><td>Per-board posts. <code>unique_id</code> has a uniqueness index; legacy duplicates are deduped on init.</td></tr>
      <tr><td><code>mail</code></td><td>Per-recipient inbox messages with chunked content support (<code>expected_content_length</code>, status flags).</td></tr>
      <tr><td><code>channels</code> / <code>channel_comments</code></td><td>Channel directory and per-channel comment threads. Comments use a compact channel manifest key when needed.</td></tr>
      <tr><td><code>profiles</code></td><td>Per-user profile records synced across nodes.</td></tr>
      <tr><td><code>game_scores</code></td><td>Hall of Fame entries.</td></tr>
      <tr><td><code>zork_saves</code></td><td>Per-user / per-game (currently <code>zork1</code>) interactive-fiction saves.</td></tr>
      <tr><td><code>deleted_sync_tombstones</code></td><td>Records of deletes that have been or should be replayed to peers; older deletes cannot wipe newer records.</td></tr>
      <tr><td><code>peer_sync_state</code></td><td>The most recent SYNCSTATE counts/hashes received from each peer; drives mismatch badges and phase-skip decisions.</td></tr>
      <tr><td><code>sync_transmissions</code></td><td>Rolling log of sent/received sync frames (<code>transmission_time</code>, <code>frame_type</code>, <code>destination_node_id</code>, <code>direction</code>, <code>frame_size_bytes</code>, <code>is_continuation</code>) shown on the Transmission Stats page.</td></tr>
    </tbody>
  </table>
</div>

<div class=\"card\">
  <h3>Live Snapshot</h3>
  <p class=\"muted\">This lightweight summary keeps the page tied to real data without trying to render every post inside the SVG.</p>
  <div class=\"pipeline-flow\">
    <div class=\"pipeline-node\">
      <strong>Boards</strong><br>
      {% if topic_branches %}
        {% for branch in topic_branches[:4] %}
          <span class=\"muted\">{{ branch.board }}: {{ branch.posts|length }} recent posts</span><br>
        {% endfor %}
      {% else %}
        <span class=\"muted\">No bulletin branches yet.</span>
      {% endif %}
    </div>
    <div class=\"pipeline-node\">
      <strong>Channels</strong><br>
      {% if comment_branches %}
        {% for branch in comment_branches[:4] %}
          <span class=\"muted\">{{ branch.channel }}: {{ branch.comments|length }} recent comments</span><br>
        {% endfor %}
      {% else %}
        <span class=\"muted\">No channel comments yet.</span>
      {% endif %}
    </div>
    <div class=\"pipeline-node\">
      <strong>Recent Records</strong><br>
      <span class=\"muted\">Bulletins: {{ recent_bulletins|length }}</span><br>
      <span class=\"muted\">Mail: {{ recent_mail|length }}</span><br>
      <span class=\"muted\">Channels: {{ recent_channels|length }}</span>
    </div>
  </div>
</div>
"""


SETTINGS_CONTENT = """
<div class=\"card\">
  <h2>Settings</h2>
  <p class=\"muted\">Manage boards, sync peers, allowed nodes, and web admin credentials from one place.</p>
</div>

<div class=\"card\" id=\"boards\">
  <h2>Board Settings</h2>
  <p class=\"muted\">Manage bulletin board categories used by create/edit dropdowns.</p>
  {% if env_override %}
  <p class=\"muted\">`BBS_BULLETIN_BOARDS` is set in environment and overrides config file at startup.</p>
  {% endif %}
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#boards\">
    <input type=\"hidden\" name=\"settings_section\" value=\"boards\">
    <label>Boards (comma separated)</label><br>
    <textarea name=\"bulletin_boards\" required>{{ boards_text }}</textarea><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Save Boards</button>
  </form>
</div>

<div class=\"card\" id=\"sync\" style=\"max-width: 800px;\">
  <h2>Sync Settings</h2>
  <p class=\"muted\">Manage BBS peer sync targets, sync pacing, and the node IDs allowed to post to the Urgent board.</p>
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#sync\">
    <input type=\"hidden\" name=\"settings_section\" value=\"sync\">
    <label>Sync BBS Nodes</label><br>
    <textarea name=\"bbs_nodes\" placeholder=\"One per line or comma separated\">{{ bbs_nodes_text }}</textarea><br>
    <p class=\"muted\">These nodes receive bulletin, mail, delete, and channel sync traffic.</p>

    <label>Allowed Urgent Board Nodes</label><br>
    <textarea name=\"allowed_nodes\" placeholder=\"Leave blank to allow all nodes\">{{ allowed_nodes_text }}</textarea><br>
    <p class=\"muted\">If left blank, any node can post to the Urgent board.</p>

    <label>Sync Interval (minutes)</label><br>
    <input type=\"text\" name=\"sync_interval_minutes\" value=\"{{ sync_interval_minutes }}\"><br>
    <p class=\"muted\">How often to re-run full peer sync. For testing, set to 5 minutes.</p>

    <label><input type=\"checkbox\" name=\"sync_zork_saves\" value=\"1\" {% if sync_zork_saves %}checked{% endif %}> Sync game saves across nodes</label><br>
    <p class=\"muted\">When disabled, players can still save locally on this node, but that progress will not appear on other BBS nodes.</p>

    <hr>
    <h3 style=\"margin-top: 16px;\">Transmission Pacing</h3>
    <p class=\"muted\">Raise speed for debugging, or slow it down if you need cleaner over-the-air pacing. Environment variables still override GUI settings while they are present.</p>

    <label><input type=\"checkbox\" name=\"sync_turbo\" value=\"1\" {% if sync_speed_settings.sync_turbo %}checked{% endif %}> Enable turbo pacing</label><br>
    <p class=\"muted\">Turbo uses the smallest normal delays and is useful when you want sync traffic to move as fast as possible.</p>
    <p class=\"muted\" style=\"color:#b35900;\"><strong>⚠ Warning:</strong> Only enable turbo on small meshes (typically 2&ndash;3 BBS nodes on the same channel). The inter-frame pause is what prevents LoRa packet collisions on busy meshes; with 3+ active BBS peers turbo can <em>worsen</em> convergence by causing the very packet loss it tries to outrun.</p>

    <label>Inter-frame Pause (seconds)</label><br>
    <input type=\"text\" name=\"sync_pause_seconds\" value=\"{{ sync_speed_settings.sync_pause_seconds }}\"><br>
    <p class=\"muted\">Delay after normal sync frames such as bulletins, mail, and channels.</p>

    <label>Hash Repair Pause (seconds)</label><br>
    <input type=\"text\" name=\"hash_repair_pause_seconds\" value=\"{{ sync_speed_settings.hash_repair_pause_seconds }}\"><br>
    <p class=\"muted\">Delay between HASHREQ, HASHREC, HASHMISS, and related repair frames.</p>

    <label>Full Sync Startup Delay (milliseconds)</label><br>
    <input type=\"text\" name=\"full_sync_delay_ms\" value=\"{{ sync_speed_settings.full_sync_delay_ms }}\"><br>
    <p class=\"muted\">Pause before a full outbound sync begins after a trigger or interval fires.</p>

    <p class=\"muted\">Effective runtime pacing: turbo={{ sync_runtime_settings.sync_turbo }}, pause={{ sync_runtime_settings.sync_pause_seconds }}s, hash repair={{ sync_runtime_settings.hash_repair_pause_seconds }}s, full sync delay={{ sync_runtime_settings.full_sync_delay_ms }}ms.</p>
    {% if sync_env_override_flags.sync_turbo or sync_env_override_flags.sync_pause_seconds or sync_env_override_flags.hash_repair_pause_seconds or sync_env_override_flags.full_sync_delay_ms %}
    <p class=\"muted\">Environment overrides are active for: {% if sync_env_override_flags.sync_turbo %}sync_turbo {% endif %}{% if sync_env_override_flags.sync_pause_seconds %}sync_pause_seconds {% endif %}{% if sync_env_override_flags.hash_repair_pause_seconds %}hash_repair_pause_seconds {% endif %}{% if sync_env_override_flags.full_sync_delay_ms %}full_sync_delay_ms{% endif %}</p>
    {% endif %}

    {% if runtime_updates_enabled %}
    <p class=\"muted\">Changes are also applied to the active interface immediately.</p>
    {% else %}
    <p class=\"muted\">Changes are saved to config.ini. Restart server.py if the BBS process is running separately from this web GUI.</p>
    {% endif %}

    <button class=\"btn btn-primary\" type=\"submit\">Save Sync Settings</button>
  </form>

  <form method=\"post\" action=\"{{ url_for('settings_page') }}#sync\" style=\"margin-top: 12px;\">
    <input type=\"hidden\" name=\"settings_section\" value=\"manual_sync\">
    <button class=\"btn\" type=\"submit\">Run Manual Sync Now</button>
  </form>

  <form method=\"post\" action=\"{{ url_for('settings_page') }}#sync\" style=\"margin-top: 8px;\">
    <input type=\"hidden\" name=\"settings_section\" value=\"force_check\">
    <button class=\"btn\" type=\"submit\">Force Mismatch Check Now</button>
  </form>

  <hr>
  <h3 style=\"margin-top: 16px;\">Resolve Save by Best Candidate</h3>
  <p class=\"muted\">Ask every configured peer for a specific Zork save candidate, then choose the best result by newest timestamp, then larger payload size, then stable hash tie-break.</p>
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#sync\" style=\"margin-top: 8px; display: flex; gap: 8px; align-items: end; flex-wrap: wrap;\">
    <input type=\"hidden\" name=\"settings_section\" value=\"resolve_zork_save\">
    <div>
      <label>User ID</label><br>
      <input type=\"text\" name=\"resolve_user_id\" placeholder=\"e.g. 1234\" style=\"width:220px;\" required>
    </div>
    <div>
      <label>Game ID</label><br>
      <input type=\"text\" name=\"resolve_game_id\" placeholder=\"e.g. zork1\" value=\"zork1\" style=\"width:220px;\" required>
    </div>
    <button class=\"btn\" type=\"submit\">Resolve Save</button>
  </form>

  <hr>
  <h3 style=\"margin-top: 16px;\">Force Full Resync to Peer</h3>
  <p class=\"muted\">Use this when a peer node was wiped and rebuilt and its game data or other content is not converging via normal hash repair. This clears the in-memory sync cache for that peer and triggers a complete database push to it.</p>
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#sync\" style=\"margin-top: 8px;\">
    <input type=\"hidden\" name=\"settings_section\" value=\"peer_resync\">
    <label>Peer Node ID</label><br>
    <input type=\"text\" name=\"peer_node_id\" placeholder=\"e.g. !a1b2c3d4\" style=\"width:260px;\" required>
    <button class=\"btn\" type=\"submit\" style=\"margin-left: 8px;\">Force Full Resync to Peer</button>
  </form>
</div>

<div class=\"card\" id=\"admin\" style=\"max-width: 600px;\">
  <h2>Admin Credentials</h2>
  <p class=\"muted\">Change the username and password for the web admin interface.</p>
  {% if username_env_override or password_env_override %}
  <p class=\"muted\">Environment variables are overriding stored admin credentials for this running process. GUI changes are saved to config.ini but will be replaced again on restart until those environment variables are removed.</p>
  {% endif %}
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#admin\">
    <input type=\"hidden\" name=\"settings_section\" value=\"admin\">
    <label>Current Password (required)</label><br>
    <input type=\"password\" name=\"current_password\" required><br><br>

    <label>New Username (leave blank to keep current)</label><br>
    <input type=\"text\" name=\"new_username\" placeholder=\"Current: {{ current_username }}\"><br><br>

    <label>New Password (leave blank to keep current)</label><br>
    <input type=\"password\" name=\"new_password\" placeholder=\"Leave blank to keep current\"><br><br>

    <label>Confirm New Password</label><br>
    <input type=\"password\" name=\"confirm_password\" placeholder=\"Confirm new password\"><br><br>

    <button class=\"btn btn-primary\" type=\"submit\">Update Credentials</button>
  </form>
</div>

<div class=\"card\" id=\"danger\" style=\"max-width: 700px; border-color: #6b2323;\">
  <h2>Danger Zone</h2>
  <p class=\"muted\">Wipe all local bulletin, mail, channel, profile, score, save, diagnostics, peer-state, and tombstone data from the SQLite database.</p>
  <p class=\"muted\">Type <strong>WIPE DATABASE</strong> exactly to confirm. If peer sync remains enabled, remote nodes may repopulate data on later sync cycles.</p>
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#danger\" onsubmit=\"return confirm('This will permanently delete all local database content. Continue?');\">
    <input type=\"hidden\" name=\"settings_section\" value=\"wipe_database\">
    <label>Confirmation</label><br>
    <input type=\"text\" name=\"wipe_confirmation\" placeholder=\"Type WIPE DATABASE\" required><br><br>
    <button class=\"btn\" type=\"submit\" style=\"background: #521c1c; border-color: #8d3434; color: #ffd7d7;\">Wipe Database</button>
  </form>
</div>

<div class=\"card\" id=\"diagnostics\" style=\"max-width: 900px;\">
  <h2>Diagnostics</h2>
  <p class=\"muted\">Quick runtime and BBS status details for troubleshooting.</p>

  <h3>Runtime</h3>
  <p><strong>Runtime source:</strong> {{ diagnostics.runtime_source }}</p>
  <p><strong>Snapshot updated at:</strong> {{ diagnostics.snapshot_updated_at }}</p>
  <p><strong>Interface attached:</strong> {{ diagnostics.interface_attached }}</p>
  <p><strong>Interface type:</strong> {{ diagnostics.interface_type }}</p>
  <p><strong>Known mesh nodes:</strong> {{ diagnostics.mesh_node_count }}</p>
  <p><strong>Local node:</strong> {{ diagnostics.local_node_id }}</p>
  <p><strong>Local short name:</strong> {{ diagnostics.local_short_name }}</p>
  <p><strong>Local long name:</strong> {{ diagnostics.local_long_name }}</p>

  <h3>BBS</h3>
  <p><strong>Configured sync peers:</strong> {{ diagnostics.bbs_nodes_count }}{% if diagnostics.bbs_nodes_text %} ({{ diagnostics.bbs_nodes_text }}){% endif %}</p>
  <p><strong>Configured urgent allow-list:</strong> {{ diagnostics.allowed_nodes_count }}{% if diagnostics.allowed_nodes_text %} ({{ diagnostics.allowed_nodes_text }}){% endif %}</p>
  <p><strong>Bulletin boards:</strong> {{ diagnostics.board_count }} ({{ diagnostics.board_list }})</p>
  <p><strong>Outbound peer sync:</strong>
  {% if diagnostics.sync_in_progress == "Yes" %}
    Sending &mdash; {{ diagnostics.sync_progress_percent }}% ({{ diagnostics.sync_completed_items }}/{{ diagnostics.sync_total_items }} items) to {{ diagnostics.sync_target_nodes_text }}
  {% elif diagnostics.sync_current_phase == "never_run" %}
    Not yet run since startup
  {% else %}
    Complete &mdash; sent {{ diagnostics.sync_total_items }} item(s) to {{ diagnostics.sync_target_nodes_text }}
  {% endif %}
  </p>
  <p><strong>Sync phase:</strong> {{ diagnostics.sync_current_phase }} &nbsp;|&nbsp; <strong>Last update:</strong> {{ diagnostics.sync_last_updated_at }}</p>
  <p><strong>Last sync result:</strong> {{ diagnostics.sync_last_result }}</p>
  {% if diagnostics.sync_in_progress == "Yes" %}
  <p class=\"muted\"><strong>Notice:</strong> Outbound sync is running. Some historical posts may not be available on peers yet.</p>
  {% endif %}
  <p><strong>Peer consistency:</strong> {{ diagnostics.peer_sync_status }}</p>
  <p><strong>Mismatch re-sync attempts:</strong> {{ diagnostics.mismatch_retry_summary }}</p>
  <p><strong>Peer-advertised record counts:</strong></p>
  <pre style=\"white-space: pre-wrap; margin-top: 4px;\">{{ diagnostics.peer_sync_counts }}</pre>
  <p><strong>Per-scope mismatch reasons:</strong></p>
  <pre style=\"white-space: pre-wrap; margin-top: 4px;\">{{ diagnostics.peer_scope_mismatches }}</pre>
  <h3>Zork Save Mismatch Focus</h3>
  <pre style=\"white-space: pre-wrap; margin-top: 4px;\">{{ diagnostics.zork_save_peer_mismatches }}</pre>
  <h3>Zork Save Tombstones</h3>
  <pre style=\"white-space: pre-wrap; margin-top: 4px;\">{{ diagnostics.zork_save_tombstones }}</pre>
  <h3>Best-Candidate Resolver</h3>
  <pre style=\"white-space: pre-wrap; margin-top: 4px;\">{{ diagnostics.zork_save_candidate_resolution }}</pre>
  <h3>Peer Hash Graph</h3>
  {% if diagnostics.peer_hash_graph %}
  <div class=\"peer-graph-grid\">
    {% for peer in diagnostics.peer_hash_graph %}
    <div class=\"peer-graph-card\">
      <h4>{{ peer.peer_node_id }}</h4>
      <div class=\"muted\">Reported at {{ peer.reported_at }}</div>
      {% for scope in peer.scopes %}
      <div class=\"scope-bar-row\">
        <div class=\"scope-bar-header\">
          <span><strong>{{ scope.label }}</strong></span>
          <span class=\"{% if scope.hash_match and scope.count_match %}scope-status-ok{% else %}scope-status-bad{% endif %}\">{% if scope.hash_match and scope.count_match %}aligned{% else %}mismatch{% endif %}</span>
        </div>
        <div class=\"scope-bar-track\">
          <div class=\"scope-bar-local\" style=\"width: {{ scope.local_width }}%\" title=\"Local {{ scope.local_count }}\"></div>
          <div class=\"scope-bar-peer\" style=\"width: {{ scope.peer_width }}%\" title=\"Peer {{ scope.peer_count }}\"></div>
        </div>
        <div class=\"scope-bar-meta\">
          <span>Local {{ scope.local_count }}</span>
          <span>Peer {{ scope.peer_count }}</span>
          <span>{% if scope.hash_match %}hash ok{% else %}hash differs{% endif %}</span>
        </div>
      </div>
      {% endfor %}
    </div>
    {% endfor %}
  </div>
  <p class=\"muted\">Blue bars are local counts. Amber bars are peer-reported counts. A scope can keep the same count and still have a hash mismatch.</p>
  {% else %}
  <p class=\"muted\">No peer hash data available yet.</p>
  {% endif %}
  <p class=\"muted\">Outbound progress can be 100% while peer consistency is mismatched. Peer counts above indicate missing records between nodes.</p>
  {% if diagnostics.mismatch_retry_details %}
  <pre style=\"white-space: pre-wrap; margin-top: 4px;\">{{ diagnostics.mismatch_retry_details }}</pre>
  {% endif %}

  <h3>Database</h3>
  <p><strong>App version:</strong> {{ diagnostics.app_version }}</p>
  <p><strong>Path:</strong> <code>{{ diagnostics.db_path }}</code></p>
  <p><strong>Database size:</strong> {{ diagnostics.db_size }} &nbsp;|&nbsp; <strong>WAL:</strong> {{ diagnostics.wal_size }} &nbsp;|&nbsp; <strong>Total on disk:</strong> {{ diagnostics.db_total_size }}</p>
  <p><strong>API mailbox:</strong> {{ diagnostics.api_mailbox_count }} stored ({{ diagnostics.api_mailbox_pending }} pending delivery)</p>
  <p><strong>Bulletins:</strong> {{ diagnostics.bulletins_count }}</p>
  <p><strong>Mail:</strong> {{ diagnostics.mail_count }}</p>
  <p><strong>Channels:</strong> {{ diagnostics.channels_count }}</p>
  <p><strong>Zork saves:</strong> {{ diagnostics.zork_saves_count }}</p>
  <p><strong>Game scores:</strong> {{ diagnostics.game_scores_count }}</p>
  <p><strong>Connection events:</strong> {{ diagnostics.connection_events_count }}</p>
  <p><strong>Last connection event:</strong> {{ diagnostics.last_connection_event }}</p>
  {% if diagnostics.error %}
  <p class=\"muted\">Diagnostics note: {{ diagnostics.error }}</p>
  {% endif %}
</div>
"""


ADMIN_SETTINGS_CONTENT = """
<div class=\"card\" style=\"max-width: 600px;\">
  <h2>Admin Credentials</h2>
  <p class=\"muted\">Change the username and password for the web admin interface.</p>
  {% if username_env_override or password_env_override %}
  <p class=\"muted\">Environment variables are overriding stored admin credentials for this running process. GUI changes are saved to config.ini but will be replaced again on restart until those environment variables are removed.</p>
  {% endif %}
  <form method=\"post\">
    <label>Current Password (required)</label><br>
    <input type=\"password\" name=\"current_password\" required><br><br>

    <label>New Username (leave blank to keep current)</label><br>
    <input type=\"text\" name=\"new_username\" placeholder=\"Current: {{ current_username }}\"><br><br>

    <label>New Password (leave blank to keep current)</label><br>
    <input type=\"password\" name=\"new_password\" placeholder=\"Leave blank to keep current\"><br><br>

    <label>Confirm New Password</label><br>
    <input type=\"password\" name=\"confirm_password\" placeholder=\"Confirm new password\"><br><br>

    <button class=\"btn btn-primary\" type=\"submit\">Update Credentials</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='bulletins') }}\">Back</a>
  </form>
</div>
"""


CHANNEL_COMMENTS_CONTENT = """
<div class=\"card\">
  <h2>Comments for {{ channel_name }} (Post #{{ channel_id }})</h2>
  <p class=\"muted\">URL/PSK: {{ channel_url }}</p>
</div>

<div class=\"card\">
  <h3>Add Comment</h3>
  <form method=\"post\">
    <label>Sender Short Name</label><br>
    <input type=\"text\" name=\"sender_short_name\" required><br><br>
    <label>Comment</label><br>
    <textarea name=\"content\" required></textarea><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Add Comment</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='channels') }}\">Back to Channels</a>
  </form>
</div>

<div class=\"card\">
  <h3>Existing Comments</h3>
  <table>
    <thead>
      <tr>
        <th>id</th>
        <th>sender_short_name</th>
        <th>date</th>
        <th>content</th>
        <th>sync_status</th>
        <th>source_node_id</th>
        <th>source_timestamp</th>
        <th>received_at</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for comment in comments %}
      <tr{% if comment['content_complete'] == 0 %} style="background:#fff1f1"{% endif %}>
        <td>{{ comment['id'] }}</td>
        <td>{{ comment['sender_short_name'] }}</td>
        <td>{{ comment['date'] }}</td>
        <td>{{ comment['content'] }}</td>
        <td>
          {% if comment['content_complete'] == 0 %}
            <strong style="color:#b91c1c;">{{ comment['sync_status'] }}</strong>
            <form method="post" action="{{ url_for('resolve_record') }}" class="inline" style="display:inline;margin-left:4px">
              <input type="hidden" name="scope" value="channels">
              <input type="hidden" name="key" value="comment:{{ comment['unique_id'] }}">
              <input type="hidden" name="redirect_to" value="{{ request.path }}">
              <button type="submit" class="btn btn-small">Resolve</button>
            </form>
          {% else %}
            <span class="muted">{{ comment['sync_status'] }}</span>
          {% endif %}
        </td>
        <td>{{ comment['source_node_id'] or '' }}</td>
        <td>{{ comment['source_timestamp'] or '' }}</td>
        <td>{{ comment['received_at'] or '' }}</td>
        <td>
          <form method=\"post\" action=\"{{ url_for('channel_comment_delete', channel_id=channel_id, comment_id=comment['id']) }}\" class=\"inline\" onsubmit=\"return confirm('Delete this comment?');\">
            <button type=\"submit\" class=\"btn btn-danger\">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
      {% if not comments %}
      <tr>
        <td colspan=\"9\" class=\"muted\">No comments yet.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>
</div>
"""


CLIENT_PROFILE_CONTENT = """
<div class="card">
  <h2>Client Profile</h2>
  {% if profile %}
  <table>
    <tbody>
      <tr><th style="width:160px">Short name</th><td>{{ profile['short_name'] or '—' }}</td></tr>
      <tr><th>Long name</th><td>{{ profile['long_name'] or '—' }}</td></tr>
      <tr><th>Node ID</th><td>{{ profile['user_id'] }}</td></tr>
      <tr><th>First seen</th><td>{{ profile['first_seen'] }}</td></tr>
      <tr><th>Last seen</th><td>{{ profile['last_seen'] }}</td></tr>
      <tr><th>Messages sent</th><td>{{ profile['messages_sent'] }}</td></tr>
      <tr><th>Bio</th><td>{{ profile['bio'] or '—' }}</td></tr>
    </tbody>
  </table>
  {% else %}
  <p class="muted">No profile on record for node <strong>{{ node_id }}</strong>.</p>
  {% endif %}
  <p style="margin-top:1rem"><a href="{{ url_for('clients_summary') }}">&larr; Back to Clients</a></p>
</div>
"""


CLIENTS_CONTENT = """
<div class=\"card\">
  <h2>Client Post Counts</h2>
  <p class=\"muted\">Unique clients and how many bulletin posts each has created.</p>
  <p class=\"muted\">Clients: {{ rows|length }} | Total bulletin posts: {{ total_posts }}</p>
  <table>
    <thead>
      <tr>
        <th>sender_short_name</th>
        <th>post_count</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        <td>{{ row['sender_short_name'] }}</td>
        <td>{{ row['post_count'] }}</td>
        <td>
          {% if row['sender_node_id'] %}
          <a href="{{ url_for('client_profile', node_id=row['sender_node_id']) }}" class="btn btn-sm">View Profile</a>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      {% if not rows %}
      <tr>
        <td colspan=\"3\" class=\"muted\">No client posts found.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>
</div>

<div class=\"card\">
  <h3>Live Log</h3>
  <p class=\"muted\">Terminal-style stream of inbound mesh activity. Auto-refreshes every 2 seconds.</p>
  <div class=\"terminal-controls\">
    <span class=\"muted\" style=\"font-size:11px;font-family:Consolas,monospace;\">Filter:</span>
    <button class=\"terminal-btn active\" data-filter=\"all\">All <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"rx\">RX <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"sync\">SYNC <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"direct\">DIRECT <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"drop\">DROP <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"log\">LOG <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"warn\">WARN <span class=\"filter-count\">0</span></button>
    <button class=\"terminal-btn\" data-filter=\"error\">ERROR <span class=\"filter-count\">0</span></button>
    <span style=\"flex:1\"></span>
    <button class=\"terminal-btn btn-pause\" id=\"btn-pause\">Pause</button>
    <button class=\"terminal-btn btn-clear\" id=\"btn-clear\">Clear</button>
  </div>
  <div id=\"connection-terminal\" class=\"terminal-window\"></div>
</div>

<script>
  (function () {
    const terminal = document.getElementById('connection-terminal');
    if (!terminal) return;

    let lastId = {{ last_event_id }};
    let paused = false;
    let currentFilter = 'all';

    function refreshFilterCounts() {
      const counts = {
        all: 0,
        rx: 0,
        sync: 0,
        direct: 0,
        drop: 0,
        log: 0,
        warn: 0,
        error: 0
      };
      terminal.querySelectorAll('.terminal-line').forEach(function(line) {
        counts.all += 1;
        const type = (line.dataset.type || '').toLowerCase();
        if (Object.prototype.hasOwnProperty.call(counts, type)) {
          counts[type] += 1;
        }
      });
      document.querySelectorAll('.terminal-btn[data-filter]').forEach(function(btn) {
        const badge = btn.querySelector('.filter-count');
        if (!badge) return;
        const key = btn.dataset.filter || 'all';
        badge.textContent = String(counts[key] || 0);
      });
    }

    function setFilter(btn, f) {
      currentFilter = f;
      document.querySelectorAll('.terminal-btn[data-filter]').forEach(function(b) {
        b.classList.toggle('active', b.dataset.filter === f);
      });
      terminal.querySelectorAll('.terminal-line').forEach(function(line) {
        line.style.display = (f === 'all' || line.dataset.type === f) ? '' : 'none';
      });
    }

    function togglePause() {
      paused = !paused;
      const btn = document.getElementById('btn-pause');
      btn.textContent = paused ? 'Resume' : 'Pause';
      btn.classList.toggle('active', paused);
      if (!paused) terminal.scrollTop = terminal.scrollHeight;
    }

    function clearTerminal() {
      terminal.innerHTML = '';
      refreshFilterCounts();
    }

    document.querySelectorAll('.terminal-btn[data-filter]').forEach(function(btn) {
      btn.addEventListener('click', function() {
        setFilter(btn, btn.dataset.filter);
      });
    });
    const pauseBtn = document.getElementById('btn-pause');
    if (pauseBtn) {
      pauseBtn.addEventListener('click', togglePause);
    }
    const clearBtn = document.getElementById('btn-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', clearTerminal);
    }

    function appendLine(evt) {
      const line = document.createElement('div');
      line.className = 'terminal-line';
      line.dataset.type = evt.display_type;
      if (currentFilter !== 'all' && evt.display_type !== currentFilter) {
        line.style.display = 'none';
      }
      const sender = evt.source_label || evt.sender_short_name || evt.sender_node_id || evt.sender_num || '?';
      const to = evt.to_id || 'group';
      line.innerHTML =
        '<span class=\"terminal-time\">[' + evt.event_time + ']</span> ' +
        '<span class="terminal-type">' + evt.display_label + '</span> ' +
        sender + ' -> ' + to + ' :: ' + evt.event_text;
      terminal.appendChild(line);
      refreshFilterCounts();
      if (!paused) terminal.scrollTop = terminal.scrollHeight;
    }

    {% for evt in connection_events %}
    appendLine({
      event_time: {{ evt['event_time']|tojson }},
      message_type: {{ evt['message_type']|tojson }},
      display_type: {{ evt['display_type']|tojson }},
      display_label: {{ evt['display_label']|tojson }},
      source_label: {{ evt['source_label']|tojson }},
      sender_short_name: {{ evt['sender_short_name']|tojson }},
      sender_node_id: {{ evt['sender_node_id']|tojson }},
      sender_num: {{ evt['sender_num']|tojson }},
      to_id: {{ evt['to_id']|tojson }},
      event_text: {{ evt['event_text']|tojson }}
    });
    {% endfor %}

    async function poll() {
      if (paused) return;
      try {
        const resp = await fetch('/api/connection-events?since_id=' + encodeURIComponent(lastId), {
          headers: { 'Accept': 'application/json' }
        });
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.events || !data.events.length) return;
        data.events.forEach(function(evt) {
          appendLine(evt);
          lastId = evt.id;
        });
      } catch (e) {
        // Keep polling even if one request fails.
      }
    }

    setInterval(poll, 2000);
  })();
</script>
"""


TRANSMISSION_DASHBOARD_CONTENT = """
<div class="card" id="sync-progress-card">
  <h2>Current Sync Tasks</h2>
  <p class="muted">Live view of per-peer record gaps and active content delivery sequences. Refreshes every 10 seconds.</p>

  <div id="sync-progress-status" style="font-size:0.82em;color:var(--muted);margin-bottom:10px;"></div>

  <div id="sync-progress-peers"></div>

  <div id="sync-progress-items">
    <p class="muted" style="font-size:0.9em;">Loading active repair items&hellip;</p>
  </div>
</div>

<script>
(function() {
  var SCOPE_LABELS = {
    bulletins:   'Bulletins',
    mail:        'Mail',
    channels:    'Channels',
    zork_saves:  'Zork Saves',
    profiles:    'Profiles',
    game_scores: 'Game Scores',
  };
  var SCOPE_ORDER = ['bulletins', 'mail', 'channels', 'zork_saves', 'profiles', 'game_scores'];
  var FRAME_COLOR = {
    BULLETIN:          '#2a9d8f',
    BULLETINCONT:      '#52b3a8',
    MAIL:              '#457b9d',
    MAILCONT:          '#6a9dbd',
    CHANNELCOMMENTCONT:'#e9c46a',
  };

  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function fmtTime(iso) {
    if (!iso) return '';
    var d = new Date(iso.replace(' ', 'T'));
    if (isNaN(d)) return iso;
    return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  }

  function relativeAge(iso) {
    if (!iso) return '';
    var d = new Date(iso.replace(' ', 'T').replace(/Z$/, '') + (iso.includes('Z') || iso.includes('+') ? '' : 'Z'));
    if (isNaN(d)) return iso;
    var secs = Math.round((Date.now() - d.getTime()) / 1000);
    if (secs < 5)   return 'just now';
    if (secs < 60)  return secs + 's ago';
    if (secs < 3600) return Math.floor(secs/60) + 'm ago';
    return Math.floor(secs/3600) + 'h ago';
  }

  function renderPeers(data) {
    var peers = data.peers || [];
    var local = data.local_counts || {};
    if (!peers.length) {
      document.getElementById('sync-progress-peers').innerHTML =
        '<p class="muted" style="font-size:0.9em;">No peer sync state recorded yet.</p>';
      return;
    }
    var html = '';
    peers.forEach(function(peer) {
      var age = relativeAge(peer.reported_at);
      var hasGaps = peer.gaps && peer.gaps.length > 0;
      var headerColor = hasGaps ? '#b45309' : '#166534';
      var headerBg = hasGaps ? 'rgba(251,191,36,0.08)' : 'rgba(22,163,74,0.07)';
      html += '<div style="margin-bottom:18px;border:1px solid var(--card-border);border-radius:8px;overflow:hidden;">';
      html += '<div style="padding:8px 14px;background:' + headerBg + ';border-bottom:1px solid var(--card-border);display:flex;align-items:center;gap:12px;">';
      html += '<span style="font-family:monospace;font-weight:700;color:' + headerColor + ';">' + esc(peer.peer_node_id) + '</span>';
      html += '<span style="color:var(--muted);font-size:0.82em;">reported ' + esc(age) + '</span>';
      if (hasGaps)
        html += '<span style="margin-left:auto;font-size:0.8em;color:#b45309;font-weight:600;">' + peer.gaps.length + ' scope(s) out of sync</span>';
      else
        html += '<span style="margin-left:auto;font-size:0.8em;color:#166534;font-weight:600;">&#10003; All scopes in sync</span>';
      html += '</div>';
      html += '<table style="width:100%;border-collapse:collapse;font-size:0.88em;">';
      html += '<thead><tr style="background:var(--table-header-bg);border-bottom:1px solid var(--table-border);">';
      html += '<th style="padding:6px 10px;text-align:left;">Scope</th>';
      html += '<th style="padding:6px 10px;text-align:right;">Local</th>';
      html += '<th style="padding:6px 10px;text-align:right;">Peer</th>';
      html += '<th style="padding:6px 10px;">Status</th>';
      html += '</tr></thead><tbody>';
      SCOPE_ORDER.forEach(function(scope) {
        var localVal = parseInt(local[scope] || 0);
        var peerVal  = parseInt((peer.counts || {})[scope] || 0);
        var delta = localVal - peerVal;
        var statusHtml, rowBg;
        if (scope === 'zork_saves' && peer.skips_zork_saves) {
          // The peer said so explicitly. Showing a delta here reads as a
          // backlog that never clears, when nothing is meant to be sent.
          statusHtml = '<span style="color:var(--text-muted);">&mdash; Peer does not sync saves</span>';
          rowBg = '';
        } else if (delta > 0) {
          statusHtml = '<span style="color:#b45309;">&darr; Peer missing ' + delta + '</span>';
          rowBg = 'rgba(251,191,36,0.06)';
        } else if (delta < 0) {
          statusHtml = '<span style="color:#1d4ed8;">&uarr; We are behind by ' + Math.abs(delta) + '</span>';
          rowBg = 'rgba(29,78,216,0.06)';
        } else {
          statusHtml = '<span style="color:#166534;">&#10003; In sync</span>';
          rowBg = '';
        }
        html += '<tr style="border-bottom:1px solid var(--table-border);' + (rowBg ? 'background:'+rowBg+';' : '') + '">';
        html += '<td style="padding:5px 10px;">' + esc(SCOPE_LABELS[scope] || scope) + '</td>';
        html += '<td style="padding:5px 10px;text-align:right;font-family:monospace;">' + localVal + '</td>';
        html += '<td style="padding:5px 10px;text-align:right;font-family:monospace;">' + peerVal + '</td>';
        html += '<td style="padding:5px 10px;">' + statusHtml + '</td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    });
    document.getElementById('sync-progress-peers').innerHTML = html;
  }

  function renderActiveItems(data) {
    var items = data.active_items || [];
    var container = document.getElementById('sync-progress-items');
    if (!items.length) {
      container.innerHTML = '<p class="muted" style="font-size:0.9em;margin-top:6px;">No active repair items in the last 30 minutes.</p>';
      return;
    }
    var html = '<h4 style="margin:14px 0 6px 0;">Active Repair Items <span style="font-weight:normal;font-size:0.82em;color:var(--muted);">(last 30 min)</span></h4>';
    items.forEach(function(item) {
      var frames = item.sent_frames || [];
      // Deduplicate by (type+offset), keeping last seen timestamp
      var frameMap = {};
      frames.forEach(function(f) {
        var key = f.type + '|' + (f.offset !== null && f.offset !== undefined ? f.offset : 'base');
        if (!frameMap[key] || f.sent_at > frameMap[key].sent_at) frameMap[key] = f;
      });
      var dedupedFrames = Object.values(frameMap).sort(function(a, b) {
        var oa = (a.offset !== null && a.offset !== undefined) ? a.offset : -1;
        var ob = (b.offset !== null && b.offset !== undefined) ? b.offset : -1;
        return oa - ob;
      });

      var scopeColor = {bulletins:'#2a9d8f', mail:'#457b9d', channels:'#e9c46a', zork_saves:'#e63946', profiles:'#6d28d9', game_scores:'#dc2626'}[item.scope] || '#888';

      html += '<div style="margin-bottom:14px;border:1px solid var(--card-border);border-radius:8px;overflow:hidden;">';
      html += '<div style="padding:8px 14px;background:rgba(0,0,0,0.03);border-bottom:1px solid var(--card-border);display:flex;flex-wrap:wrap;align-items:center;gap:10px;">';
      html += '<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:' + scopeColor + ';color:#fff;font-size:0.78em;font-weight:700;text-transform:uppercase;">' + esc(item.scope) + '</span>';
      html += '<span style="font-weight:600;font-size:0.95em;">' + esc(item.subject) + '</span>';
      html += '<span style="font-family:monospace;font-size:0.78em;color:var(--muted);">' + esc(item.unique_id) + '</span>';
      html += '<span style="margin-left:auto;font-size:0.82em;color:var(--muted);">peer: <strong>' + esc(item.peer) + '</strong></span>';
      html += '<span style="font-size:0.82em;color:var(--muted);">' + item.request_count + ' request(s), last ' + esc(relativeAge(item.last_hashmiss_at)) + '</span>';
      html += '</div>';

      if (dedupedFrames.length) {
        html += '<div style="padding:10px 14px;">';
        html += '<div style="font-size:0.8em;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.04em;">Recent outbound frame sequence</div>';
        html += '<div style="font-family:monospace;font-size:0.88em;display:flex;flex-direction:column;gap:4px;">';
        dedupedFrames.forEach(function(f) {
          var color = FRAME_COLOR[f.type] || '#888';
          var label;
          if (f.type === 'BULLETIN' || f.type === 'MAIL' || f.type === 'CHANNEL') {
            label = f.type + ' (base frame, offset 0)';
          } else if (f.offset !== null && f.offset !== undefined) {
            label = f.type + '@' + f.offset;
          } else {
            label = f.type;
          }
          html += '<div style="display:flex;align-items:center;gap:10px;">';
          html += '<span style="display:inline-block;min-width:8px;height:8px;background:' + color + ';border-radius:50%;flex-shrink:0;"></span>';
          html += '<span style="color:' + color + ';font-weight:600;min-width:240px;">' + esc(label) + '</span>';
          html += '<span style="color:var(--muted);font-size:0.9em;">sent ' + esc(fmtTime(f.sent_at)) + '</span>';
          html += '</div>';
        });
        html += '</div></div>';
      } else {
        html += '<div style="padding:8px 14px;font-size:0.88em;color:var(--muted);">No outbound frames recorded for this item in this window.</div>';
      }
      html += '</div>';
    });
    container.innerHTML = html;
  }

  function refresh() {
    fetch('/api/sync/progress?lookback_seconds=1800', { headers: { 'Accept': 'application/json' } })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        renderPeers(data);
        renderActiveItems(data);
        document.getElementById('sync-progress-status').textContent =
          'Last updated: ' + new Date().toLocaleTimeString();
      })
      .catch(function() {
        document.getElementById('sync-progress-status').textContent = 'Update failed — retrying\u2026';
      });
  }

  refresh();
  setInterval(refresh, 10000);
})();
</script>

<div class="card" id="sync-sessions-card">
  <h2>Sync Session History</h2>
  <p class="muted">Each row is one inferred sync burst — a cluster of non-SYNCSTATE frames to the same peer with no gap longer than 90 seconds. Duration, byte totals, and effective throughput are derived from the live transmission log.</p>
  <div id="sync-sessions-active" style="margin-bottom:14px;"></div>
  <h3 style="margin-bottom:4px;">Recent (last 24 hours)</h3>
  <div id="sync-sessions-table"><p class="muted" style="font-size:0.9em;">Loading&hellip;</p></div>
  <div id="sync-sessions-status" style="font-size:0.78em;color:var(--muted);margin-top:8px;"></div>

  <h3 style="margin-top:22px;margin-bottom:4px;">Long-term History</h3>
  <p class="muted" style="font-size:0.85em;">Persisted to the database, so completed sync sessions are preserved even after the raw transmission log rolls over. Use this to track how full-sync duration changes over time.</p>
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
    <label for="sync-history-window" style="font-size:0.85em;">Window:</label>
    <select id="sync-history-window" style="font-size:0.85em;">
      <option value="86400">Last 24 hours</option>
      <option value="604800" selected>Last 7 days</option>
      <option value="2592000">Last 30 days</option>
      <option value="7776000">Last 90 days</option>
      <option value="31536000">Last year</option>
    </select>
    <span id="sync-history-summary" style="font-size:0.82em;color:var(--muted);"></span>
  </div>
  <div id="sync-history-table"><p class="muted" style="font-size:0.9em;">Loading&hellip;</p></div>
</div>

<script>
(function() {
  function fmtBytes(b) {
    if (b == null) return '—';
    b = parseInt(b) || 0;
    if (b < 1024)        return b + ' B';
    if (b < 1048576)     return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(2) + ' MB';
  }
  function fmtDur(sec) {
    if (sec == null) return '—';
    sec = parseFloat(sec);
    if (sec < 60) return sec.toFixed(1) + 's';
    var m = Math.floor(sec / 60), s = Math.round(sec % 60);
    if (sec < 3600) return m + 'm ' + s + 's';
    var h = Math.floor(m / 60); m = m % 60;
    return h + 'h ' + m + 'm';
  }
  function fmtSpeed(bps) {
    if (bps == null) return '—';
    bps = parseFloat(bps);
    if (bps < 1024)    return bps.toFixed(1) + ' B/s';
    if (bps < 1048576) return (bps / 1024).toFixed(2) + ' KB/s';
    return (bps / 1048576).toFixed(3) + ' MB/s';
  }
  function isoToDate(s) {
    if (!s) return null;
    return new Date(s.replace(' ', 'T').replace(/Z?$/, 'Z'));
  }
  function relTime(s) {
    var d = isoToDate(s);
    if (!d || isNaN(d)) return '';
    var secs = Math.round((Date.now() - d.getTime()) / 1000);
    if (secs < 5)    return 'just now';
    if (secs < 60)   return secs + 's ago';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    return Math.floor(secs / 3600) + 'h ago';
  }
  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  var _activeSessions = [];
  var _timerInterval = null;

  function renderActive() {
    var el = document.getElementById('sync-sessions-active');
    if (!_activeSessions.length) { el.innerHTML = ''; return; }
    var now = Date.now();
    var html = '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:4px;">';
    _activeSessions.forEach(function(s) {
      var startMs = (isoToDate(s.started_at) || new Date()).getTime();
      var elapsed = ((now - startMs) / 1000);
      html += '<div style="border:2px solid #2a9d8f;border-radius:8px;padding:10px 14px;background:rgba(42,157,143,0.06);min-width:220px;">';
      html += '<div style="font-size:0.75em;text-transform:uppercase;letter-spacing:0.06em;color:#2a9d8f;font-weight:700;margin-bottom:4px;">&#9679; Syncing</div>';
      html += '<div style="font-family:monospace;font-size:0.9em;margin-bottom:4px;">' + esc(s.peer_node_id) + '</div>';
      html += '<div style="font-size:1.5em;font-weight:700;letter-spacing:0.04em;" class="session-elapsed" data-start="' + (startMs) + '">' + fmtDur(elapsed) + '</div>';
      html += '<div style="font-size:0.82em;color:var(--muted);margin-top:4px;">' + fmtBytes(s.total_bytes) + ' &middot; ' + s.frame_count + ' frames</div>';
      html += '</div>';
    });
    html += '</div>';
    el.innerHTML = html;
  }

  function tickElapsed() {
    var now = Date.now();
    document.querySelectorAll('.session-elapsed').forEach(function(el) {
      var start = parseInt(el.dataset.start) || now;
      el.textContent = fmtDur((now - start) / 1000);
    });
  }

  function renderTable(sessions) {
    var el = document.getElementById('sync-sessions-table');
    if (!sessions || !sessions.length) {
      el.innerHTML = '<p class="muted" style="font-size:0.9em;">No completed sync sessions recorded yet in the last 24 hours.</p>';
      return;
    }
    var html = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.88em;">';
    html += '<thead><tr style="background:var(--table-header-bg);border-bottom:2px solid var(--table-border);">';
    html += '<th style="padding:7px 8px;text-align:left;">Peer</th>';
    html += '<th style="padding:7px 8px;text-align:left;">Ended</th>';
    html += '<th style="padding:7px 8px;text-align:right;">Duration</th>';
    html += '<th style="padding:7px 8px;text-align:right;">Sent</th>';
    html += '<th style="padding:7px 8px;text-align:right;">Received</th>';
    html += '<th style="padding:7px 8px;text-align:right;">Total</th>';
    html += '<th style="padding:7px 8px;text-align:right;">Speed</th>';
    html += '<th style="padding:7px 8px;text-align:right;">Frames</th>';
    html += '</tr></thead><tbody>';
    sessions.forEach(function(s, i) {
      var rowBg = i % 2 === 0 ? '' : 'background:var(--table-alt-bg,rgba(0,0,0,0.02));';
      html += '<tr style="border-bottom:1px solid var(--table-border);' + rowBg + '">';
      html += '<td style="padding:6px 8px;font-family:monospace;font-size:0.85em;">' + esc(s.peer_node_id) + '</td>';
      html += '<td style="padding:6px 8px;font-size:0.82em;color:var(--muted);" title="' + esc(s.ended_at) + '">' + esc(relTime(s.ended_at)) + '</td>';
      html += '<td style="padding:6px 8px;text-align:right;font-weight:600;">' + fmtDur(s.duration_seconds) + '</td>';
      html += '<td style="padding:6px 8px;text-align:right;">' + fmtBytes(s.bytes_tx) + '</td>';
      html += '<td style="padding:6px 8px;text-align:right;">' + fmtBytes(s.bytes_rx) + '</td>';
      html += '<td style="padding:6px 8px;text-align:right;font-weight:600;">' + fmtBytes(s.total_bytes) + '</td>';
      html += '<td style="padding:6px 8px;text-align:right;color:#2a9d8f;">' + fmtSpeed(s.bps) + '</td>';
      html += '<td style="padding:6px 8px;text-align:right;color:var(--muted);">' + esc(s.frame_count) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }

  function refresh() {
    fetch('/api/sync/sessions', { headers: { 'Accept': 'application/json' } })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        _activeSessions = data.active || [];
        renderActive();
        renderTable(data.sessions || []);
        document.getElementById('sync-sessions-status').textContent =
          'Updated ' + new Date().toLocaleTimeString();
      })
      .catch(function() {
        document.getElementById('sync-sessions-status').textContent = 'Refresh failed — retrying\u2026';
      });
  }

  refresh();
  setInterval(refresh, 30000);
  if (_timerInterval) clearInterval(_timerInterval);
  _timerInterval = setInterval(tickElapsed, 1000);

  function renderHistory(sessions) {
    var el = document.getElementById('sync-history-table');
    var sumEl = document.getElementById('sync-history-summary');
    if (!sessions || !sessions.length) {
      el.innerHTML = '<p class="muted" style="font-size:0.9em;">No persisted sessions in this window yet.</p>';
      sumEl.textContent = '';
      return;
    }
    // Compute summary stats (avg/min/max duration, total bytes).
    var n = sessions.length, sumDur = 0, sumBytes = 0;
    var minDur = Infinity, maxDur = 0;
    sessions.forEach(function(s) {
      var d = parseFloat(s.duration_seconds) || 0;
      sumDur += d; sumBytes += (parseInt(s.total_bytes) || 0);
      if (d < minDur) minDur = d;
      if (d > maxDur) maxDur = d;
    });
    var avg = sumDur / n;
    sumEl.textContent = n + ' sessions \u2014 avg ' + fmtDur(avg) +
      ', min ' + fmtDur(minDur) + ', max ' + fmtDur(maxDur) +
      ', total ' + fmtBytes(sumBytes);

    var html = '<div style="overflow-x:auto;max-height:480px;overflow-y:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.85em;">';
    html += '<thead style="position:sticky;top:0;"><tr style="background:var(--table-header-bg);border-bottom:2px solid var(--table-border);">';
    html += '<th style="padding:6px 8px;text-align:left;">Peer</th>';
    html += '<th style="padding:6px 8px;text-align:left;">Ended</th>';
    html += '<th style="padding:6px 8px;text-align:right;">Duration</th>';
    html += '<th style="padding:6px 8px;text-align:right;">Total</th>';
    html += '<th style="padding:6px 8px;text-align:right;">Speed</th>';
    html += '<th style="padding:6px 8px;text-align:right;">Frames</th>';
    html += '</tr></thead><tbody>';
    sessions.forEach(function(s, i) {
      var rowBg = i % 2 === 0 ? '' : 'background:var(--table-alt-bg,rgba(0,0,0,0.02));';
      html += '<tr style="border-bottom:1px solid var(--table-border);' + rowBg + '">';
      html += '<td style="padding:5px 8px;font-family:monospace;font-size:0.82em;">' + esc(s.peer_node_id) + '</td>';
      html += '<td style="padding:5px 8px;font-size:0.8em;color:var(--muted);" title="' + esc(s.ended_at) + '">' + esc(relTime(s.ended_at)) + '</td>';
      html += '<td style="padding:5px 8px;text-align:right;font-weight:600;">' + fmtDur(s.duration_seconds) + '</td>';
      html += '<td style="padding:5px 8px;text-align:right;">' + fmtBytes(s.total_bytes) + '</td>';
      html += '<td style="padding:5px 8px;text-align:right;color:#2a9d8f;">' + fmtSpeed(s.bps) + '</td>';
      html += '<td style="padding:5px 8px;text-align:right;color:var(--muted);">' + esc(s.frame_count) + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }

  function refreshHistory() {
    var sel = document.getElementById('sync-history-window');
    var since = sel ? sel.value : 604800;
    fetch('/api/sync/session-history?since_seconds=' + encodeURIComponent(since) + '&limit=500',
          { headers: { 'Accept': 'application/json' } })
      .then(function(r) { return r.json(); })
      .then(function(data) { renderHistory(data.sessions || []); })
      .catch(function() {
        document.getElementById('sync-history-table').innerHTML =
          '<p class="muted" style="font-size:0.9em;">Failed to load history.</p>';
      });
  }
  var winSel = document.getElementById('sync-history-window');
  if (winSel) winSel.addEventListener('change', refreshHistory);
  refreshHistory();
  setInterval(refreshHistory, 60000);
})();
</script>

<div class="card">
  <h2>Sync Transmission Stats</h2>
  <form method="post" action="{{ url_for('system_transmissions_reset') }}" onsubmit="return confirm('Reset transmission stats history now?');" style="margin:8px 0 14px 0;">
    <button type="submit" class="btn btn-danger">Reset Stats</button>
  </form>
  <p class=\"muted\">Breakdown of sync frames sent and received by this node, with live inbound and outbound panes below for verification.</p>

  <h3>Direction Summary</h3>
  <p class=\"muted\">Rebuilding nodes often show most game traffic under received frames, because the primary node is pushing state toward them.</p>
  <div style=\"display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:10px 0 24px 0;\">
    <div class=\"card\"><h4 style=\"margin-top:0;\">Last Hour</h4>{{ stats_1h_direction_html|safe }}</div>
    <div class=\"card\"><h4 style=\"margin-top:0;\">Last 24 Hours</h4>{{ stats_24h_direction_html|safe }}</div>
  </div>

  <h3>Category Summary</h3>
  <p class=\"muted\">Game = SCORESYNC + ZORKSAVE, Content = BULLETIN/MAIL/CHANNEL/deletes, Profile = PROFILESYNC, Protocol = SYNCSTATE/HASH* frames.</p>
  <div style=\"display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:10px 0 24px 0;\">
    <div class=\"card\"><h4 style=\"margin-top:0;\">Last Hour</h4>{{ stats_1h_category_html|safe }}</div>
    <div class=\"card\"><h4 style=\"margin-top:0;\">Last 24 Hours</h4>{{ stats_24h_category_html|safe }}</div>
  </div>

  <h3>Per-Frame-Type Detail</h3>
  <div style=\"display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:20px 0;\">
    <div class=\"card\"><h4 style=\"margin-top:0;\">Last Hour</h4>{{ stats_1h_breakdown_html|safe }}</div>
    <div class=\"card\"><h4 style=\"margin-top:0;\">Last 24 Hours</h4>{{ stats_24h_breakdown_html|safe }}</div>
  </div>
</div>

<div class=\"card\">
  <h3>Live Transmission Log</h3>
  <p class=\"muted\">Separate outbound and inbound panes, with shared filters for frame type, peer, free-text search, and an important-only verification view.</p>
  <div class=\"filter-grid\">
    <div>
      <label for=\"sync-log-frame-filter\">Frame Type</label>
      <select id=\"sync-log-frame-filter\"><option value=\"\">All frame types</option></select>
    </div>
    <div>
      <label for=\"sync-log-peer-filter\">Peer Node</label>
      <select id=\"sync-log-peer-filter\"><option value=\"\">All peers</option></select>
    </div>
    <div>
      <label for=\"sync-log-search\">Search Payload</label>
      <input type=\"text\" id=\"sync-log-search\" placeholder=\"HASHMISS, uid, node id, payload text\">
    </div>
    <div style=\"display:flex;align-items:end;gap:12px;flex-wrap:wrap;\">
      <label><input type=\"checkbox\" id=\"sync-log-important-only\"> Important only</label>
      <label><input type=\"checkbox\" id=\"sync-log-hide-continuations\"> Hide continuations</label>
      <button class=\"btn btn-small\" type=\"button\" id=\"sync-log-clear\">Clear View</button>
    </div>
  </div>

  <div class=\"log-grid\">
    <div class=\"log-pane\">
      <h4>Outbound Frames</h4>
      <div class=\"log-meta\" id=\"sync-log-outbound-meta\">0 frame(s)</div>
      <div id=\"sync-log-outbound\" class=\"log-window\"></div>
    </div>
    <div class=\"log-pane\">
      <h4>Inbound Frames</h4>
      <div class=\"log-meta\" id=\"sync-log-inbound-meta\">0 frame(s)</div>
      <div id=\"sync-log-inbound\" class=\"log-window\"></div>
    </div>
  </div>
</div>

<div class=\"card\">
  <h3>Recent Channel Activity</h3>
  <p class=\"muted\">Nearby channel state for sync/debug sessions: recent channel directory entries plus recent comment traffic.</p>
  <div class=\"channel-grid\">
    <div>
      <h4 style=\"margin-top:0;\">Recent Channels</h4>
      <ul class=\"activity-list\">
        {% for row in recent_channels %}
        <li><strong>{{ row['name'] }}</strong> <span class=\"muted\">{{ row['url'] }}</span></li>
        {% endfor %}
        {% if not recent_channels %}
        <li class=\"muted\">No channel entries recorded.</li>
        {% endif %}
      </ul>
    </div>
    <div>
      <h4 style=\"margin-top:0;\">Recent Channel Comments</h4>
      <ul class=\"activity-list\">
        {% for row in recent_channel_comments %}
        <li><strong>{{ row['channel_name'] }}</strong> <span class=\"muted\">{{ row['sender_short_name'] }} | {{ row['date'] }}</span><br>{{ row['content'] }}</li>
        {% endfor %}
        {% if not recent_channel_comments %}
        <li class=\"muted\">No recent channel comment activity.</li>
        {% endif %}
      </ul>
    </div>
  </div>
</div>

<script>
  (function () {
    const outboundRoot = document.getElementById('sync-log-outbound');
    const inboundRoot = document.getElementById('sync-log-inbound');
    if (!outboundRoot || !inboundRoot) return;

    const frameFilter = document.getElementById('sync-log-frame-filter');
    const peerFilter = document.getElementById('sync-log-peer-filter');
    const searchInput = document.getElementById('sync-log-search');
    const importantOnly = document.getElementById('sync-log-important-only');
    const hideContinuations = document.getElementById('sync-log-hide-continuations');
    const clearButton = document.getElementById('sync-log-clear');
    const outboundMeta = document.getElementById('sync-log-outbound-meta');
    const inboundMeta = document.getElementById('sync-log-inbound-meta');

    let entries = {{ initial_transmissions|tojson }};
    let lastId = {{ last_transmission_id }};

    function escapeHtml(value) {
      return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function updateFilterOptions() {
      const selectedFrame = frameFilter.value;
      const selectedPeer = peerFilter.value;
      const frameTypes = Array.from(new Set(entries.map((entry) => entry.frame_type).filter(Boolean))).sort();
      const peers = Array.from(new Set(entries.map((entry) => entry.peer_node_id).filter(Boolean))).sort();
      frameFilter.innerHTML = '<option value="">All frame types</option>' + frameTypes.map((value) => '<option value="' + escapeHtml(value) + '">' + escapeHtml(value) + '</option>').join('');
      peerFilter.innerHTML = '<option value="">All peers</option>' + peers.map((value) => '<option value="' + escapeHtml(value) + '">' + escapeHtml(value) + '</option>').join('');
      frameFilter.value = frameTypes.includes(selectedFrame) ? selectedFrame : '';
      peerFilter.value = peers.includes(selectedPeer) ? selectedPeer : '';
    }

    function renderPane(target, metaTarget, direction) {
      const frameType = frameFilter.value;
      const peer = peerFilter.value;
      const search = (searchInput.value || '').trim().toLowerCase();
      const important = importantOnly.checked;
      const hideChunks = hideContinuations.checked;
      const filtered = entries.filter((entry) => {
        if (entry.direction !== direction) return false;
        if (frameType && entry.frame_type !== frameType) return false;
        if (peer && entry.peer_node_id !== peer) return false;
        if (important && !entry.is_important) return false;
        if (hideChunks && entry.is_continuation) return false;
        if (search) {
          const haystack = [entry.frame_text, entry.frame_type, entry.peer_node_id].join(' ').toLowerCase();
          if (!haystack.includes(search)) return false;
        }
        return true;
      });

      metaTarget.textContent = filtered.length + ' frame(s)';
      target.innerHTML = filtered.map((entry) => {
        const badges = [
          entry.is_important ? '<span class="log-badge important">important</span>' : '',
          entry.is_continuation ? '<span class="log-badge">chunk</span>' : ''
        ].join('');
        return '<div class="log-line">'
          + '<span class="log-time">[' + escapeHtml(entry.transmission_time) + ']</span> '
          + '<span class="log-type">' + escapeHtml(entry.frame_type) + '</span> '
          + '<span class="log-peer">' + escapeHtml(entry.peer_node_id) + '</span> '
          + '<span class="log-size">' + escapeHtml(entry.frame_size_bytes) + ' B</span>'
          + badges
          + '<div class="log-preview">' + escapeHtml(entry.frame_preview) + '</div>'
          + '</div>';
      }).join('');
      target.scrollTop = target.scrollHeight;
    }

    function render() {
      updateFilterOptions();
      renderPane(outboundRoot, outboundMeta, 'tx');
      renderPane(inboundRoot, inboundMeta, 'rx');
    }

    async function poll() {
      try {
        const resp = await fetch('/api/sync/transmissions?since_id=' + encodeURIComponent(lastId), { headers: { 'Accept': 'application/json' } });
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.entries || !data.entries.length) return;
        data.entries.forEach((entry) => {
          entries.push(entry);
          lastId = Math.max(lastId, Number(entry.id || 0));
        });
        if (entries.length > 600) entries = entries.slice(entries.length - 600);
        render();
      } catch (err) {
        // Ignore transient polling failures.
      }
    }

    [frameFilter, peerFilter, searchInput, importantOnly, hideContinuations].forEach((element) => {
      element.addEventListener('input', render);
      element.addEventListener('change', render);
    });
    clearButton.addEventListener('click', function () {
      entries = [];
      render();
    });

    render();
    setInterval(poll, 2000);
  })();
</script>
"""


FLOWCHART_CONTENT = UPDATED_FLOWCHART_CONTENT

MESHTASTIC_DEVICE_CONTENT_SERIAL = """
<div class="card">
  <h2>Meshtastic Device</h2>
  <p class="muted">This BBS is connected to the Meshtastic device via <strong>serial</strong>.
     Serial ports only support one connection at a time, so the built-in device web UI cannot
     run alongside the BBS server.</p>
  <p>You can still access the Meshtastic web client directly in your browser using
     <strong>WebSerial</strong> — it connects from the browser side without going through the BBS
     server, so there is no conflict as long as you stop the BBS server first.</p>
  <div style="margin:24px 0;">
    <a class="btn" href="https://client.meshtastic.org" target="_blank" rel="noopener noreferrer">
      Open Meshtastic Web Client (client.meshtastic.org)
    </a>
  </div>
  <p class="muted" style="font-size:0.85em;">
    Steps: stop the BBS server &rarr; open the link above &rarr; click <em>New connection &rarr; Serial</em>
    &rarr; select your device &rarr; reconnect the BBS server when finished.
  </p>
</div>
"""

MESHTASTIC_DEVICE_CONTENT_TCP = """
<div class="card">
  <h2>Meshtastic Device</h2>
  <p class="muted">Connected via TCP to <strong>{{ device_host }}</strong>.
     The device's built-in web UI is embedded below.</p>
  <p class="muted" style="font-size:0.85em;">If the frame is blank, ensure the device is powered on
     and reachable at <a href="{{ device_url }}" target="_blank" rel="noopener noreferrer">{{ device_url }}</a>.</p>
</div>
<div style="margin:0 -8px;">
  <iframe src="{{ device_url }}"
          style="width:100%;height:calc(100vh - 180px);border:none;border-radius:6px;background:#000;"
          allow="serial"
          title="Meshtastic Device Web UI">
  </iframe>
</div>
"""


def create_app(runtime_interface=None) -> Flask:
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.secret_key = os.getenv("BBS_WEBGUI_SECRET", "change-this-secret")
    app.config["DB_PATH"] = resolve_app_path(os.getenv("BBS_DB_PATH"), "bulletins.db")
    app.config["CONFIG_PATH"] = resolve_app_path(os.getenv("BBS_CONFIG_PATH"), "config.ini")
    # bacon-web-admin.service is a separate process from mesh-bbs.service and
    # must not depend on that other service having already run schema
    # migrations at least once -- e.g. a fresh install, or any window where
    # mesh-bbs.service is stopped (maintenance, troubleshooting) while the
    # web admin stays up, would otherwise 500 on any account/table this
    # process is first to touch. initialize_database() is fully idempotent
    # (CREATE TABLE IF NOT EXISTS / guarded ALTER TABLE throughout), so
    # calling it here is always safe, even when mesh-bbs.service already did.
    initialize_database()
    install_connection_log_handler(app.config["DB_PATH"])
    admin_user, admin_password, username_env_override, password_env_override = load_admin_credentials(app.config["CONFIG_PATH"])
    app.config["ADMIN_USER"] = admin_user
    app.config["ADMIN_PASSWORD"] = admin_password
    app.config["ADMIN_USER_ENV_OVERRIDE"] = username_env_override
    app.config["ADMIN_PASSWORD_ENV_OVERRIDE"] = password_env_override
    app.config["BULLETIN_BOARDS"] = load_bulletin_boards(app.config["CONFIG_PATH"])
    app.config["RUNTIME_UPDATES_ENABLED"] = runtime_interface is not None
    app.config["DISPLAY_VERSION"] = get_display_version()
    # Cache-bust static assets per deploy: derive a token from the version string
    # (changes on every release), falling back to process start time. Appended as
    # ?v=... to CSS/JS links so browsers fetch fresh assets after an update
    # instead of serving stale cached copies.
    import re as _re
    app.config["ASSET_VERSION"] = _re.sub(r'[^A-Za-z0-9]', '', app.config["DISPLAY_VERSION"]) or str(int(time.time()))
    _mesh_ui_dist_env = os.getenv("BBS_MESH_UI_DIST_PATH", "")
    app.config["MESH_UI_DIST_PATH"] = resolve_app_path(_mesh_ui_dist_env if _mesh_ui_dist_env else None, "meshtastic-web-dist")

    @app.context_processor
    def inject_global_template_values():
      return {
        "app_version_display": app.config.get("DISPLAY_VERSION", "unknown"),
        "asset_version": app.config.get("ASSET_VERSION", "0"),
        "csrf_token": get_csrf_token(),
      }

    @app.template_filter("relative_age")
    def relative_age_filter(value) -> str:
      """'2m ago' for a stored 'YYYY-MM-DD HH:MM:SS' timestamp.

      Rendered server-side rather than by script so the table never shows
      a flash of raw timestamps, and still reads correctly with scripting
      off. The exact value stays available in the cell's title attribute.
      """
      text = str(value or "").strip()
      if not text:
        return ""
      for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
          when = datetime.strptime(text[:19], fmt)
          break
        except ValueError:
          continue
      else:
        # Unparseable: show what we have rather than an empty cell.
        return text
      seconds = int((datetime.now() - when).total_seconds())
      if seconds < 0:
        return "just now"
      if seconds < 60:
        return f"{seconds}s ago"
      if seconds < 3600:
        return f"{seconds // 60}m ago"
      if seconds < 86400:
        return f"{seconds // 3600}h ago"
      return f"{seconds // 86400}d ago"

    @app.template_filter("age_seconds")
    def age_seconds_filter(value) -> str:
      """Seconds since a stored timestamp, for the client-side filters.

      Computed here rather than in the browser because the stored values
      are the server's local time; comparing them against the viewer's
      clock would skew the "seen within" filter by the offset between the
      two. Empty when unparseable, and the filter keeps such rows rather
      than hiding a real device over a missing field.
      """
      text = str(value or "").strip()
      if not text:
        return ""
      for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
          when = datetime.strptime(text[:19], fmt)
          break
        except ValueError:
          continue
      else:
        return ""
      return str(max(0, int((datetime.now() - when).total_seconds())))

    @app.template_filter("middle_ellipsis")
    def middle_ellipsis_filter(value, keep: int = 8) -> str:
      """Shorten from the middle, keeping both ends legible.

      MeshCore node ids are 64-character public keys while Meshtastic ids
      are 9 characters, so one MeshCore row would otherwise set the width
      of the whole column. Both ends are kept because the head identifies
      the key and the tail is what distinguishes two keys with a shared
      prefix -- a trailing ellipsis would throw away the useful half.
      """
      text = str(value or "")
      if len(text) <= keep * 2 + 1:
        return text
      return f"{text[:keep]}…{text[-keep:]}"

    _CSRF_SESSION_KEY = "_csrf_token"
    _MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def get_csrf_token() -> str:
      token = str(session.get(_CSRF_SESSION_KEY) or "").strip()
      if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
      return token

    def csrf_request_valid() -> bool:
      expected = str(session.get(_CSRF_SESSION_KEY) or "").strip()
      if not expected:
        return False
      provided = str(request.form.get("csrf_token", "") or "").strip()
      if not provided:
        provided = str(request.headers.get("X-CSRF-Token", "") or "").strip()
      if not provided:
        provided = str(request.headers.get("X-CSRFToken", "") or "").strip()
      if not provided:
        return False
      return secrets.compare_digest(expected, provided)

    def get_runtime_interface():
        return runtime_interface

    class ManagedConnection(sqlite3.Connection):
      def __exit__(self, exc_type, exc_val, exc_tb):
        try:
          return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
          self.close()

    def initialize_db_safety() -> None:
        with sqlite3.connect(app.config["DB_PATH"], timeout=30, factory=ManagedConnection) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=FULL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute('''CREATE TABLE IF NOT EXISTS channel_comments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              channel_id INTEGER NOT NULL,
              sender_short_name TEXT NOT NULL,
              date TEXT NOT NULL,
              content TEXT NOT NULL,
              unique_id TEXT NOT NULL DEFAULT '',
              expected_content_length INTEGER,
              content_complete INTEGER NOT NULL DEFAULT 1,
              source_node_id TEXT,
              source_timestamp TEXT,
              received_at TEXT,
              FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )''')
            try:
              conn.execute("ALTER TABLE channel_comments ADD COLUMN unique_id TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE channel_comments ADD COLUMN expected_content_length INTEGER")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE channel_comments ADD COLUMN content_complete INTEGER NOT NULL DEFAULT 1")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE channel_comments ADD COLUMN source_node_id TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE channel_comments ADD COLUMN source_timestamp TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE channel_comments ADD COLUMN received_at TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE bulletins ADD COLUMN source_node_id TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE bulletins ADD COLUMN source_timestamp TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE bulletins ADD COLUMN received_at TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE mail ADD COLUMN source_node_id TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE mail ADD COLUMN source_timestamp TEXT")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE mail ADD COLUMN received_at TEXT")
            except Exception:
              pass
            conn.execute('''CREATE TABLE IF NOT EXISTS connection_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_time TEXT NOT NULL,
              sender_num TEXT,
              sender_node_id TEXT,
              sender_short_name TEXT,
              to_id TEXT,
              message_type TEXT NOT NULL,
              event_text TEXT NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS deleted_sync_tombstones (
              tombstone_key TEXT PRIMARY KEY,
              deleted_at TEXT NOT NULL
            )''')
            try:
              conn.execute("ALTER TABLE bulletins ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE channels ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
            except Exception:
              pass
            conn.execute('''CREATE TABLE IF NOT EXISTS peer_sync_state (
              peer_node_id TEXT PRIMARY KEY,
              bulletins INTEGER NOT NULL DEFAULT 0,
              mail INTEGER NOT NULL DEFAULT 0,
              channels INTEGER NOT NULL DEFAULT 0,
              zork_saves INTEGER NOT NULL DEFAULT 0,
              profiles INTEGER NOT NULL DEFAULT 0,
              game_scores INTEGER NOT NULL DEFAULT 0,
              bulletins_hash TEXT NOT NULL DEFAULT '',
              mail_hash TEXT NOT NULL DEFAULT '',
              channels_hash TEXT NOT NULL DEFAULT '',
              zork_saves_hash TEXT NOT NULL DEFAULT '',
              profiles_hash TEXT NOT NULL DEFAULT '',
              game_scores_hash TEXT NOT NULL DEFAULT '',
              reported_at TEXT NOT NULL
            )''')
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN profiles INTEGER NOT NULL DEFAULT 0")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN game_scores INTEGER NOT NULL DEFAULT 0")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN bulletins_hash TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN mail_hash TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN channels_hash TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN zork_saves_hash TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN profiles_hash TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN game_scores_hash TEXT NOT NULL DEFAULT ''")
            except Exception:
              pass
            try:
              conn.execute("ALTER TABLE peer_sync_state ADD COLUMN tombstones INTEGER NOT NULL DEFAULT -1")
            except Exception:
              pass

    def get_db_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(app.config["DB_PATH"], timeout=30, factory=ManagedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def execute_write(query: str, params: tuple = ()) -> None:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(query, params)
            conn.commit()

    def wipe_database_contents() -> None:
      tables = [
        "channel_comments",
        "public_chatter",
        "bulletins",
        "mail",
        "channels",
        "zork_saves",
        "user_profiles",
        "game_scores",
        "connection_events",
        "peer_sync_state",
        "deleted_sync_tombstones",
      ]
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("PRAGMA foreign_keys = ON")
        for table_name in tables:
          cursor.execute(f"DELETE FROM {table_name}")
        try:
          placeholders = ",".join("?" for _ in tables)
          cursor.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tuple(tables))
        except sqlite3.OperationalError:
          pass
        conn.commit()

    def save_bulletin_boards(boards: list[str]) -> None:
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("boards"):
        config.add_section("boards")
      config.set("boards", "bulletin_boards", ",".join(boards))
      write_config_file(config, app.config["CONFIG_PATH"])

    def save_subscriber_settings(form) -> None:
      """Persist [sync] subscriber_nodes — node IDs the gateway answers pull
      requests for (WANT/HASHMISS) without push-syncing to them. Read fresh by
      the server's peer-list refresh, so it hot-reloads."""
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("sync"):
        config.add_section("sync")
      raw = form.get("subscriber_nodes", "")
      nodes = [n.strip() for n in raw.replace("\r", "\n").replace(",", "\n").split("\n") if n.strip()]
      config.set("sync", "subscriber_nodes", ",".join(nodes))
      write_config_file(config, app.config["CONFIG_PATH"])

    def save_storage_settings(form) -> None:
      """Persist [maintenance] max_db_size_mb. Read fresh by the server each
      maintenance pass, so the change hot-reloads (no restart needed)."""
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("maintenance"):
        config.add_section("maintenance")
      raw = form.get("max_db_size_mb", "").strip()
      try:
        val = max(0, int(raw))
      except ValueError:
        val = 0
      config.set("maintenance", "max_db_size_mb", str(val))
      write_config_file(config, app.config["CONFIG_PATH"])

    def save_public_chatter_settings(form) -> list[str]:
      errors = []
      parsed = {}
      for key in ("primary_channels", "secondary_channels"):
        values = []
        for token in form.get(key, "").replace("\n", ",").split(","):
          token = token.strip()
          if not token:
            continue
          try:
            value = int(token)
          except ValueError:
            errors.append(f"{key.replace('_', ' ').title()} contains '{token}', which is not a number.")
            continue
          if not 0 <= value <= 255:
            errors.append(f"Channel index {value} must be between 0 and 255.")
          elif value not in values:
            values.append(value)
        parsed[key] = values
      if errors:
        return errors
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("public_chatter"):
        config.add_section("public_chatter")
      config.set(
        "public_chatter", "enabled",
        "true" if _parse_bool_setting(form.get("enabled", ""), False) else "false",
      )
      for key, values in parsed.items():
        config.set("public_chatter", key, ",".join(str(value) for value in values))
      write_config_file(config, app.config["CONFIG_PATH"])
      return []

    def save_gateway_settings(form) -> None:
      """Persist the [gateway] section from the web-admin form. Hot-reloads:
      gateway.py + local_capabilities_token read config fresh on every call, so
      no restart is needed for the change (incl. enabling the apigw capability)."""
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("gateway"):
        config.add_section("gateway")
      enabled = _parse_bool_setting(form.get("gateway_enabled", ""), False)
      config.set("gateway", "enabled", "true" if enabled else "false")
      # Free-text / list fields.
      for key in ("ai_base_url", "ai_model", "ai_system_prompt", "ai_api_key",
                  "allowed_hosts", "allowed_schemes", "allowed_nodes"):
        config.set("gateway", key, form.get(f"gateway_{key}", "").strip())
      dialect = form.get("gateway_ai_dialect", "ollama").strip().lower()
      if dialect not in ("ollama", "openai", "nomad"):
        dialect = "ollama"
      config.set("gateway", "ai_dialect", dialect)
      # Numeric fields (clamped to sane minimums; fall back on bad input).
      for key, default, minimum in (("request_timeout", 20, 1),
                                    ("max_response_bytes", 800, 64),
                                    ("rate_limit_per_node", 5, 0)):
        raw = form.get(f"gateway_{key}", "").strip()
        try:
          val = max(minimum, int(raw))
        except ValueError:
          val = default
        config.set("gateway", key, str(val))
      write_config_file(config, app.config["CONFIG_PATH"])

    def _write_device_fields(config, section: str, form, prefix: str) -> None:
      config.set(section, "port", form.get(f"{prefix}port", "").strip())
      config.set(section, "hostname", form.get(f"{prefix}hostname", "").strip())
      config.set(section, "tcp_port", str(_parse_int_setting(form.get(f"{prefix}tcp_port", ""), 5000, minimum=1)))
      config.set(section, "baudrate", str(_parse_int_setting(form.get(f"{prefix}baudrate", ""), 115200, minimum=1)))
      config.set(section, "ble_address", form.get(f"{prefix}ble_address", "").strip())
      config.set(section, "ble_pin", form.get(f"{prefix}ble_pin", "").strip())
      config.set(section, "channel_index", str(_parse_int_setting(form.get(f"{prefix}channel_index", ""), 0, minimum=0)))

    def save_device_settings(form) -> list[str]:
      """Persist [interface] (primary) and, optionally, [interface2] +
      [sync2]/[allow_list2] (secondary, dual-radio bridge mode) from the web
      Devices form. Returns a list of validation error messages -- on any
      error, nothing is written, so a bad edit can't leave config.ini in a
      half-saved state. Requires a mesh-bbs service restart to take effect
      (see load_device_settings)."""
      errors = []

      primary_type = form.get("primary_type", "").strip().lower()
      if primary_type not in DEVICE_TYPES:
        errors.append("Primary radio: choose a valid device type.")
      primary_port = form.get("primary_port", "").strip()
      primary_hostname = form.get("primary_hostname", "").strip()
      if primary_type in ("tcp", "meshcore_tcp") and not primary_hostname:
        errors.append("Primary radio: hostname is required for a TCP connection.")

      secondary_enabled = _parse_bool_setting(form.get("secondary_enabled", ""), False)
      secondary_type = form.get("secondary_type", "").strip().lower()
      secondary_port = form.get("secondary_port", "").strip()
      secondary_hostname = form.get("secondary_hostname", "").strip()
      if secondary_enabled:
        if secondary_type not in DEVICE_TYPES or secondary_type == "none":
          errors.append("Secondary radio: choose a valid device type.")
        elif secondary_type in ("tcp", "meshcore_tcp") and not secondary_hostname:
          errors.append("Secondary radio: hostname is required for a TCP connection.")
        if (
          primary_type in ("serial", "meshcore_serial")
          and secondary_type in ("serial", "meshcore_serial")
          and primary_port and primary_port == secondary_port
        ):
          errors.append("Primary and secondary radios cannot use the same serial port.")

      if errors:
        return errors

      config = read_config_file(app.config["CONFIG_PATH"])

      if not config.has_section("interface"):
        config.add_section("interface")
      config.set("interface", "type", primary_type)
      _write_device_fields(config, "interface", form, "primary_")

      if secondary_enabled:
        if not config.has_section("interface2"):
          config.add_section("interface2")
        config.set("interface2", "enabled", "true")
        config.set("interface2", "type", secondary_type)
        _write_device_fields(config, "interface2", form, "secondary_")

        if not config.has_section("sync2"):
          config.add_section("sync2")
        # Peers are edited on the Sync page now, so this form no longer
        # carries them. Absent must mean "leave alone" -- reading a missing
        # field as empty would silently wipe every secondary-radio peer on
        # any unrelated save from this form.
        if "bbs_nodes2" in form:
          config.set("sync2", "bbs_nodes", ",".join(parse_list_input(form.get("bbs_nodes2", ""))))
        if not config.has_section("allow_list2"):
          config.add_section("allow_list2")
        config.set("allow_list2", "allowed_nodes", ",".join(parse_list_input(form.get("allowed_nodes2", ""))))
      elif config.has_section("interface2"):
        # Keep the section (and its saved connection details) around but
        # mark disabled, rather than deleting it, so re-enabling later
        # doesn't require re-entering everything.
        config.set("interface2", "enabled", "false")

      write_config_file(config, app.config["CONFIG_PATH"])
      return []

    def save_account_settings(form) -> None:
      """Persist [accounts] tunables. Hot-reloads: command_handlers.py
      reads these fresh via utils._config_int on every use, so no restart
      is needed for the change."""
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("accounts"):
        config.add_section("accounts")
      for key, default, minimum in (
        ("link_code_ttl_minutes", 10, 1),
        ("link_requests_per_hour", 3, 0),
        ("link_attempts_per_hour", 5, 0),
        ("max_linked_devices", 6, 1),
        ("link_code_delay_minutes", 2, 1),
      ):
        raw = form.get(key, "").strip()
        try:
          val = max(minimum, int(raw))
        except ValueError:
          val = default
        config.set("accounts", key, str(val))
      write_config_file(config, app.config["CONFIG_PATH"])

    def save_mqtt_settings(form) -> list[str]:
      """Persist every [mqttN] section (+ its [sync_mqttN]/[allow_list_mqttN]
      peer lists) from the web MQTT Bridges form.

      The submitted 'mqtt_indexes' hidden field (kept in sync by the page's
      JS as broker rows are added/removed) is the source of truth for which
      brokers exist: any previously-configured index no longer in that list
      is deleted outright -- unlike [interface2]'s fixed slot, MQTT links are
      open-ended and user-managed, so "removed in the form" should mean
      "gone from config.ini", not just disabled.

      Returns a list of validation error messages -- on any error, nothing
      is written, so a bad edit can't leave config.ini half-saved. Requires
      a mesh-bbs service restart to take effect (see load_mqtt_settings)."""
      errors = []

      submitted_indexes = []
      for token in form.get("mqtt_indexes", "").strip().split(","):
        token = token.strip()
        if not token:
          continue
        try:
          submitted_indexes.append(int(token))
        except ValueError:
          pass

      # Validate every upload BEFORE writing any of them, so a bad file in
      # one broker can't leave another broker's cert already replaced on
      # disk -- same all-or-nothing contract the config write below has.
      pending_uploads = []   # (index, role, text)
      uploaded_paths = {}    # (index, role) -> path the config should point at
      for index in submitted_indexes:
        for role in _MQTT_CERT_ROLES:
          upload = request.files.get(f"mqtt_{index}_{role}_upload")
          if upload is None or not (upload.filename or "").strip():
            continue
          label = f"Broker #{index} {role.replace('tls_', '').replace('_', ' ')}"
          expect = "key" if role == "tls_keyfile" else "cert"
          text, error = validate_pem_upload(upload.read(), expect, label)
          if error:
            errors.append(error)
          else:
            pending_uploads.append((index, role, text))
            uploaded_paths[(index, role)] = os.path.join(
              get_mqtt_cert_dir(), f"mqtt{index}", _MQTT_CERT_ROLES[role][0]
            )

      parsed = []
      for index in submitted_indexes:
        prefix = f"mqtt_{index}_"
        host = form.get(f"{prefix}host", "").strip()
        # Normalize topic-forming fields on save so config.ini stores the
        # value that will actually be used -- spaces become '-', and MQTT
        # wildcards/separators are neutralized. Without this the saved text
        # and the live topic silently disagree.
        from mqtt_interface import sanitize_topic_segment
        raw_topic_prefix = form.get(f"{prefix}topic_prefix", "").strip()
        topic_prefix = sanitize_topic_segment(raw_topic_prefix, allow_slash=True)
        if not host:
          errors.append(f"Broker #{index}: host is required.")
        if not topic_prefix:
          errors.append(f"Broker #{index}: topic prefix is required.")
        # An upload wins over the text path field: the operator just picked
        # a file, so that's the more recent intent.
        tls_certfile = uploaded_paths.get(
          (index, "tls_certfile"), form.get(f"{prefix}tls_certfile", "").strip())
        tls_keyfile = uploaded_paths.get(
          (index, "tls_keyfile"), form.get(f"{prefix}tls_keyfile", "").strip())
        if tls_keyfile and not tls_certfile:
          errors.append(
            f"Broker #{index}: a client key file needs its client certificate too "
            "(set both, or neither)."
          )
        parsed.append({
          "index": index,
          "enabled": _parse_bool_setting(form.get(f"{prefix}enabled", ""), False),
          "host": host,
          "port": _parse_int_setting(form.get(f"{prefix}port", ""), 1883, minimum=1),
          "tls": _parse_bool_setting(form.get(f"{prefix}tls", ""), False),
          "tls_ca_certs": uploaded_paths.get(
            (index, "tls_ca_certs"), form.get(f"{prefix}tls_ca_certs", "").strip()),
          "tls_certfile": tls_certfile,
          "tls_keyfile": tls_keyfile,
          "tls_keyfile_password": form.get(f"{prefix}tls_keyfile_password", "").strip(),
          "tls_insecure": _parse_bool_setting(form.get(f"{prefix}tls_insecure", ""), False),
          "username": form.get(f"{prefix}username", "").strip(),
          "password": form.get(f"{prefix}password", "").strip(),
          "topic_prefix": topic_prefix,
          "local_id": sanitize_topic_segment(form.get(f"{prefix}local_id", "")),
          "client_id": form.get(f"{prefix}client_id", "").strip(),
          "keepalive": _parse_int_setting(form.get(f"{prefix}keepalive", ""), 60, minimum=5),
          "publish_status": _parse_bool_setting(form.get(f"{prefix}publish_status", ""), False),
          "publish_clients": _parse_bool_setting(form.get(f"{prefix}publish_clients", ""), False),
          "publish_telemetry": _parse_bool_setting(form.get(f"{prefix}publish_telemetry", ""), False),
          "publish_activity": _parse_bool_setting(form.get(f"{prefix}publish_activity", ""), False),
          "publish_sync_stats": _parse_bool_setting(form.get(f"{prefix}publish_sync_stats", ""), False),
          "publish_prefix": sanitize_topic_segment(
            form.get(f"{prefix}publish_prefix", ""), allow_slash=True),
          "publish_clients_max_age_hours": _parse_int_setting(
            form.get(f"{prefix}publish_clients_max_age_hours", ""), 24, minimum=0),
          # Same as above: absent means keep what is configured, since the
          # Sync page owns peer lists now.
          "bbs_nodes": (parse_list_input(form.get(f"{prefix}bbs_nodes", ""))
                        if f"{prefix}bbs_nodes" in form else None),
          "allowed_nodes": parse_list_input(form.get(f"{prefix}allowed_nodes", "")),
        })

      # Each bridge relationship needs its own topic_prefix -- reusing one
      # across links means every subscriber sees every message, silently
      # merging two supposedly-independent bridges (see example_config.ini).
      seen_prefixes: dict[str, int] = {}
      for link in parsed:
        if not link["topic_prefix"]:
          continue
        prior = seen_prefixes.get(link["topic_prefix"])
        if prior is not None:
          errors.append(
            f"Broker #{prior} and #{link['index']} both use topic prefix "
            f"'{link['topic_prefix']}' -- each bridge needs its own unique prefix."
          )
        else:
          seen_prefixes[link["topic_prefix"]] = link["index"]

      if errors:
        return errors

      # Every upload validated cleanly -- safe to write them to disk now.
      for index, role, text in pending_uploads:
        try:
          store_mqtt_cert(index, role, text)
        except OSError as exc:
          return [f"Broker #{index}: could not save the uploaded file: {exc}"]

      config = read_config_file(app.config["CONFIG_PATH"])

      existing_indexes = set()
      for section_name in config.sections():
        match = _MQTT_SECTION_RE.match(section_name)
        if match:
          existing_indexes.add(int(match.group(1)))
      submitted_set = {link["index"] for link in parsed}
      for index in existing_indexes - submitted_set:
        for section_name in (f"mqtt{index}", f"sync_mqtt{index}", f"allow_list_mqtt{index}"):
          if config.has_section(section_name):
            config.remove_section(section_name)

      for link in parsed:
        section_name = f"mqtt{link['index']}"
        if not config.has_section(section_name):
          config.add_section(section_name)
        config.set(section_name, "enabled", "true" if link["enabled"] else "false")
        config.set(section_name, "host", link["host"])
        config.set(section_name, "port", str(link["port"]))
        config.set(section_name, "tls", "true" if link["tls"] else "false")
        config.set(section_name, "tls_ca_certs", link["tls_ca_certs"])
        config.set(section_name, "tls_certfile", link["tls_certfile"])
        config.set(section_name, "tls_keyfile", link["tls_keyfile"])
        config.set(section_name, "tls_keyfile_password", link["tls_keyfile_password"])
        config.set(section_name, "tls_insecure", "true" if link["tls_insecure"] else "false")
        config.set(section_name, "username", link["username"])
        config.set(section_name, "password", link["password"])
        config.set(section_name, "topic_prefix", link["topic_prefix"])
        config.set(section_name, "local_id", link["local_id"])
        config.set(section_name, "client_id", link["client_id"])
        config.set(section_name, "keepalive", str(link["keepalive"]))
        for kind in ("status", "clients", "telemetry", "activity", "sync_stats"):
          config.set(section_name, f"publish_{kind}",
                     "true" if link[f"publish_{kind}"] else "false")
        config.set(section_name, "publish_prefix", link["publish_prefix"])
        config.set(section_name, "publish_clients_max_age_hours",
                   str(link["publish_clients_max_age_hours"]))

        sync_section_name = f"sync_mqtt{link['index']}"
        if not config.has_section(sync_section_name):
          config.add_section(sync_section_name)
        # None means the form did not carry peers (they are edited on the
        # Sync page); anything else replaces the list.
        if link["bbs_nodes"] is not None:
          config.set(sync_section_name, "bbs_nodes", ",".join(link["bbs_nodes"]))

        allow_section_name = f"allow_list_mqtt{link['index']}"
        if not config.has_section(allow_section_name):
          config.add_section(allow_section_name)
        config.set(allow_section_name, "allowed_nodes", ",".join(link["allowed_nodes"]))

      write_config_file(config, app.config["CONFIG_PATH"])
      return []

    def save_admin_credentials(username, password) -> None:
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("admin"):
        config.add_section("admin")
      if username is not None:
        config.set("admin", "username", username)
      if password is not None:
        config.set("admin", "password", password)
      write_config_file(config, app.config["CONFIG_PATH"])

    def save_sync_lists(
      bbs_nodes: list[str],
      allowed_nodes: list[str],
      sync_interval_minutes: int,
      sync_zork_saves: bool,
      sync_speed_settings: dict[str, object],
    ) -> None:
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("sync"):
        config.add_section("sync")
      if not config.has_section("allow_list"):
        config.add_section("allow_list")
      config.set("sync", "bbs_nodes", ",".join(bbs_nodes))
      config.set("sync", "sync_interval_minutes", str(sync_interval_minutes))
      config.set("sync", "sync_zork_saves", "true" if sync_zork_saves else "false")
      config.set("sync", "sync_turbo", "true" if bool(sync_speed_settings.get("sync_turbo", False)) else "false")
      config.set("sync", "sync_pause_seconds", str(sync_speed_settings.get("sync_pause_seconds", 0.75)))
      config.set("sync", "hash_repair_pause_seconds", str(sync_speed_settings.get("hash_repair_pause_seconds", 0.1)))
      config.set("sync", "full_sync_delay_ms", str(sync_speed_settings.get("full_sync_delay_ms", 500)))
      config.set("allow_list", "allowed_nodes", ",".join(allowed_nodes))
      write_config_file(config, app.config["CONFIG_PATH"])

    def apply_runtime_sync_settings(bbs_nodes: list[str], allowed_nodes: list[str], sync_zork_saves: bool) -> None:
      interface = get_runtime_interface()
      if interface is None:
        return
      interface.bbs_nodes = list(bbs_nodes)
      interface.allowed_nodes = list(allowed_nodes)
      interface.sync_zork_saves = bool(sync_zork_saves)

    def update_board_settings(raw_boards: str) -> bool:
      updated_boards = parse_list_input(raw_boards)

      if not updated_boards:
        flash("At least one board is required.", "error")
        return False

      save_bulletin_boards(updated_boards)
      app.config["BULLETIN_BOARDS"] = updated_boards
      flash("Board list saved.", "success")
      return True

    def add_sync_peer(section: str, node_id: str) -> None:
      """Append one peer to one link's list, from the Sync page."""
      section = str(section or "").strip()
      node_id = str(node_id or "").strip()
      if not node_id:
        flash("Enter a peer node ID.", "error")
        return
      valid = {t["section"] for t in load_peer_link_targets(app.config["CONFIG_PATH"])}
      if section not in valid:
        flash("Pick which link this peer is reachable on.", "error")
        return

      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section(section):
        config.add_section(section)
      existing = parse_list_input(config.get(section, "bbs_nodes", fallback=""))
      if node_id in existing:
        flash(f"{node_id} is already a peer on that link.", "error")
        return

      # An MQTT peer's address embeds the topic it lives on; one that does
      # not match the link's own prefix can never be reached, and saying so
      # now beats it silently never syncing.
      mqtt_match = re.match(r"^sync_mqtt(\d+)$", section)
      if mqtt_match:
        link_prefix = config.get(f"mqtt{mqtt_match.group(1)}", "topic_prefix", fallback="").strip()
        if not node_id.startswith("mqtt:") or node_id.count(":") < 2:
          flash(f"{node_id} is not an MQTT address — expected mqtt:<topic>:<name>.", "error")
          return
        peer_prefix = node_id.split(":", 2)[1]
        if link_prefix and peer_prefix != link_prefix:
          flash(
            f"{node_id} is on topic '{peer_prefix}' but that link uses '{link_prefix}', "
            "so they would never reach each other.", "error")
          return

      existing.append(node_id)
      config.set(section, "bbs_nodes", ",".join(existing))
      write_config_file(config, app.config["CONFIG_PATH"])
      flash(f"Added {node_id}. Restart the mesh-bbs service to start syncing with it.", "success")

    def remove_sync_peers(selected: list) -> None:
      """Drop chosen peers from config and forget their sync state.

      Both halves matter. Removing the config entry stops us talking to the
      peer; deleting its peer_sync_state row is what stops it appearing in
      Diagnostics forever as a peer that is permanently behind. A peer that
      only exists in the database (already removed from config at some
      point) is handled by the second half alone.
      """
      from db_operations import forget_peer_sync_state

      config = read_config_file(app.config["CONFIG_PATH"])
      removed_config = 0
      forgotten = 0
      touched_sections = set()

      for item in selected:
        section, _, node_id = str(item).partition("|")
        node_id = node_id.strip()
        if not node_id:
          continue
        if section and config.has_option(section, "bbs_nodes"):
          remaining = [
            existing for existing in parse_list_input(config.get(section, "bbs_nodes", fallback=""))
            if existing != node_id
          ]
          config.set(section, "bbs_nodes", ",".join(remaining))
          removed_config += 1
          touched_sections.add(section)
        try:
          if forget_peer_sync_state(node_id):
            forgotten += 1
        except Exception:
          logging.debug("could not forget peer state for %s", node_id, exc_info=True)

      if touched_sections:
        write_config_file(config, app.config["CONFIG_PATH"])

      if not removed_config and not forgotten:
        flash("Nothing to remove.", "error")
        return
      parts = []
      if removed_config:
        parts.append(f"removed {removed_config} peer(s) from config")
      if forgotten:
        parts.append(f"cleared {forgotten} stored sync state row(s)")
      note = " and ".join(parts).capitalize()
      if removed_config:
        note += ". Restart the mesh-bbs service to stop syncing with them"
      flash(note + ".", "success")

    def update_sync_settings(
      raw_bbs_nodes,          # str, or None to keep the configured peers
      raw_allowed_nodes: str,
      raw_sync_interval_minutes: str,
      raw_sync_zork_saves: str,
      raw_sync_turbo: str,
      raw_sync_pause_seconds: str,
      raw_hash_repair_pause_seconds: str,
      raw_full_sync_delay_ms: str,
    ) -> bool:
      # None means "not in the form" -- peers are edited on the Sync
      # page's peer table now, and treating a missing field as empty would
      # wipe every primary peer whenever the pacing form is saved.
      if raw_bbs_nodes is None:
        bbs_nodes = parse_list_input(
          read_config_file(app.config["CONFIG_PATH"]).get("sync", "bbs_nodes", fallback=""))
      else:
        bbs_nodes = parse_list_input(raw_bbs_nodes)
      allowed_nodes = parse_list_input(raw_allowed_nodes)
      try:
        sync_interval_minutes = int((raw_sync_interval_minutes or "").strip())
      except ValueError:
        flash("Sync interval must be a whole number of minutes.", "error")
        return False

      if sync_interval_minutes < 1:
        flash("Sync interval must be at least 1 minute.", "error")
        return False

      existing_speed_settings = load_sync_speed_settings(app.config["CONFIG_PATH"])

      sync_pause_input = str(raw_sync_pause_seconds or "").strip()
      if sync_pause_input == "":
        sync_pause_seconds = float(existing_speed_settings["sync_pause_seconds"])
      else:
        try:
          sync_pause_seconds = max(0.0, float(sync_pause_input))
        except ValueError:
          flash("Inter-frame pause must be a non-negative number of seconds.", "error")
          return False

      hash_repair_input = str(raw_hash_repair_pause_seconds or "").strip()
      if hash_repair_input == "":
        hash_repair_pause_seconds = float(existing_speed_settings["hash_repair_pause_seconds"])
      else:
        try:
          hash_repair_pause_seconds = max(0.0, float(hash_repair_input))
        except ValueError:
          flash("Hash repair pause must be a non-negative number of seconds.", "error")
          return False

      full_sync_delay_input = str(raw_full_sync_delay_ms or "").strip()
      if full_sync_delay_input == "":
        full_sync_delay_ms = int(existing_speed_settings["full_sync_delay_ms"])
      else:
        try:
          full_sync_delay_ms = max(0, int(full_sync_delay_input))
        except ValueError:
          flash("Full sync startup delay must be a non-negative whole number of milliseconds.", "error")
          return False

      sync_speed_settings = {
        "sync_turbo": _parse_bool_setting(raw_sync_turbo, bool(existing_speed_settings["sync_turbo"])),
        "sync_pause_seconds": sync_pause_seconds,
        "hash_repair_pause_seconds": hash_repair_pause_seconds,
        "full_sync_delay_ms": full_sync_delay_ms,
      }
      sync_zork_saves = _parse_bool_setting(raw_sync_zork_saves, False)

      save_sync_lists(bbs_nodes, allowed_nodes, sync_interval_minutes, sync_zork_saves, sync_speed_settings)
      apply_runtime_sync_settings(bbs_nodes, allowed_nodes, sync_zork_saves)
      flash("Sync settings updated.", "success")
      return True

    def update_admin_settings(current_password: str, new_username: str, new_password: str, confirm_password: str) -> bool:
      if current_password != app.config["ADMIN_PASSWORD"]:
        flash("Current password is incorrect.", "error")
        return False
      if new_password != confirm_password:
        flash("New passwords do not match.", "error")
        return False
      if new_password and len(new_password) < 4:
        flash("New password must be at least 4 characters.", "error")
        return False

      updated_username = None
      updated_password = None

      if new_username:
        updated_username = new_username
        app.config["ADMIN_USER"] = new_username

      if new_password:
        updated_password = new_password
        app.config["ADMIN_PASSWORD"] = new_password

      save_admin_credentials(updated_username, updated_password)
      flash("Credentials updated successfully. Use your new credentials on next login.", "success")
      return True

    def get_peer_mismatch_snapshot(expected_peer_nodes=None) -> dict:
      expected = None
      if expected_peer_nodes is not None:
        expected = {str(node) for node in expected_peer_nodes if str(node).strip()}
      snapshot = {
        "mismatch": False,
        "mismatch_count": 0,
        "status_text": "no peer reports",
        "rows": [],
        "scope_lines": ["No peer status received yet"],
      }
      try:
        conn = get_db_connection()
        try:
          c = conn.cursor()
          c.execute(
            "SELECT peer_node_id, bulletins, mail, channels, zork_saves, profiles, game_scores, "
            "bulletins_hash, mail_hash, channels_hash, zork_saves_hash, profiles_hash, game_scores_hash, reported_at "
            "FROM peer_sync_state ORDER BY peer_node_id"
          )
          peers = c.fetchall()
          if expected is not None:
            peers = [peer for peer in peers if str(peer[0]) in expected]
          snapshot["rows"] = peers
          if not peers:
            return snapshot

          from db_operations import get_local_record_counts, get_mismatched_peer_scopes

          local_hashes = get_local_record_counts()
          scopes_by_peer = get_mismatched_peer_scopes(expected)

          mismatch_count = 0
          scope_lines = []
          for peer in peers:
            peer_id = str(peer[0])
            pb = int(peer[1])
            pm = int(peer[2])
            pc = int(peer[3])
            pz = int(peer[4])
            pp = int(peer[5])
            ps = int(peer[6])
            phb = str(peer[7] or "")
            phm = str(peer[8] or "")
            phc = str(peer[9] or "")
            phz = str(peer[10] or "")
            php = str(peer[11] or "")
            phs = str(peer[12] or "")

            peer_mismatch = (
              (pb != int(local_hashes.get("bulletins", 0)))
              or (pm != int(local_hashes.get("mail", 0)))
              or (pc != int(local_hashes.get("channels", 0)))
              or (pz != int(local_hashes.get("zork_saves", 0)))
              or (pp != int(local_hashes.get("profiles", 0)))
              or (ps != int(local_hashes.get("game_scores", 0)))
              or (phb and phb != str(local_hashes.get("bulletins_hash", "")))
              or (phm and phm != str(local_hashes.get("mail_hash", "")))
              or (phc and phc != str(local_hashes.get("channels_hash", "")))
              or (phz and phz != str(local_hashes.get("zork_saves_hash", "")))
              or (php and php != str(local_hashes.get("profiles_hash", "")))
              or (phs and phs != str(local_hashes.get("game_scores_hash", "")))
            )
            if peer_mismatch:
              mismatch_count += 1
            scopes = scopes_by_peer.get(peer_id, [])
            scope_text = ", ".join(scopes) if scopes else "none"
            scope_lines.append(f"{peer_id} -> {scope_text}")

          snapshot["mismatch"] = mismatch_count > 0
          snapshot["mismatch_count"] = mismatch_count
          snapshot["status_text"] = f"mismatch ({mismatch_count} peer)" if mismatch_count > 0 else "aligned"
          snapshot["scope_lines"] = scope_lines or ["No mismatched scopes detected"]
          return snapshot
        finally:
          conn.close()
      except Exception:
        return snapshot

    def attach_discovered_mqtt_peers(mqtt_links: list[dict]) -> None:
      """Add each broker card's 'discovered_peers' and 'self_node_id'.

      The running BBS notices other nodes from the retained status messages
      on its topic and reports them in the diagnostics snapshot it already
      writes every main-loop pass. Reading that here means the web process
      needs no broker connection of its own, and no second place handling
      MQTT passwords and TLS material.

      Best-effort: a missing or stale snapshot (mesh-bbs stopped, first run
      before one is written) just yields an empty list, and the card falls
      back to the hand-typed peer box it has always had.
      """
      for link in mqtt_links:
        link.setdefault("discovered_peers", [])
        prefix = str(link.get("topic_prefix", "")).strip()
        local_id = str(link.get("local_id", "")).strip()
        link["self_node_id"] = f"mqtt:{prefix}:{local_id}" if prefix and local_id else ""

      snapshot_path = resolve_app_path(os.getenv("BBS_RUNTIME_DIAG_PATH"), "runtime_diagnostics.json")
      snapshot = load_runtime_snapshot(snapshot_path)
      if not isinstance(snapshot, dict):
        return
      radios = snapshot.get("radios")
      if not isinstance(radios, list):
        return

      by_link_name = {
        str(radio.get("name", "")): radio
        for radio in radios
        if isinstance(radio, dict)
      }
      for link in mqtt_links:
        radio = by_link_name.get(f"mqtt{link['index']}")
        if not isinstance(radio, dict):
          continue
        peers = radio.get("discovered_peers")
        if not isinstance(peers, list):
          continue
        # Already-configured peers are shown as such rather than offered
        # again -- adding a duplicate is the obvious next mistake.
        existing = {line.strip() for line in str(link.get("bbs_nodes_text", "")).splitlines() if line.strip()}
        link["discovered_peers"] = [
          {
            "label": str(peer.get("label", "")),
            "node_id": str(peer.get("node_id", "")),
            "last_seen": str(peer.get("last_seen") or ""),
            "reported_age": _peer_reported_age(peer.get("updated_at")),
            "already_peer": str(peer.get("node_id", "")) in existing,
          }
          for peer in peers
          if isinstance(peer, dict) and peer.get("node_id")
        ]

    def get_link_status_list() -> list[dict]:
      """Per-link connection status (name, protocol, connected, reconnecting)
      for every active radio/MQTT link -- shared by the nav-bar status badges'
      /api/status/links poll (see static/js/app.js's initLinkStatus). Generic
      over transport: an MQTT bridge appears here exactly like a radio, since
      server.py's RadioLink/_describe_radio already treat every link type
      uniformly (this is also what makes it trivial to add a future protocol
      here later -- nothing link-type-specific to update on this end).
      """
      interface = get_runtime_interface()
      if interface is not None:
        # web_admin.py sharing a process with the live interface (rare in
        # normal deployments -- mesh-bbs.service and bacon-web-admin.service
        # run as separate processes; this path mainly matters for tests).
        # No multi-link snapshot to read in this case, just the one interface.
        return [{
          "kind": "link",
          "name": "primary",
          "protocol": str(getattr(interface, "protocol_name", interface.__class__.__name__)),
          "connected": bool(getattr(interface, "is_connected", True)),
          "reconnecting": False,
        }]

      snapshot_path = resolve_app_path(os.getenv("BBS_RUNTIME_DIAG_PATH"), "runtime_diagnostics.json")
      snapshot = load_runtime_snapshot(snapshot_path)
      if not isinstance(snapshot, dict):
        return []

      entries = []
      radios_raw = snapshot.get("radios")
      if isinstance(radios_raw, list):
        entries.extend(
          {
            # 'link' carries BBS traffic and has a reconnect path, so the
            # web admin offers a Reconnect button for it.
            "kind": "link",
            "name": str(r.get("name", "primary")),
            "protocol": str(r.get("radio_protocol", "Unknown")),
            "connected": bool(r.get("connected", True)),
            "reconnecting": bool(r.get("reconnecting", False)),
          }
          for r in radios_raw
        )

      services_raw = snapshot.get("services")
      if isinstance(services_raw, list):
        entries.extend(
          {
            # 'service' is informational only -- no sync traffic, no
            # reconnect path (see server._describe_services).
            "kind": "service",
            "name": str(s.get("name", "service")),
            "protocol": str(s.get("protocol", "Unknown")),
            "connected": bool(s.get("connected", False)),
            "reconnecting": bool(s.get("reconnecting", False)),
          }
          for s in services_raw
        )
      return entries

    def build_settings_diagnostics() -> dict[str, str]:
      bbs_nodes, allowed_nodes, sync_interval_minutes, sync_zork_saves = load_sync_settings(app.config["CONFIG_PATH"])
      scope_labels = [
        ("mail", "Mail"),
        ("bulletins", "Bulletins"),
        ("channels", "Channels"),
        ("profiles", "Profiles"),
        ("game_scores", "Game Scores"),
        ("zork_saves", "Zork Saves"),
      ]
      diagnostics = {
        "app_version": app.config.get("DISPLAY_VERSION", "unknown"),
        # Empty when the commit resolved. When it did not, this is the only
        # thing distinguishing one build from the next, so say why rather
        # than showing the same bare version forever.
        "version_note": get_version_resolution_note(),
        "interface_attached": "No",
        "interface_type": "Unavailable",
        "runtime_source": "None",
        "snapshot_updated_at": "Unavailable",
        "mesh_node_count": "Unavailable",
        "local_node_id": "Unavailable",
        "local_short_name": "Unavailable",
        "local_long_name": "Unavailable",
        "radios": [],  # per-radio breakdown for dual-radio bridge mode; always overwritten below
        "bbs_nodes_count": str(len(bbs_nodes)),
        "allowed_nodes_count": str(len(allowed_nodes)),
        "sync_interval_minutes": str(sync_interval_minutes),
        "sync_zork_saves": "Yes" if sync_zork_saves else "No",
        "bbs_nodes_text": ", ".join(bbs_nodes),
        "allowed_nodes_text": ", ".join(allowed_nodes),
        "board_count": str(len(app.config["BULLETIN_BOARDS"])),
        "board_list": ", ".join(app.config["BULLETIN_BOARDS"]),
        "sync_in_progress": "No",
        "sync_progress_percent": "0",
        "sync_completed_items": "0",
        "sync_total_items": "0",
        "sync_remaining_items": "0",
        "sync_current_phase": "never_run",
        "sync_target_nodes_text": "None",
        "sync_last_updated_at": "Unavailable",
        "sync_last_result": "Not yet run",
        "sync_next_run_epoch": "0",
        "peer_sync_status": "Unknown",
        "peer_sync_counts": "No peer status received yet",
        "peer_scope_mismatches": "No peer status received yet",
        "zork_save_peer_mismatches": "No zork save peer mismatches reported",
        "zork_save_tombstones": "No zork save tombstones recorded",
        "zork_save_candidate_resolution": "No resolver activity yet",
        "peer_hash_graph": [],
        "mismatch_retry_summary": "None",
        "mismatch_retry_details": "",
        "db_path": app.config["DB_PATH"],
        "db_size": "Unknown",
        "wal_size": "Unknown",
        "db_total_size": "Unknown",
        "bulletins_count": "Unknown",
        "mail_count": "Unknown",
        "channels_count": "Unknown",
        "zork_saves_count": "Unknown",
        "game_scores_count": "Unknown",
        "connection_events_count": "Unknown",
        "api_mailbox_count": "Unknown",
        "api_mailbox_pending": "Unknown",
        "last_connection_event": "None",
        "error": "",
      }

      # On-disk footprint: the SQLite file plus its WAL/SHM sidecars. Lets an
      # operator watch growth on an unattended node before the SD card fills.
      def _fmt_bytes(n):
        try:
          n = float(n)
        except (TypeError, ValueError):
          return "Unknown"
        for unit in ("B", "KB", "MB", "GB", "TB"):
          if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
          n /= 1024
        return f"{n:.1f} TB"

      try:
        _db_path = app.config["DB_PATH"]
        _db_bytes = os.path.getsize(_db_path) if os.path.exists(_db_path) else 0
        _wal_bytes = os.path.getsize(_db_path + "-wal") if os.path.exists(_db_path + "-wal") else 0
        _shm_bytes = os.path.getsize(_db_path + "-shm") if os.path.exists(_db_path + "-shm") else 0
        diagnostics["db_size"] = _fmt_bytes(_db_bytes)
        diagnostics["wal_size"] = _fmt_bytes(_wal_bytes)
        diagnostics["db_total_size"] = _fmt_bytes(_db_bytes + _wal_bytes + _shm_bytes)
      except Exception:
        pass

      interface = get_runtime_interface()
      snapshot = {}
      if interface is not None:
        diagnostics["interface_attached"] = "Yes"
        diagnostics["interface_type"] = interface.__class__.__name__
        diagnostics["runtime_source"] = "Live interface"
        try:
          nodes = getattr(interface, "nodes", None)
          if isinstance(nodes, dict):
            diagnostics["mesh_node_count"] = str(len(nodes))

          my_info = None
          get_my_info = getattr(interface, "getMyNodeInfo", None)
          if callable(get_my_info):
            my_info = get_my_info()

          if isinstance(my_info, dict):
            node_num = my_info.get("num")
            user = my_info.get("user", {}) if isinstance(my_info.get("user"), dict) else {}
            if node_num is not None:
              diagnostics["local_node_id"] = str(node_num)
            if user.get("id"):
              diagnostics["local_node_id"] = str(user.get("id"))
            if user.get("shortName"):
              diagnostics["local_short_name"] = str(user.get("shortName"))
            if user.get("longName"):
              diagnostics["local_long_name"] = str(user.get("longName"))
        except Exception as exc:
          diagnostics["error"] = f"Runtime diagnostics unavailable: {exc}"
      else:
        snapshot_path = resolve_app_path(os.getenv("BBS_RUNTIME_DIAG_PATH"), "runtime_diagnostics.json")
        snapshot = load_runtime_snapshot(snapshot_path)
        if snapshot:
          diagnostics["runtime_source"] = "Snapshot file"
          diagnostics["snapshot_updated_at"] = str(snapshot.get("updated_at", "Unavailable"))
          diagnostics["interface_attached"] = "Yes" if snapshot.get("interface_attached", False) else "No"
          diagnostics["interface_type"] = str(snapshot.get("interface_type", diagnostics["interface_type"]))

          mesh_node_count = snapshot.get("mesh_node_count")
          if mesh_node_count is not None:
            diagnostics["mesh_node_count"] = str(mesh_node_count)
          if snapshot.get("local_node_id"):
            diagnostics["local_node_id"] = str(snapshot.get("local_node_id"))
          if snapshot.get("local_short_name"):
            diagnostics["local_short_name"] = str(snapshot.get("local_short_name"))
          if snapshot.get("local_long_name"):
            diagnostics["local_long_name"] = str(snapshot.get("local_long_name"))

          if isinstance(snapshot.get("bbs_nodes"), list):
            diagnostics["bbs_nodes_count"] = str(len(snapshot["bbs_nodes"]))
            diagnostics["bbs_nodes_text"] = ", ".join(str(node) for node in snapshot["bbs_nodes"])
          if isinstance(snapshot.get("allowed_nodes"), list):
            diagnostics["allowed_nodes_count"] = str(len(snapshot["allowed_nodes"]))
            diagnostics["allowed_nodes_text"] = ", ".join(str(node) for node in snapshot["allowed_nodes"])

          diagnostics["sync_in_progress"] = "Yes" if snapshot.get("sync_in_progress", False) else "No"
          diagnostics["sync_progress_percent"] = str(snapshot.get("sync_progress_percent", 0))
          diagnostics["sync_completed_items"] = str(snapshot.get("sync_completed_items", 0))
          diagnostics["sync_total_items"] = str(snapshot.get("sync_total_items", 0))
          diagnostics["sync_remaining_items"] = str(snapshot.get("sync_remaining_items", 0))
          diagnostics["sync_current_phase"] = str(snapshot.get("sync_current_phase", "never_run"))
          target_nodes = snapshot.get("sync_target_nodes", [])
          if isinstance(target_nodes, list) and target_nodes:
            diagnostics["sync_target_nodes_text"] = ", ".join(str(node) for node in target_nodes)
          diagnostics["sync_last_updated_at"] = str(snapshot.get("sync_last_updated_at", "Unavailable"))
          diagnostics["sync_last_result"] = str(snapshot.get("sync_last_result", "Not yet run"))
          diagnostics["sync_interval_minutes"] = str(snapshot.get("sync_interval_minutes", diagnostics["sync_interval_minutes"]))
          diagnostics["sync_next_run_epoch"] = str(snapshot.get("sync_next_run_epoch", 0))
          mismatch_retry_at = snapshot.get("sync_mismatch_retry_at", {})
          if isinstance(mismatch_retry_at, dict) and mismatch_retry_at:
            lines = []
            for node in sorted(mismatch_retry_at.keys(), key=str):
              lines.append(f"{node} @ {mismatch_retry_at.get(node)}")
            diagnostics["mismatch_retry_summary"] = f"{len(lines)} peer(s) retried"
            diagnostics["mismatch_retry_details"] = "\n".join(lines)

          if snapshot.get("error"):
            diagnostics["error"] = str(snapshot.get("error"))

      # Per-radio breakdown for dual-radio bridge mode (the snapshot's
      # 'radios' array -- see server.py's write_runtime_diagnostics_snapshot).
      # Falls back to a single entry synthesized from the flat fields above
      # so the template can always iterate diagnostics["radios"], whether
      # this node is running single- or dual-radio, and whether diagnostics
      # came from a live interface or the snapshot file.
      radios_raw = snapshot.get("radios") if isinstance(snapshot, dict) else None
      if isinstance(radios_raw, list) and radios_raw:
        diagnostics["radios"] = [
          {
            "name": str(r.get("name", "primary")),
            "interface_type": str(r.get("interface_type", "Unavailable")),
            "radio_protocol": str(r.get("radio_protocol", "Meshtastic")),
            "connected": "Yes" if r.get("connected", True) else "No",
            "reconnecting": "Yes" if r.get("reconnecting", False) else "No",
            "mesh_node_count": str(r.get("mesh_node_count")) if r.get("mesh_node_count") is not None else "Unavailable",
            "local_node_id": str(r.get("local_node_id") or "Unavailable"),
            "local_short_name": str(r.get("local_short_name") or "Unavailable"),
            "local_long_name": str(r.get("local_long_name") or "Unavailable"),
            "bbs_nodes_count": str(len(r.get("bbs_nodes") or [])),
            "bbs_nodes_text": ", ".join(str(n) for n in (r.get("bbs_nodes") or [])),
            "allowed_nodes_count": str(len(r.get("allowed_nodes") or [])),
            "allowed_nodes_text": ", ".join(str(n) for n in (r.get("allowed_nodes") or [])),
          }
          for r in radios_raw
        ]
      else:
        diagnostics["radios"] = [{
          "name": "primary",
          "interface_type": diagnostics["interface_type"],
          "radio_protocol": str(snapshot.get("radio_protocol", "Meshtastic")) if isinstance(snapshot, dict) else "Meshtastic",
          "connected": diagnostics["interface_attached"],
          "reconnecting": "No",
          "mesh_node_count": diagnostics["mesh_node_count"],
          "local_node_id": diagnostics["local_node_id"],
          "local_short_name": diagnostics["local_short_name"],
          "local_long_name": diagnostics["local_long_name"],
          "bbs_nodes_count": diagnostics["bbs_nodes_count"],
          "bbs_nodes_text": diagnostics["bbs_nodes_text"],
          "allowed_nodes_count": diagnostics["allowed_nodes_count"],
          "allowed_nodes_text": diagnostics["allowed_nodes_text"],
        }]

      try:
        conn = get_db_connection()
        try:
          cursor = conn.cursor()
          cursor.execute("SELECT COUNT(*) FROM bulletins")
          diagnostics["bulletins_count"] = str(cursor.fetchone()[0])
          cursor.execute("SELECT COUNT(*) FROM mail")
          diagnostics["mail_count"] = str(cursor.fetchone()[0])
          cursor.execute("SELECT COUNT(*) FROM channels")
          diagnostics["channels_count"] = str(cursor.fetchone()[0])
          cursor.execute("SELECT COUNT(*) FROM zork_saves")
          diagnostics["zork_saves_count"] = str(cursor.fetchone()[0])
          cursor.execute("SELECT COUNT(*) FROM game_scores")
          diagnostics["game_scores_count"] = str(cursor.fetchone()[0])
          cursor.execute("SELECT COUNT(*) FROM connection_events")
          diagnostics["connection_events_count"] = str(cursor.fetchone()[0])
          try:
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN delivered=0 THEN 1 ELSE 0 END),0) FROM api_mailbox")
            _mb = cursor.fetchone()
            diagnostics["api_mailbox_count"] = str(_mb[0])
            diagnostics["api_mailbox_pending"] = str(_mb[1])
          except Exception:
            diagnostics["api_mailbox_count"] = "0"
            diagnostics["api_mailbox_pending"] = "0"
          cursor.execute(
            "SELECT tombstone_key, deleted_at FROM deleted_sync_tombstones WHERE tombstone_key LIKE 'zork_saves:%' ORDER BY deleted_at DESC, tombstone_key ASC LIMIT 10"
          )
          tombstone_rows = cursor.fetchall()
          if tombstone_rows:
            diagnostics["zork_save_tombstones"] = "\n".join(
              f"{row[0]} @ {row[1]}" for row in tombstone_rows
            )
          cursor.execute("SELECT event_time, message_type, event_text FROM connection_events ORDER BY id DESC LIMIT 1")
          row = cursor.fetchone()
          if row:
            diagnostics["last_connection_event"] = f"{row['event_time']} | {row['message_type']} | {row['event_text']}"

          peer_rows = get_peer_mismatch_snapshot(set(bbs_nodes)).get("rows", [])
          if peer_rows:
            from db_operations import get_local_record_counts
            local_hashes = get_local_record_counts()
            lines = []
            graph_rows = []
            mismatch = False
            for peer in peer_rows:
              peer_id = str(peer[0])
              pb = int(peer[1])
              pm = int(peer[2])
              pc = int(peer[3])
              pz = int(peer[4])
              pp = int(peer[5])
              ps = int(peer[6])
              phb = str(peer[7] or "")
              phm = str(peer[8] or "")
              phc = str(peer[9] or "")
              phz = str(peer[10] or "")
              php = str(peer[11] or "")
              phs = str(peer[12] or "")
              peer_scope_counts = {
                "bulletins": pb,
                "mail": pm,
                "channels": pc,
                "zork_saves": pz,
                "profiles": pp,
                "game_scores": ps,
              }
              peer_scope_hashes = {
                "bulletins": phb,
                "mail": phm,
                "channels": phc,
                "zork_saves": phz,
                "profiles": php,
                "game_scores": phs,
              }
              peer_mismatch = (
                (pb != int(local_hashes.get("bulletins", 0)))
                or (pm != int(local_hashes.get("mail", 0)))
                or (pc != int(local_hashes.get("channels", 0)))
                or (pz != int(local_hashes.get("zork_saves", 0)))
                or (pp != int(local_hashes.get("profiles", 0)))
                or (ps != int(local_hashes.get("game_scores", 0)))
                or (phb and phb != str(local_hashes.get("bulletins_hash", "")))
                or (phm and phm != str(local_hashes.get("mail_hash", "")))
                or (phc and phc != str(local_hashes.get("channels_hash", "")))
                or (phz and phz != str(local_hashes.get("zork_saves_hash", "")))
                or (php and php != str(local_hashes.get("profiles_hash", "")))
                or (phs and phs != str(local_hashes.get("game_scores_hash", "")))
              )
              mismatch = mismatch or peer_mismatch
              status = "MISMATCH" if peer_mismatch else "OK"
              lines.append(
                f"{peer_id} -> B:{pb} M:{pm} C:{pc} Z:{pz} P:{pp} S:{ps} @ {peer[13]} [{status}]"
              )
              scope_rows = []
              for scope_key, scope_label in scope_labels:
                local_count = int(local_hashes.get(scope_key, 0) or 0)
                peer_count = int(peer_scope_counts.get(scope_key, 0) or 0)
                local_hash = str(local_hashes.get(f"{scope_key}_hash", "") or "")
                peer_hash = str(peer_scope_hashes.get(scope_key, "") or "")
                max_count = max(local_count, peer_count, 1)
                scope_rows.append({
                  "key": scope_key,
                  "label": scope_label,
                  "local_count": local_count,
                  "peer_count": peer_count,
                  "local_width": max(8, round((local_count / max_count) * 100)) if local_count > 0 else 0,
                  "peer_width": max(8, round((peer_count / max_count) * 100)) if peer_count > 0 else 0,
                  "count_match": local_count == peer_count,
                  "hash_match": bool(local_hash) and bool(peer_hash) and local_hash == peer_hash,
                })
              graph_rows.append({
                "peer_node_id": peer_id,
                "reported_at": str(peer[13]),
                "scopes": scope_rows,
              })
            diagnostics["peer_sync_status"] = "Mismatch detected" if mismatch else "Counts aligned"
            diagnostics["peer_sync_counts"] = "\n".join(lines)
            mismatch_snapshot = get_peer_mismatch_snapshot(set(bbs_nodes))
            diagnostics["peer_scope_mismatches"] = "\n".join(mismatch_snapshot.get("scope_lines", []))
            save_mismatch_lines = []
            for graph_row in graph_rows:
              for scope_row in graph_row.get("scopes", []):
                if scope_row.get("key") != "zork_saves":
                  continue
                if scope_row.get("count_match") and scope_row.get("hash_match"):
                  continue
                save_mismatch_lines.append(
                  f"{graph_row['peer_node_id']} -> local={scope_row['local_count']} peer={scope_row['peer_count']} | count={'ok' if scope_row['count_match'] else 'diff'} | hash={'ok' if scope_row['hash_match'] else 'diff'}"
                )
            if save_mismatch_lines:
              diagnostics["zork_save_peer_mismatches"] = "\n".join(save_mismatch_lines)
            diagnostics["peer_hash_graph"] = graph_rows
          else:
            diagnostics["peer_sync_status"] = "No peer reports yet"
            diagnostics["peer_sync_counts"] = "No peer status received yet"
            diagnostics["peer_scope_mismatches"] = "No peer status received yet"
            diagnostics["zork_save_peer_mismatches"] = "No zork save peer mismatches reported"
        finally:
          conn.close()
      except Exception as exc:
        diagnostics["error"] = f"Database diagnostics unavailable: {exc}"

      candidate_resolution = snapshot.get("candidate_resolution") if snapshot else None
      if isinstance(candidate_resolution, dict):
        active = candidate_resolution.get("active", [])
        recent = candidate_resolution.get("recent", [])
        candidate_lines = []
        if isinstance(active, list) and active:
          candidate_lines.append("Active:")
          candidate_lines.extend(
            f"{item.get('key', '')} [{item.get('status', '')}] {item.get('responses', 0)}/{item.get('expected', 0)} peer(s)" for item in active
          )
        if isinstance(recent, list) and recent:
          if candidate_lines:
            candidate_lines.append("")
          candidate_lines.append("Recent:")
          candidate_lines.extend(
            f"{item.get('key', '')} -> {item.get('result', '')}" for item in recent[-5:]
          )
        if candidate_lines:
          diagnostics["zork_save_candidate_resolution"] = "\n".join(candidate_lines)

      return diagnostics

    def render_settings_page():
      bbs_nodes, allowed_nodes, sync_interval_minutes, sync_zork_saves = load_sync_settings(app.config["CONFIG_PATH"])
      sync_speed_settings = load_sync_speed_settings(app.config["CONFIG_PATH"])
      sync_runtime_settings = get_sync_runtime_settings()
      diagnostics = build_settings_diagnostics()
      gateway_settings = load_gateway_settings(app.config["CONFIG_PATH"])
      storage_settings = load_storage_settings(app.config["CONFIG_PATH"])
      public_chatter_settings = load_public_chatter_settings(app.config["CONFIG_PATH"])
      subscriber_settings = load_subscriber_settings(app.config["CONFIG_PATH"])
      device_settings = load_device_settings(app.config["CONFIG_PATH"])
      account_settings = load_account_settings(app.config["CONFIG_PATH"])
      mqtt_settings = load_mqtt_settings(app.config["CONFIG_PATH"])
      attach_discovered_mqtt_peers(mqtt_settings)
      sync_peers = load_sync_peers(app.config["CONFIG_PATH"])
      peer_link_targets = load_peer_link_targets(app.config["CONFIG_PATH"])
      link_statuses = get_link_status_list()
      return render_template(
        "settings.html",
        link_statuses=link_statuses,
        title="Settings",
        show_nav=True,
        gateway=gateway_settings,
        storage=storage_settings,
        public_chatter=public_chatter_settings,
        subscribers=subscriber_settings,
        devices=device_settings,
        accounts=account_settings,
        mqtt_links=mqtt_settings,
        sync_peers=sync_peers,
        peer_link_targets=peer_link_targets,
        boards_text=",".join(app.config["BULLETIN_BOARDS"]),
        env_override=bool(os.getenv("BBS_BULLETIN_BOARDS", "").strip()),
        bbs_nodes_text="\n".join(bbs_nodes),
        allowed_nodes_text="\n".join(allowed_nodes),
        sync_interval_minutes=str(sync_interval_minutes),
        sync_zork_saves=sync_zork_saves,
        sync_speed_settings=sync_speed_settings,
        sync_runtime_settings=sync_runtime_settings,
        sync_env_override_flags=get_sync_env_override_flags(),
        runtime_updates_enabled=app.config["RUNTIME_UPDATES_ENABLED"],
        current_username=app.config["ADMIN_USER"],
        username_env_override=app.config["ADMIN_USER_ENV_OVERRIDE"],
        password_env_override=app.config["ADMIN_PASSWORD_ENV_OVERRIDE"],
        diagnostics=diagnostics,
      )

    initialize_db_safety()

    def login_required(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return view_func(*args, **kwargs)

        return wrapped

    @app.before_request
    def enforce_csrf_for_mutations():
      if request.method not in _MUTATING_METHODS:
        return None
      if request.endpoint == "static":
        return None
      if csrf_request_valid():
        return None
      if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "CSRF token missing or invalid"}), 403
      return "CSRF token missing or invalid", 403

    def get_table_config(table: str) -> dict:
        if table not in TABLE_CONFIG:
            raise KeyError(f"Unknown table: {table}")
        return TABLE_CONFIG[table]

    @app.route("/")
    def index():
        if session.get("logged_in"):
            return redirect(url_for("table_list", table="bulletins"))
        return redirect(url_for("login"))

    def _bounded_int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
      try:
        value = int(request.args.get(name, str(default)))
      except (TypeError, ValueError):
        value = default
      return max(minimum, min(maximum, value))

    @app.get("/chatter")
    @login_required
    def public_chatter_page():
      return render_template(
        "public_chatter.html",
        title="Public Chatter",
        title_suffix="Bacon BBS",
        show_nav=True,
        filters=get_public_chatter_filters(),
      )

    @app.get("/fleet")
    @login_required
    def fleet_page():
      return render_template(
        "fleet.html",
        title="Fleet",
        title_suffix="Bacon BBS",
        show_nav=True,
        fleet=build_fleet_view(app.config["CONFIG_PATH"]),
      )

    @app.post("/fleet/apply")
    @login_required
    def fleet_apply():
      """Verify a pasted instruction, then record it.

      The web admin password is the only thing in front of this box, and it
      is plaintext in config.ini -- so the paste is NOT trusted because it
      arrived here. It is trusted only if its signature checks out against a
      key this node already holds, exactly as if it had arrived over the
      air. That is why signing stays on the admin machine: this endpoint can
      relay authority, never mint it.
      """
      blob = (request.form.get("instruction") or "").strip()
      if not blob:
        flash("Paste a signed instruction first.", "error")
        return redirect(url_for("fleet_page"))

      settings = load_fleet_settings(app.config["CONFIG_PATH"])
      if settings.get("updates") == "off":
        flash("Fleet updates are off on this node ([fleet] updates = off), so "
              "the instruction was not stored.", "error")
        return redirect(url_for("fleet_page"))

      try:
        import fleet_update
        from db_operations import last_fleet_issued_at, store_fleet_target
      except Exception:
        flash("This node cannot verify signatures (the cryptography package is "
              "missing), so the instruction was refused.", "error")
        return redirect(url_for("fleet_page"))

      trusted = fleet_update.parse_trusted_keys(settings.get("trusted_keys", ""))
      group = settings.get("group", "")
      try:
        payload = fleet_update.verify_instruction(
          blob, trusted, group, last_issued_at=last_fleet_issued_at(group))
      except fleet_update.FleetVerificationError as exc:
        flash(f"Instruction rejected: {exc}", "error")
        return redirect(url_for("fleet_page"))

      if store_fleet_target(payload, blob):
        request_fleet_apply_trigger()
        flash(f"Target accepted: version {payload.get('v')} "
              f"(commit {str(payload.get('c'))[:12]}). It will be applied and "
              "relayed to peers.", "success")
      else:
        flash("That instruction is not newer than the one already stored.", "error")
      return redirect(url_for("fleet_page"))

    def _save_fleet_config(mutate) -> None:
      """Apply a change to [fleet] in config.ini.

      Read-modify-write of the live file rather than of a cached copy: the
      operator may have edited it by hand, and silently reverting that would
      be worse than any of the changes made here.
      """
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("fleet"):
        config.add_section("fleet")
      mutate(config)
      write_config_file(config, app.config["CONFIG_PATH"])

    @app.post("/fleet/config")
    @login_required
    def fleet_config():
      """Group, update mode and pin. None of these can grant new authority:
      they scope or disable what an already-trusted key may do."""
      group = (request.form.get("group") or "").strip()
      mode = (request.form.get("updates") or "off").strip().lower()
      pin = (request.form.get("pin_commit") or "").strip().lower()

      if mode not in ("auto", "notify", "off"):
        flash("Updates must be auto, notify or off.", "error")
        return redirect(url_for("fleet_page"))
      if pin and not re.fullmatch(r"[0-9a-f]{7,40}", pin):
        flash("Pin commit must be a hex commit sha, or empty.", "error")
        return redirect(url_for("fleet_page"))

      def mutate(config):
        config.set("fleet", "group", group)
        config.set("fleet", "updates", mode)
        config.set("fleet", "pin_commit", pin)

      _save_fleet_config(mutate)
      logging.warning("Fleet config changed via web admin: group=%r updates=%r "
                      "pin=%r", group, mode, pin[:12])
      flash("Fleet settings saved. Restart the BBS service for them to take "
            "effect.", "success")
      return redirect(url_for("fleet_page"))

    @app.post("/fleet/keys/add")
    @login_required
    def fleet_key_add():
      """Trust a new signing key.

      This is the only setting on the page that MINTS authority rather than
      scoping it: whoever holds the matching private key can from now on run
      code on this node. So it needs an explicit acknowledgement, it shows
      the fingerprint actually derived from the key rather than whatever the
      paste claimed, and it is logged at warning level.
      """
      entry = (request.form.get("trusted_key") or "").strip()
      confirmed = (request.form.get("confirm") or "").strip() == "1"
      if not entry:
        flash("Paste a public key entry first.", "error")
        return redirect(url_for("fleet_page"))
      if not confirmed:
        flash("Trusting a key lets its holder run code on this node. Tick the "
              "confirmation box to continue.", "error")
        return redirect(url_for("fleet_page"))

      try:
        from fleet_update import parse_trusted_keys, public_key_entry
      except Exception:
        flash("This node cannot validate keys (the cryptography package is "
              "missing).", "error")
        return redirect(url_for("fleet_page"))

      parsed = parse_trusted_keys(entry)
      if not parsed:
        flash("That is not a usable public key entry. It should look like "
              "fkabc123:<base64>, from `fleet_sign.py --show-pubkey`.", "error")
        return redirect(url_for("fleet_page"))

      settings = load_fleet_settings(app.config["CONFIG_PATH"])
      existing = parse_trusted_keys(settings.get("trusted_keys", ""))
      added = {k: v for k, v in parsed.items() if k not in existing}
      if not added:
        flash("That key is already trusted.", "success")
        return redirect(url_for("fleet_page"))

      merged = {**existing, **added}
      # Rebuilt from the parsed keys, so a hand-edited id cannot end up
      # stored: the entry written is always derived from the key itself.
      canonical = ",".join(public_key_entry(raw) for raw in merged.values())

      def mutate(config):
        config.set("fleet", "trusted_keys", canonical)

      _save_fleet_config(mutate)
      for key_id in added:
        logging.warning("Fleet: trusted key %s ADDED via web admin. Its holder "
                        "can now direct this node to run any commit.", key_id)
      flash(f"Now trusting {', '.join(sorted(added))}. Restart the BBS service "
            "for it to take effect.", "success")
      return redirect(url_for("fleet_page"))

    @app.post("/fleet/keys/remove")
    @login_required
    def fleet_key_remove():
      """Revoking is always safe, so it needs no confirmation."""
      key_id = (request.form.get("key_id") or "").strip().lower()
      try:
        from fleet_update import parse_trusted_keys, public_key_entry
      except Exception:
        flash("This node cannot validate keys.", "error")
        return redirect(url_for("fleet_page"))

      settings = load_fleet_settings(app.config["CONFIG_PATH"])
      remaining = {k: v for k, v in
                   parse_trusted_keys(settings.get("trusted_keys", "")).items()
                   if k != key_id}
      canonical = ",".join(public_key_entry(raw) for raw in remaining.values())

      def mutate(config):
        config.set("fleet", "trusted_keys", canonical)

      _save_fleet_config(mutate)
      logging.warning("Fleet: trusted key %s removed via web admin.", key_id)
      flash(f"No longer trusting {key_id}. Restart the BBS service for it to "
            "take effect.", "success")
      return redirect(url_for("fleet_page"))

    @app.get("/api/public/chatter")
    @login_required
    def public_chatter_api():
      channel_index = request.args.get("channel", "").strip()
      try:
        parsed_channel = int(channel_index) if channel_index else None
      except ValueError:
        return jsonify({"ok": False, "error": "channel must be a numeric index"}), 400
      result = get_public_chatter_history(
        hours=_bounded_int_arg("hours", 24, 1, 168),
        limit=None,
        network=request.args.get("network", ""),
        channel_index=parsed_channel,
        capture_node_id=request.args.get("capture_node", ""),
        search_query=request.args.get("q", ""),
      )
      return jsonify({"ok": True, **result})

    # -- Client emulator ------------------------------------------------
    #
    # Types at the BBS the way a mesh user would, driving the real
    # message_processing.process_message rather than a stand-in. bbs_emulator
    # is imported lazily inside each handler: it reaches message_processing,
    # which imports the meshtastic package, and the web admin is a separate
    # service that otherwise starts without a radio library present.

    def _emulator_session_or_error():
      import bbs_emulator

      token = (request.get_json(silent=True) or {}).get("token") \
          if request.method == "POST" else request.args.get("token", "")
      session = bbs_emulator.get_session(token)
      if session is None:
        return None, (jsonify({
          "ok": False,
          "error": "That session has expired. Start a new one.",
        }), 410)
      return session, None

    def _emulator_state(session):
      state = session.menu_state() or {}
      return {
        "token": session.token,
        "node_id": session.sender_node_id,
        "node_num": session.sender_id,
        "label": session.label,
        "acting_as_real": session.acting_as_real,
        "max_text_bytes": session.interface.max_text_bytes,
        "menu": {
          "command": state.get("command"),
          "step": state.get("step"),
        },
      }

    @app.get("/emulator")
    @login_required
    def emulator_page():
      import bbs_emulator

      return render_template(
        "emulator.html",
        title="Emulator",
        title_suffix="Bacon BBS",
        show_nav=True,
        roster=bbs_emulator.roster_choices(),
        default_max_bytes=bbs_emulator.DEFAULT_MAX_TEXT_BYTES,
      )

    @app.post("/api/emulator/session")
    @login_required
    def emulator_start_session():
      import bbs_emulator

      payload = request.get_json(silent=True) or {}
      node_id = str(payload.get("node_id") or "").strip()
      # Acting as a real node writes to the shared database under that
      # node's identity: mail is genuinely from them and a Zork autosave
      # overwrites their real save. The page asks first; this refuses to
      # take the caller's word for it having done so.
      if node_id and not payload.get("confirm_act_as"):
        return jsonify({
          "ok": False,
          "error": "Acting as a real node needs an explicit confirmation.",
        }), 400
      try:
        max_bytes = int(payload.get("max_text_bytes")
                        or bbs_emulator.DEFAULT_MAX_TEXT_BYTES)
      except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "max_text_bytes must be a number"}), 400
      session = bbs_emulator.start_session(
        node_id=node_id or None,
        short_name=payload.get("short_name"),
        max_text_bytes=max(32, min(1024, max_bytes)),
      )
      return jsonify({"ok": True, "session": _emulator_state(session)})

    @app.post("/api/emulator/send")
    @login_required
    def emulator_send():
      session, error = _emulator_session_or_error()
      if error is not None:
        return error
      text = str((request.get_json(silent=True) or {}).get("text") or "")
      if not text.strip():
        return jsonify({"ok": False, "error": "Nothing to send"}), 400
      chunks, failure = session.send(text)
      return jsonify({
        "ok": True,
        "chunks": chunks,
        "error": failure,
        "session": _emulator_state(session),
      })

    @app.get("/api/emulator/poll")
    @login_required
    def emulator_poll():
      # Ask Nomad answers from a worker thread seconds to a minute after the
      # question returned. Without this the page shows the slow ack and then
      # nothing, which looks exactly like a broken gateway.
      session, error = _emulator_session_or_error()
      if error is not None:
        return error
      return jsonify({
        "ok": True,
        "chunks": session.drain(),
        "session": _emulator_state(session),
      })

    @app.post("/api/emulator/reset")
    @login_required
    def emulator_reset():
      import bbs_emulator

      session, error = _emulator_session_or_error()
      if error is not None:
        return error
      bbs_emulator.reset_session(session.token)
      return jsonify({"ok": True, "session": _emulator_state(session)})

    @app.post("/api/emulator/end")
    @login_required
    def emulator_end():
      import bbs_emulator

      token = str((request.get_json(silent=True) or {}).get("token") or "")
      return jsonify({"ok": bbs_emulator.end_session(token)})

    @app.route("/mesh-ui/")
    @app.route("/mesh-ui/<path:filename>")
    @login_required
    def mesh_ui_index(filename="index.html"):
        dist_path = app.config["MESH_UI_DIST_PATH"]
        if not os.path.isdir(dist_path):
            return render_template_string(
                BASE_TEMPLATE,
                show_nav=True,
                content=(
                    '<div class="card">'
                    '<h2>Meshtastic Web UI</h2>'
                    '<p>The Meshtastic web client has not been built yet.</p>'
                    '<p>Run <code>setup_mesh_ui.ps1</code> (Windows) or '
                    '<code>setup_mesh_ui.sh</code> (Linux) to clone and build it.</p>'
                    '</div>'
                ),
            )
        return send_from_directory(dist_path, filename)

    @app.route("/api/mesh-ui/node-config")
    @login_required
    def mesh_ui_node_config():
        config = read_config_file(app.config["CONFIG_PATH"])
        iface_type = config.get("interface", "type", fallback="serial").strip()
        hostname = config.get("interface", "hostname", fallback="").strip()
        port = config.get("interface", "port", fallback="").strip()
        return jsonify({
            "interface_type": iface_type,
            "hostname": hostname if iface_type == "tcp" else None,
            "serial_port": port if iface_type == "serial" else None,
        })

    @app.get("/api/csrf-token")
    def api_csrf_token():
      return jsonify({"csrf_token": get_csrf_token()})

    @app.get("/api/quick-search")
    @login_required
    def api_quick_search():
      q = request.args.get("q", "").strip()
      if not q or len(q) > 200:
        return jsonify({"results": []})
      results = []
      like = f"%{q}%"
      with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
          cursor.execute(
            "SELECT id, board, subject, sender_short_name FROM bulletins "
            "WHERE subject LIKE ? OR sender_short_name LIKE ? OR content LIKE ? ORDER BY id DESC LIMIT 8",
            (like, like, like),
          )
          for row in cursor.fetchall():
            results.append({"type": "bulletin", "id": row["id"], "label": f"[{row['board']}] {row['subject']}", "sub": row["sender_short_name"], "url": f"/bulletins/{row['id']}/edit"})
        except Exception:
          pass
        try:
          cursor.execute(
            "SELECT id, sender_short_name, recipient, subject FROM mail "
            "WHERE subject LIKE ? OR sender_short_name LIKE ? OR recipient LIKE ? ORDER BY id DESC LIMIT 8",
            (like, like, like),
          )
          for row in cursor.fetchall():
            results.append({"type": "mail", "id": row["id"], "label": f"Mail: {row['subject']}", "sub": f"{row['sender_short_name']} → {row['recipient']}", "url": f"/mail/{row['id']}/edit"})
        except Exception:
          pass
        try:
          cursor.execute(
            "SELECT id, name, url FROM channels WHERE name LIKE ? OR url LIKE ? ORDER BY id DESC LIMIT 6",
            (like, like),
          )
          for row in cursor.fetchall():
            results.append({"type": "channel", "id": row["id"], "label": f"Channel: {row['name']}", "sub": row["url"], "url": f"/channels/{row['id']}/edit"})
        except Exception:
          pass
      return jsonify({"results": results})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == app.config["ADMIN_USER"] and password == app.config["ADMIN_PASSWORD"]:
                session["logged_in"] = True
                flash("Login successful.", "success")
                return redirect(url_for("table_list", table="bulletins"))
            flash("Invalid username or password.", "error")

        return render_template("login.html", title="Login", show_nav=False)

    @app.route("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
      if request.method == "POST":
        section = request.form.get("settings_section", "").strip().lower()

        if section == "boards":
          update_board_settings(request.form.get("bulletin_boards", ""))
          return redirect(url_for("settings_page") + "#boards")

        if section == "devices":
          errors = save_device_settings(request.form)
          for error in errors:
            flash(error, "error")
          if not errors:
            flash("Device settings saved. Restart the mesh-bbs service for the change to take effect.", "success")
          return redirect(url_for("settings_page") + "#devices")

        if section == "accounts":
          save_account_settings(request.form)
          flash("Account linking settings saved.", "success")
          return redirect(url_for("settings_page") + "#accounts")

        if section == "add_peer":
          add_sync_peer(request.form.get("peer_section", ""), request.form.get("peer_node_id", ""))
          return redirect(url_for("settings_page") + "#sync")

        if section == "remove_peers":
          remove_sync_peers(request.form.getlist("remove_peer"))
          return redirect(url_for("settings_page") + "#sync")

        if section == "reload_links":
          request_links_reload_trigger()
          flash(
            "Reloading links from config.ini — newly added brokers are opened, removed "
            "ones closed, and edited ones reconnected. No restart needed.",
            "success",
          )
          return redirect(url_for("settings_page") + "#links")

        if section == "reconnect_link":
          link_name = request.form.get("link_name", "").strip()
          if not link_name:
            flash("Pick a link to reconnect.", "error")
          else:
            request_link_reconnect_trigger(link_name)
            target = "every link" if link_name == "all" else f"link '{link_name}'"
            flash(
              f"Reconnect queued for {target}. The connection drops and re-establishes "
              "within a few seconds -- the BBS and all other links keep running.",
              "success",
            )
          return redirect(url_for("settings_page") + "#links")

        if section == "mqtt":
          errors = save_mqtt_settings(request.form)
          for error in errors:
            flash(error, "error")
          if not errors:
            # Applies live: the server opens newly-added brokers, drops
            # removed ones, and reconnects edited ones. No restart -- see
            # server.reload_links_from_config.
            request_links_reload_trigger()
            flash(
              "MQTT bridge settings saved and being applied to the running service "
              "now — no restart needed. Watch Links & Services for the result.",
              "success",
            )
          return redirect(url_for("settings_page") + "#mqtt")

        if section == "sync":
          update_sync_settings(
            request.form.get("bbs_nodes") if "bbs_nodes" in request.form else None,
            request.form.get("allowed_nodes", ""),
            request.form.get("sync_interval_minutes", "5"),
            request.form.get("sync_zork_saves", ""),
            request.form.get("sync_turbo", ""),
            request.form.get("sync_pause_seconds", ""),
            request.form.get("hash_repair_pause_seconds", ""),
            request.form.get("full_sync_delay_ms", ""),
          )
          return redirect(url_for("settings_page") + "#sync")

        if section == "manual_sync":
          request_manual_sync_trigger()
          flash("Manual sync requested. The server will start a sync cycle shortly.", "success")
          return redirect(url_for("settings_page") + "#sync")

        if section == "force_check":
          request_force_check_trigger()
          flash("Mismatch check requested. The server will run targeted hash checks shortly.", "success")
          return redirect(url_for("settings_page") + "#sync")

        if section == "resolve_zork_save":
          user_id = request.form.get("resolve_user_id", "").strip()
          game_id = request.form.get("resolve_game_id", "").strip() or "zork1"
          if not user_id:
            flash("User ID is required for save resolution.", "error")
            return redirect(url_for("settings_page") + "#sync")
          request_zork_save_resolve_trigger(user_id, game_id)
          flash(f"Best-candidate save resolution queued for {user_id}:{game_id}.", "success")
          return redirect(url_for("settings_page") + "#sync")

        if section == "peer_resync":
          peer_node_id = request.form.get("peer_node_id", "").strip()
          if not peer_node_id:
            flash("Peer node ID is required.", "error")
            return redirect(url_for("settings_page") + "#sync")
          request_peer_resync_trigger(peer_node_id)
          flash(f"Full resync queued for peer {peer_node_id}. The server will initiate a complete database push to that peer shortly.", "success")
          return redirect(url_for("settings_page") + "#sync")

        if section == "wipe_database":
          confirmation = request.form.get("wipe_confirmation", "").strip()
          if confirmation != "WIPE DATABASE":
            flash("Database wipe cancelled. Type WIPE DATABASE exactly to confirm.", "error")
            return redirect(url_for("settings_page") + "#danger")
          wipe_database_contents()
          flash("Local database wiped.", "success")
          return redirect(url_for("settings_page") + "#danger")

        if section == "gateway":
          save_gateway_settings(request.form)
          flash("API gateway settings saved.", "success")
          return redirect(url_for("settings_page") + "#gateway")

        if section == "subscribers":
          save_subscriber_settings(request.form)
          flash("Subscriber nodes saved. These nodes can pull (WANT/HASHMISS) but are not push-synced to.", "success")
          return redirect(url_for("settings_page") + "#sync")

        if section == "storage":
          save_storage_settings(request.form)
          flash("Storage cap saved. 0 disables it; otherwise the oldest content is pruned mesh-wide to stay under the cap.", "success")
          return redirect(url_for("settings_page") + "#storage")

        if section == "public_chatter":
          errors = save_public_chatter_settings(request.form)
          for error in errors:
            flash(error, "error")
          if not errors:
            flash("Public chatter monitoring settings saved and will hot-reload shortly.", "success")
          return redirect(url_for("settings_page") + "#public-chatter")

        if section == "admin":
          changed = update_admin_settings(
            request.form.get("current_password", ""),
            request.form.get("new_username", "").strip(),
            request.form.get("new_password", "").strip(),
            request.form.get("confirm_password", "").strip(),
          )
          if changed:
            return redirect(url_for("logout"))
          return redirect(url_for("settings_page") + "#admin")

        flash("Unknown settings section.", "error")
        return redirect(url_for("settings_page"))

      return render_settings_page()

    @app.route("/settings/boards", methods=["GET", "POST"])
    @login_required
    def board_settings():
      if request.method == "POST":
        update_board_settings(request.form.get("bulletin_boards", ""))
      return redirect(url_for("settings_page") + "#boards")

    @app.route("/settings/sync", methods=["GET", "POST"])
    @login_required
    def sync_settings():
      if request.method == "POST":
        update_sync_settings(
          request.form.get("bbs_nodes") if "bbs_nodes" in request.form else None,
          request.form.get("allowed_nodes", ""),
          request.form.get("sync_interval_minutes", "5"),
          request.form.get("sync_zork_saves", ""),
          request.form.get("sync_turbo", ""),
          request.form.get("sync_pause_seconds", ""),
          request.form.get("hash_repair_pause_seconds", ""),
          request.form.get("full_sync_delay_ms", ""),
        )
      return redirect(url_for("settings_page") + "#sync")

    @app.route("/settings/admin", methods=["GET", "POST"])
    @login_required
    def admin_settings():
      if request.method == "POST":
        changed = update_admin_settings(
          request.form.get("current_password", ""),
          request.form.get("new_username", "").strip(),
          request.form.get("new_password", "").strip(),
          request.form.get("confirm_password", "").strip(),
        )
        if changed:
          return redirect(url_for("logout"))
      return redirect(url_for("settings_page") + "#admin")

    @app.get("/api/status/links")
    @login_required
    def api_status_links():
      """Lightweight per-link connection status for the nav-bar status
      badges -- polled every few seconds by static/js/app.js's
      initLinkStatus(), separate from the heavier /api/sync/status (sync
      progress) and Settings > Diagnostics (full per-radio breakdown)."""
      return jsonify({"links": get_link_status_list()})

    @app.get("/api/sync/status")
    @login_required
    def api_sync_status():
      snapshot_path = os.getenv("BBS_RUNTIME_DIAG_PATH", "runtime_diagnostics.json")
      snapshot = load_runtime_snapshot(snapshot_path)

      _, _, config_interval_minutes, _ = load_sync_settings(app.config["CONFIG_PATH"])
      progress_percent = int(snapshot.get("sync_progress_percent", 0)) if snapshot else 0
      in_progress = bool(snapshot.get("sync_in_progress", False)) if snapshot else False
      phase = str(snapshot.get("sync_current_phase", "never_run")) if snapshot else "never_run"
      next_run_epoch = int(snapshot.get("sync_next_run_epoch", 0)) if snapshot else 0
      interval_minutes = int(snapshot.get("sync_interval_minutes", config_interval_minutes)) if snapshot else config_interval_minutes
      last_trigger_reason = str(snapshot.get("sync_last_trigger_reason", "scheduled")) if snapshot else "scheduled"
      now_epoch = int(datetime.utcnow().timestamp())
      seconds_until_next = max(next_run_epoch - now_epoch, 0) if next_run_epoch > 0 else 0

      peer_mismatch = False
      peer_status_text = "no peer reports"
      mismatch_snapshot = get_peer_mismatch_snapshot()
      peer_mismatch = bool(mismatch_snapshot.get("mismatch", False))
      peer_status_text = str(mismatch_snapshot.get("status_text", "no peer reports"))

      return jsonify({
        "in_progress": in_progress,
        "progress_percent": progress_percent,
        "phase": phase,
        "next_run_epoch": next_run_epoch,
        "seconds_until_next": seconds_until_next,
        "sync_interval_minutes": interval_minutes,
        "last_trigger_reason": last_trigger_reason,
        "peer_mismatch": peer_mismatch,
        "peer_status_text": peer_status_text,
      })

    @app.get("/api/sync/mismatches")
    @login_required
    def api_sync_mismatches():
      expected_nodes, _, _, _ = load_sync_settings(app.config["CONFIG_PATH"])
      snapshot = get_peer_mismatch_snapshot(set(expected_nodes))
      peers_payload = []
      scope_map = {}
      try:
        from db_operations import get_mismatched_peer_scopes
        scope_map = get_mismatched_peer_scopes(set(expected_nodes))
      except Exception:
        scope_map = {}

      for peer in snapshot.get("rows", []):
        peer_id = str(peer[0])
        peers_payload.append({
          "peer_node_id": peer_id,
          "reported_at": str(peer[13]),
          "counts": {
            "bulletins": int(peer[1]),
            "mail": int(peer[2]),
            "channels": int(peer[3]),
            "zork_saves": int(peer[4]),
            "profiles": int(peer[5]),
            "game_scores": int(peer[6]),
          },
          "mismatched_scopes": list(scope_map.get(peer_id, [])),
        })

      return jsonify({
        "summary": {
          "mismatch": bool(snapshot.get("mismatch", False)),
          "mismatch_count": int(snapshot.get("mismatch_count", 0)),
          "status_text": str(snapshot.get("status_text", "no peer reports")),
        },
        "peers": peers_payload,
      })

    @app.post("/api/sync/manual")
    @login_required
    def api_sync_manual():
      request_manual_sync_trigger()
      return jsonify({"ok": True, "message": "Manual sync requested"})

    @app.get("/api/sync/sessions")
    @login_required
    def api_sync_sessions():
      from db_operations import get_sync_sessions
      since = min(max(int(request.args.get("since_seconds", 86400)), 300), 604800)
      data = get_sync_sessions(since_seconds=since)
      return jsonify(data)

    @app.get("/api/sync/session-history")
    @login_required
    def api_sync_session_history():
      from db_operations import get_sync_session_history
      since = min(max(int(request.args.get("since_seconds", 30 * 24 * 3600)), 3600), 365 * 24 * 3600)
      limit = min(max(int(request.args.get("limit", 200)), 1), 1000)
      return jsonify({"sessions": get_sync_session_history(since_seconds=since, limit=limit)})

    @app.post("/api/sync/force-check")
    @login_required
    def api_sync_force_check():
      request_force_check_trigger()
      return jsonify({"ok": True, "message": "Force mismatch check requested"})

    @app.post("/api/sync/resync-peer")
    @login_required
    def api_sync_resync_peer():
      data = request.get_json(silent=True) or {}
      peer_node_id = str(data.get("peer_node_id", "")).strip()
      if not peer_node_id:
        return jsonify({"ok": False, "error": "peer_node_id required"}), 400
      request_peer_resync_trigger(peer_node_id)
      return jsonify({"ok": True, "message": f"Full resync queued for peer {peer_node_id}"})

    @app.post("/api/sync/resolve-zork-save")
    @login_required
    def api_sync_resolve_zork_save():
      data = request.get_json(silent=True) or {}
      user_id = str(data.get("user_id", "")).strip()
      game_id = str(data.get("game_id", "")).strip() or "zork1"
      if not user_id:
        return jsonify({"ok": False, "error": "user_id required"}), 400
      request_zork_save_resolve_trigger(user_id, game_id)
      return jsonify({"ok": True, "message": f"Best-candidate resolution queued for {user_id}:{game_id}"})

    @app.post("/api/sync/resolve-record")
    @login_required
    def api_sync_resolve_record():
      data = request.get_json(silent=True) or {}
      scope = str(data.get("scope", "")).strip().lower()
      key = str(data.get("key", "")).strip()
      if not scope or not key:
        return jsonify({"ok": False, "error": "scope and key required"}), 400
      request_record_resolve_trigger(scope, key)
      return jsonify({"ok": True, "message": f"Repair queued for {scope}:{key}"})

    @app.post("/sync/resolve-record")
    @login_required
    def resolve_record():
      scope = request.form.get("scope", "").strip().lower()
      key = request.form.get("key", "").strip()
      redirect_to = request.form.get("redirect_to", "").strip()
      if not scope or not key:
        flash("Scope and key are required to resolve a record.", "error")
      else:
        request_record_resolve_trigger(scope, key)
        flash(f"Repair queued for {scope}:{key}.", "success")
      if redirect_to:
        return redirect(redirect_to)
      return redirect(url_for("settings_page") + "#sync")

    @app.get("/api/sync/transmissions")
    @login_required
    def api_sync_transmissions():
      from db_operations import get_sync_transmission_entries, prune_old_sync_transmissions

      prune_old_sync_transmissions(max_rows=10000)

      try:
        since_id = max(0, int(request.args.get("since_id", "0")))
      except ValueError:
        since_id = 0

      try:
        limit = max(1, min(500, int(request.args.get("limit", "200"))))
      except ValueError:
        limit = 200

      entries = get_sync_transmission_entries(
        since_id=since_id,
        limit=limit,
        direction=request.args.get("direction", ""),
        frame_type=request.args.get("frame_type", ""),
        peer_node_id=request.args.get("peer_node_id", ""),
        search_query=request.args.get("search", ""),
      )
      serialized = [serialize_sync_transmission(entry) for entry in entries]
      return jsonify({
        "entries": serialized,
        "last_id": max((entry["id"] for entry in serialized), default=since_id),
      })

    @app.get("/api/sync/progress")
    @login_required
    def api_sync_progress():
      from db_operations import get_sync_progress_data
      try:
        lookback = max(60, min(86400, int(request.args.get("lookback_seconds", "1800"))))
      except ValueError:
        lookback = 1800
      data = get_sync_progress_data(lookback_seconds=lookback)
      return jsonify(data)

    @app.post("/api/reorder/<table>")
    @login_required
    def api_reorder(table: str):
      try:
        data = request.get_json()
        from_id = int(data.get("from_id", 0))
        to_id = int(data.get("to_id", 0))
        
        if table not in TABLE_CONFIG:
          return {"success": False, "error": "Unknown table"}, 400
        
        if from_id == to_id:
          return {"success": True}
        
        with get_db_connection() as conn:
          cursor = conn.cursor()
          
          # Get both rows
          cursor.execute(f"SELECT id FROM {table} WHERE id IN (?, ?)", (from_id, to_id))
          rows = cursor.fetchall()
          
          if len(rows) != 2:
            return {"success": False, "error": "Rows not found"}, 404
          
          # Swap the rows by swapping their content (except id and unique_id)
          cursor.execute(f"SELECT * FROM {table} WHERE id = ?", (from_id,))
          from_row = dict(cursor.fetchone())
          
          cursor.execute(f"SELECT * FROM {table} WHERE id = ?", (to_id,))
          to_row = dict(cursor.fetchone())
          
          # Create a temporary table to facilitate the swap
          cursor.execute("BEGIN IMMEDIATE")
          
          # We'll just update by swapping the data while keeping IDs fixed
          cfg = TABLE_CONFIG[table]
          editable_fields = cfg["editable"]
          
          # Store to_row values temporarily
          from_values = tuple(from_row[f] for f in editable_fields)
          to_values = tuple(to_row[f] for f in editable_fields)
          
          # Update from_row with to_row values
          set_clause = ", ".join([f"{field} = ?" for field in editable_fields])
          cursor.execute(
            f"UPDATE {table} SET {set_clause} WHERE id = ?",
            (*to_values, from_id)
          )
          
          # Update to_row with from_row values
          cursor.execute(
            f"UPDATE {table} SET {set_clause} WHERE id = ?",
            (*from_values, to_id)
          )
          
          conn.commit()
        
        return {"success": True}
      except Exception as e:
        print(f"Reorder error: {e}")
        return {"success": False, "error": str(e)}, 500

    @app.route("/clients")
    @login_required
    def clients_summary():
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(bulletins)")
        bulletin_columns = {row[1] for row in cursor.fetchall()}
        has_bulletin_sender_node = "sender_node_id" in bulletin_columns

        cursor.execute("PRAGMA table_info(connection_events)")
        event_columns = {row[1] for row in cursor.fetchall()}
        has_event_sender_node = "sender_node_id" in event_columns

        cursor.execute(
          (
            """
            SELECT sender_short_name,
                   MAX(sender_node_id) AS sender_node_id,
                   COUNT(*) AS post_count
            FROM bulletins
            WHERE sender_short_name IS NOT NULL AND TRIM(sender_short_name) != ''
            GROUP BY sender_short_name
            ORDER BY post_count DESC, sender_short_name ASC
            """
            if has_bulletin_sender_node
            else
            """
            SELECT sender_short_name,
                   NULL AS sender_node_id,
                   COUNT(*) AS post_count
            FROM bulletins
            WHERE sender_short_name IS NOT NULL AND TRIM(sender_short_name) != ''
            GROUP BY sender_short_name
            ORDER BY post_count DESC, sender_short_name ASC
            """
          )
        )
        rows = cursor.fetchall()
        total_posts = sum(row["post_count"] for row in rows)

        cursor.execute(
          (
            """
            SELECT id, event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text
            FROM connection_events
            ORDER BY id DESC
            LIMIT 120
            """
            if has_event_sender_node
            else
            """
            SELECT id, event_time, sender_num, NULL AS sender_node_id, sender_short_name, to_id, message_type, event_text
            FROM connection_events
            ORDER BY id DESC
            LIMIT 120
            """
          )
        )
        events_desc = cursor.fetchall()

      connection_events = [serialize_connection_event(row) for row in reversed(events_desc)]
      last_event_id = connection_events[-1]["id"] if connection_events else 0

      from db_operations import get_mesh_clients
      mesh_clients = get_mesh_clients()

      return render_template(
        "clients.html",
        title="Clients",
        show_nav=True,
        rows=rows,
        total_posts=total_posts,
        connection_events=connection_events,
        last_event_id=last_event_id,
        mesh_clients=mesh_clients,
      )

    @app.route("/clients/<path:node_id>/profile")
    @login_required
    def client_profile(node_id):
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
          "SELECT user_id, short_name, long_name, first_seen, last_seen, messages_sent, bio "
          "FROM user_profiles WHERE user_id = ?",
          (node_id,),
        )
        profile = cursor.fetchone()
      short = profile["short_name"] if profile else node_id

      # Opportunistic cross-link to the Accounts page: user_profiles is
      # keyed by the NUMERIC node id, linked_nodes by the STRING id, and
      # there's no persisted mapping between them -- best-effort resolve
      # via the live interface's node table (only available when web_admin
      # runs embedded with a runtime_interface, not the common standalone
      # deployment). Silently omitted if resolution isn't possible.
      account_id = None
      try:
        interface = get_runtime_interface()
        if interface is not None:
          from utils import get_node_id_from_num
          from db_operations import get_account_id_for_node
          string_node_id = get_node_id_from_num(int(node_id), interface)
          if string_node_id:
            account_id = get_account_id_for_node(string_node_id)
      except Exception:
        account_id = None

      return render_template(
        "client_profile.html",
        title=f"Profile – {short}",
        show_nav=True,
        profile=profile,
        node_id=node_id,
        linked_account_id=account_id,
      )

    @app.get("/api/connection-events")
    @login_required
    def api_connection_events():
      try:
        since_id = int(request.args.get("since_id", "0"))
      except ValueError:
        since_id = 0

      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
          """
          SELECT id, event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text
          FROM connection_events
          WHERE id > ?
          ORDER BY id ASC
          LIMIT 200
          """,
          (since_id,),
        )
        events = [serialize_connection_event(row) for row in cursor.fetchall()]
      return jsonify({"events": events})

    @app.get("/accounts")
    @login_required
    def accounts_list():
      from db_operations import list_accounts
      rows = list_accounts()
      return render_template("accounts.html", title="Accounts", show_nav=True, rows=rows)

    @app.route("/deleted")
    @login_required
    def deleted_content():
      from db_operations import get_sync_tombstones
      return render_template("deleted.html", title="Deleted Content", show_nav=True,
                             tombstones=get_sync_tombstones())

    @app.post("/deleted/restore")
    @login_required
    def deleted_restore():
      from db_operations import restore_sync_tombstone
      key = request.form.get("tombstone_key", "").strip()
      if not key:
        flash("No record selected.", "error")
      elif restore_sync_tombstone(key):
        # Clearing the tombstone is part of restoring: leaving it would let
        # the next sync pass delete the record again.
        flash("Restored. It will sync back out to peers on the next pass.", "success")
      else:
        flash("Could not restore that record — no stored copy of it.", "error")
      return redirect(url_for("deleted_content"))

    @app.post("/deleted/forget")
    @login_required
    def deleted_forget():
      from db_operations import forget_sync_tombstone
      key = request.form.get("tombstone_key", "").strip()
      if key and forget_sync_tombstone(key):
        flash("Tombstone forgotten. A peer that still has the record may send it back.", "success")
      else:
        flash("No such tombstone.", "error")
      return redirect(url_for("deleted_content"))

    @app.route("/accounts/<account_id>", methods=["GET", "POST"])
    @login_required
    def account_detail(account_id):
      from db_operations import (
        get_account, get_linked_nodes_detail, set_account_alias,
        unlink_node, link_node_to_account, get_account_id_for_node,
      )
      from utils import home_network

      account = get_account(account_id)
      if account is None:
        flash("Account not found.", "error")
        return redirect(url_for("accounts_list"))

      if request.method == "POST":
        action = request.form.get("action", "").strip()
        if action == "set_alias":
          alias = request.form.get("alias", "").strip()[:20]
          if not set_account_alias(account_id, alias):
            flash(f'"{alias}" is already used by another account.', "error")
          else:
            flash(f'Alias set to "{alias}".' if alias else "Alias cleared.", "success")
        elif action == "unlink":
          node_id = request.form.get("node_id", "").strip()
          if node_id and unlink_node(node_id):
            flash(f"Unlinked {node_id}.", "success")
          else:
            flash("Couldn't unlink that device (it may be the account's only one).", "error")
        elif action == "force_link":
          node_id = request.form.get("new_node_id", "").strip()
          if not node_id:
            flash("Node ID is required.", "error")
          elif get_account_id_for_node(node_id) is not None:
            flash(f"{node_id} is already linked to an account.", "error")
          else:
            link_node_to_account(node_id, account_id, home_network(node_id))
            flash(f"Linked {node_id}.", "success")
        else:
          flash("Unknown action.", "error")
        return redirect(url_for("account_detail", account_id=account_id))

      detail = get_linked_nodes_detail(account_id)
      return render_template(
        "account_detail.html",
        title=f"Account {account_id[:8]}",
        show_nav=True,
        account_id=account_id,
        alias=account[1],
        created_at=account[2],
        devices=detail,
      )

    def _describe_configured_device(config, section: str, label: str, runtime_interface_for_hostname=None) -> dict:
      """Build the {device_mode, ...} dict meshtastic_device.html renders for
      ONE radio (section='interface' primary, or 'interface2' secondary in
      dual-radio bridge mode). Shared so the view below can render one or
      two device cards from the same logic."""
      interface_type = config.get(section, "type", fallback="serial").strip().lower()
      device = {"label": label}
      if interface_type.startswith("meshcore_"):
        transport = interface_type.removeprefix("meshcore_")
        endpoint = ""
        if transport == "tcp":
          hostname = config.get(section, "hostname", fallback="").strip()
          tcp_port = config.get(section, "tcp_port", fallback="5000").strip()
          endpoint = f"{hostname}:{tcp_port}" if hostname else f"TCP port {tcp_port}"
        elif transport == "serial":
          endpoint = config.get(section, "port", fallback="auto-detected serial port").strip()
        elif transport == "ble":
          endpoint = config.get(section, "ble_address", fallback="BLE scan").strip() or "BLE scan"
        device.update(device_mode="meshcore", meshcore_transport=transport.upper(), meshcore_endpoint=endpoint)
      elif interface_type == "tcp":
        hostname = config.get(section, "hostname", fallback="").strip()
        if not hostname and runtime_interface_for_hostname is not None:
          hostname = getattr(runtime_interface_for_hostname, "hostname", "") or ""
        device_url = f"http://{hostname}" if hostname else "http://meshtastic.local"
        device.update(device_mode="tcp", device_host=hostname or "meshtastic.local", device_url=device_url)
      else:
        device.update(device_mode="serial")
      return device

    @app.route("/system/meshtastic")
    @login_required
    def meshtastic_device():
      config = read_config_file(app.config["CONFIG_PATH"])
      devices = [_describe_configured_device(config, "interface", "Radio", get_runtime_interface())]
      interface2_type = config.get("interface2", "type", fallback="").strip().lower()
      interface2_enabled = bool(interface2_type) and config.getboolean("interface2", "enabled", fallback=True)
      if interface2_enabled:
        devices[0]["label"] = "Radio 1 (primary)"
        devices.append(_describe_configured_device(config, "interface2", "Radio 2 (secondary, bridge mode)"))
      title = "MeshCore Companion Radio" if devices[0]["device_mode"] == "meshcore" and len(devices) == 1 else "Radio Device"
      return render_template(
        "meshtastic_device.html",
        title=title,
        show_nav=True,
        devices=devices,
        # Back-compat: single-radio deployments' templates/tooling may still
        # reference the old flat vars directly instead of devices[0].
        device_mode=devices[0]["device_mode"],
        meshcore_transport=devices[0].get("meshcore_transport"),
        meshcore_endpoint=devices[0].get("meshcore_endpoint"),
        device_host=devices[0].get("device_host"),
        device_url=devices[0].get("device_url"),
      )

    @app.route("/system/flowchart")
    @login_required
    def system_flowchart():
      with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
          cursor.execute(
            """
            SELECT id, board, sender_short_name, date, subject, content
            FROM bulletins
            ORDER BY id DESC
            LIMIT 60
            """
          )
          recent_bulletins = cursor.fetchall()
        except Exception:
          recent_bulletins = []

        try:
          cursor.execute(
            """
            SELECT id, sender_short_name, recipient, date, subject, content
            FROM mail
            ORDER BY id DESC
            LIMIT 15
            """
          )
          recent_mail = cursor.fetchall()
        except Exception:
          recent_mail = []

        try:
          cursor.execute(
            """
            SELECT id, name, url
            FROM channels
            ORDER BY id DESC
            LIMIT 15
            """
          )
          recent_channels = cursor.fetchall()
        except Exception:
          recent_channels = []

        try:
          cursor.execute(
            """
            SELECT cc.id, c.name AS channel_name, cc.sender_short_name, cc.date, cc.content
            FROM channel_comments cc
            JOIN channels c ON c.id = cc.channel_id
            ORDER BY cc.id DESC
            LIMIT 60
            """
          )
          recent_channel_comments = cursor.fetchall()
        except Exception:
          recent_channel_comments = []

      board_posts = {}
      for row in recent_bulletins:
        board_name = (row["board"] or "Uncategorized").strip() or "Uncategorized"
        if board_name not in board_posts:
          board_posts[board_name] = []

        if len(board_posts[board_name]) >= 4:
          continue

        sender = (row["sender_short_name"] or "unknown").strip() or "unknown"
        subject = (row["subject"] or "").strip()
        content = (row["content"] or "").strip()
        preview = subject if subject else content
        if len(preview) > 34:
          preview = f"{preview[:31]}..."

        board_posts[board_name].append(
          {
            "id": row["id"],
            "sender": sender,
            "date": row["date"],
            "preview": preview,
          }
        )

      topic_branches = []
      for board_name, posts in board_posts.items():
        topic_branches.append({"board": board_name, "posts": posts})

      channel_comment_posts = {}
      for row in recent_channel_comments:
        channel_name = (row["channel_name"] or "Unknown Channel").strip() or "Unknown Channel"
        if channel_name not in channel_comment_posts:
          channel_comment_posts[channel_name] = []

        if len(channel_comment_posts[channel_name]) >= 4:
          continue

        sender = (row["sender_short_name"] or "unknown").strip() or "unknown"
        content = (row["content"] or "").strip()
        preview = content
        if len(preview) > 34:
          preview = f"{preview[:31]}..."

        channel_comment_posts[channel_name].append(
          {
            "id": row["id"],
            "sender": sender,
            "date": row["date"],
            "preview": preview,
          }
        )

      comment_branches = []
      for channel_name, comments in channel_comment_posts.items():
        comment_branches.append({"channel": channel_name, "comments": comments})

      return render_template(
        "flowchart.html",
        title="Documentation",
        show_nav=True,
        recent_bulletins=recent_bulletins,
        recent_mail=recent_mail,
        recent_channels=recent_channels,
        topic_branches=topic_branches,
        comment_branches=comment_branches,
      )

    @app.route("/system/transmissions")
    @login_required
    def system_transmissions():
      from db_operations import get_sync_transmission_entries, get_sync_transmission_stats, prune_old_sync_transmissions

      prune_old_sync_transmissions(max_rows=10000)

      stats_1h  = get_sync_transmission_stats(since_seconds=3600)
      stats_24h = get_sync_transmission_stats(since_seconds=86400)

      # ------------------------------------------------------------------
      # Frame-type → category mapping
      # ------------------------------------------------------------------
      _FRAME_CATEGORIES = {
        # Game data — potentially heavy (ZORKSAVE is chunked binary save files)
        'SCORESYNC':       'Game',
        'ZORKSAVE':        'Game',
        # Content records
        'BULLETIN':        'Content',
        'BULLETINCONT':    'Content',
        'MAIL':            'Content',
        'MAILCONT':        'Content',
        'CHANNEL':         'Content',
        'CHANNELCOMMENT':  'Content',
        'CHANNELCOMMENTCONT': 'Content',
        'DELETE_BULLETIN': 'Content',
        'DELETE_MAIL':     'Content',
        'DELETE_CHANNELCOMMENT': 'Content',
        # User profiles
        'PROFILESYNC':     'Profile',
        # Sync protocol overhead
        'SYNCSTATE':       'Protocol',
        'HASHREQ':         'Protocol',
        'HASHREC':         'Protocol',
        'HASHZ':           'Protocol',
        'HASHEND':         'Protocol',
        'HASHMISS':        'Protocol',
        'HASHMISS_TOMB':   'Protocol',
      }
      _CATEGORY_COLORS = {
        'Game':     '#e63946',
        'Content':  '#2a9d8f',
        'Profile':  '#e9c46a',
        'Protocol': '#457b9d',
      }
      _CATEGORY_ORDER = ['Game', 'Content', 'Profile', 'Protocol', 'Other']

      def _direction_html(stats):
        total_tx = stats.get('total_transmissions', 0)
        total_bytes = stats.get('total_bytes', 0)
        direction_counts = stats.get('direction_breakdown', {})
        direction_bytes = stats.get('direction_bytes', {})
        tx_count = int(direction_counts.get('tx', 0) or 0)
        rx_count = int(direction_counts.get('rx', 0) or 0)
        tx_bytes = int(direction_bytes.get('tx', 0) or 0)
        rx_bytes = int(direction_bytes.get('rx', 0) or 0)

        return f"""
        <div style=\"display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 16px 0;\">
          <div style=\"border:1px solid var(--card-border);border-radius:6px;padding:10px 12px;background:var(--card-bg);min-width:170px;\">
            <div style=\"font-size:0.8em;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;\">Sent to peers</div>
            <div style=\"font-size:1.2em;font-weight:700;\">{tx_count:,} frames</div>
            <div style=\"font-size:0.9em;color:var(--muted);\">{tx_bytes:,} bytes</div>
          </div>
          <div style=\"border:1px solid var(--card-border);border-radius:6px;padding:10px 12px;background:var(--card-bg);min-width:170px;\">
            <div style=\"font-size:0.8em;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;\">Received from peers</div>
            <div style=\"font-size:1.2em;font-weight:700;\">{rx_count:,} frames</div>
            <div style=\"font-size:0.9em;color:var(--muted);\">{rx_bytes:,} bytes</div>
          </div>
          <div style=\"border:1px solid var(--card-border);border-radius:6px;padding:10px 12px;background:var(--card-bg);min-width:170px;\">
            <div style=\"font-size:0.8em;color:var(--muted);text-transform:uppercase;letter-spacing:0.04em;\">Combined total</div>
            <div style=\"font-size:1.2em;font-weight:700;\">{total_tx:,} frames</div>
            <div style=\"font-size:0.9em;color:var(--muted);\">{total_bytes:,} bytes</div>
          </div>
        </div>"""

      def _category_html(stats):
        total_bytes = stats.get('total_bytes', 0)
        total_tx = stats.get('total_transmissions', 0)
        fb = stats.get('frame_breakdown', {})
        fby = stats.get('frame_bytes', {})

        if not total_tx:
          return "<p><em>No transmissions recorded in this period.</em></p>"

        cat_bytes = {}
        cat_counts = {}
        for ft, cnt in fb.items():
          cat = _FRAME_CATEGORIES.get(ft, 'Other')
          cat_bytes[cat] = cat_bytes.get(cat, 0) + fby.get(ft, 0)
          cat_counts[cat] = cat_counts.get(cat, 0) + cnt

        rows = ""
        for cat in _CATEGORY_ORDER:
          if cat not in cat_bytes:
            continue
          byt = cat_bytes[cat]
          cnt = cat_counts[cat]
          b_pct = round(byt / total_bytes * 100, 1) if total_bytes else 0
          c_pct = round(cnt / total_tx * 100, 1) if total_tx else 0
          color = _CATEGORY_COLORS.get(cat, '#aaa')
          rows += f"""
          <tr style="border-bottom:1px solid var(--table-border);">
            <td style="padding:8px 4px;">
              <span style="display:inline-block;width:12px;height:12px;background:{color};border-radius:2px;margin-right:6px;vertical-align:middle;"></span>
              <strong>{cat}</strong>
            </td>
            <td style="padding:8px 4px;text-align:right;">{cnt:,}</td>
            <td style="padding:8px 4px;text-align:right;">{c_pct}%</td>
            <td style="padding:8px 4px;text-align:right;">{byt:,}</td>
            <td style="padding:8px 4px;text-align:right;font-weight:bold;">{b_pct}%</td>
            <td style="padding:8px 4px;min-width:150px;">
              <div style="background:{color};height:14px;width:{b_pct}%;border-radius:3px;display:inline-block;vertical-align:middle;"></div>
            </td>
          </tr>"""

        return f"""
        <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
          <thead>
            <tr style="background:var(--table-header-bg);border-bottom:2px solid var(--table-border);">
              <th style="padding:8px 4px;text-align:left;">Category</th>
              <th style="padding:8px 4px;text-align:right;"># Frames</th>
              <th style="padding:8px 4px;text-align:right;">% Count</th>
              <th style="padding:8px 4px;text-align:right;">Bytes</th>
              <th style="padding:8px 4px;text-align:right;">% Bytes</th>
              <th style="padding:8px 4px;">Bytes share</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
          <tfoot>
            <tr style="border-top:2px solid var(--table-border);font-weight:bold;">
              <td style="padding:8px 4px;">TOTAL</td>
              <td style="padding:8px 4px;text-align:right;">{total_tx:,}</td>
              <td style="padding:8px 4px;text-align:right;">100%</td>
              <td style="padding:8px 4px;text-align:right;">{total_bytes:,}</td>
              <td style="padding:8px 4px;text-align:right;">100%</td>
              <td></td>
            </tr>
          </tfoot>
        </table>"""

      def _breakdown_html(stats):
        total_tx = stats.get('total_transmissions', 0)
        total_bytes = stats.get('total_bytes', 0)
        fb = stats.get('frame_breakdown', {})
        fby = stats.get('frame_bytes', {})
        node_bd = stats.get('node_breakdown', {})

        if not total_tx:
          return "<p><em>No transmissions recorded in this period.</em></p>"

        types_sorted = sorted(fb.keys(), key=lambda t: fby.get(t, 0), reverse=True)

        rows_html = ""
        for ft in types_sorted:
          cnt = fb[ft]
          byt = fby.get(ft, 0)
          cnt_pct = round(cnt / total_tx * 100, 1) if total_tx else 0
          byte_pct = round(byt / total_bytes * 100, 1) if total_bytes else 0
          cat = _FRAME_CATEGORIES.get(ft, 'Other')
          color = _CATEGORY_COLORS.get(cat, '#aaa')
          rows_html += f"""
          <tr style="border-bottom:1px solid var(--table-border);">
            <td style="padding:8px 4px;font-weight:bold;white-space:nowrap;">
              <span style="display:inline-block;width:8px;height:8px;background:{color};border-radius:50%;margin-right:5px;vertical-align:middle;"></span>
              {ft}
            </td>
            <td style="padding:8px 4px;text-align:right;">{cnt:,}</td>
            <td style="padding:8px 4px;text-align:right;">{cnt_pct}%</td>
            <td style="padding:8px 4px;text-align:right;">{byt:,}</td>
            <td style="padding:8px 4px;text-align:right;">{byte_pct}%</td>
            <td style="padding:8px 4px;min-width:120px;">
              <div style="background:{color};height:10px;width:{byte_pct}%;border-radius:3px;display:inline-block;vertical-align:middle;"></div>
            </td>
          </tr>"""

        node_rows = ""
        for nid, cnt in sorted(node_bd.items(), key=lambda x: -x[1])[:8]:
          n_pct = round(cnt / total_tx * 100, 1) if total_tx else 0
          node_rows += f"<li><strong>{nid or 'broadcast'}:</strong> {cnt:,} frames ({n_pct}%)</li>"

        return f"""
        <table style="width:100%;border-collapse:collapse;font-size:0.9em;">
          <thead>
            <tr style="background:var(--table-header-bg);border-bottom:2px solid var(--table-border);">
              <th style="padding:8px 4px;text-align:left;">Frame Type</th>
              <th style="padding:8px 4px;text-align:right;"># Frames</th>
              <th style="padding:8px 4px;text-align:right;">% Count</th>
              <th style="padding:8px 4px;text-align:right;">Bytes</th>
              <th style="padding:8px 4px;text-align:right;">% Bytes</th>
              <th style="padding:8px 4px;">Bytes share</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
          <tfoot>
            <tr style="border-top:2px solid var(--table-border);font-weight:bold;">
              <td style="padding:8px 4px;">TOTAL</td>
              <td style="padding:8px 4px;text-align:right;">{total_tx:,}</td>
              <td style="padding:8px 4px;text-align:right;">100%</td>
              <td style="padding:8px 4px;text-align:right;">{total_bytes:,}</td>
              <td style="padding:8px 4px;text-align:right;">100%</td>
              <td></td>
            </tr>
          </tfoot>
        </table>
        <h4 style="margin-top:14px;">By Peer Node:</h4>
        <ul style="margin:6px 0;">{node_rows}</ul>"""

      initial_transmissions = [
        serialize_sync_transmission(row)
        for row in get_sync_transmission_entries(limit=200)
      ]
      last_transmission_id = max((row["id"] for row in initial_transmissions), default=0)

      recent_channels = []
      recent_channel_comments = []
      with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
          cursor.execute("SELECT id, name, url FROM channels ORDER BY id DESC LIMIT 8")
          recent_channels = cursor.fetchall()
        except sqlite3.OperationalError:
          recent_channels = []
        try:
          cursor.execute(
            """
            SELECT ch.name AS channel_name, cc.sender_short_name, cc.date, cc.content
            FROM channel_comments cc
            JOIN channels ch ON ch.id = cc.channel_id
            ORDER BY cc.id DESC
            LIMIT 8
            """
          )
          recent_channel_comments = cursor.fetchall()
        except sqlite3.OperationalError:
          recent_channel_comments = []

        return render_template(
            "transmissions.html",
            title="Transmission Stats",
            show_nav=True,
            stats_1h_direction_html=_direction_html(stats_1h),
            stats_24h_direction_html=_direction_html(stats_24h),
            stats_1h_category_html=_category_html(stats_1h),
            stats_24h_category_html=_category_html(stats_24h),
            stats_1h_breakdown_html=_breakdown_html(stats_1h),
            stats_24h_breakdown_html=_breakdown_html(stats_24h),
            initial_transmissions=initial_transmissions,
            last_transmission_id=last_transmission_id,
            recent_channels=recent_channels,
            recent_channel_comments=recent_channel_comments,
        )

    @app.route("/system/transmissions/reset", methods=["POST"])
    @login_required
    def system_transmissions_reset():
        from db_operations import clear_sync_transmissions

        clear_sync_transmissions()
        flash("Transmission stats reset.", "success")
        return redirect(url_for("system_transmissions"))

    @app.route("/<table>")
    @login_required
    def table_list(table: str):
        try:
            cfg = get_table_config(table)
        except KeyError:
            flash("Unknown table.", "error")
            return redirect(url_for("table_list", table="bulletins"))

        search_query = request.args.get("q", "").strip()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if table == "bulletins":
                select_sql = (
                    "SELECT id, board, sender_short_name, date, subject, content, local_only, unique_id, "
                    "COALESCE(content_complete, 1) AS _content_complete, "
                    "COALESCE(expected_content_length, LENGTH(content)) AS _expected_content_length "
                    "FROM bulletins"
                )
                if search_query:
                    where_clause = " OR ".join([f"{col} LIKE ?" for col in cfg["searchable"]])
                    params = [f"%{search_query}%" for _ in cfg["searchable"]]
                    cursor.execute(f"{select_sql} WHERE {where_clause} ORDER BY id DESC", params)
                else:
                    cursor.execute(f"{select_sql} ORDER BY id DESC")
            else:
                if search_query:
                    where_clause = " OR ".join([f"{col} LIKE ?" for col in cfg["searchable"]])
                    params = [f"%{search_query}%" for _ in cfg["searchable"]]
                    cursor.execute(
                        f"SELECT {', '.join(cfg['columns'])} FROM {table} WHERE {where_clause} ORDER BY id DESC",
                        params,
                    )
                else:
                    cursor.execute(f"SELECT {', '.join(cfg['columns'])} FROM {table} ORDER BY id DESC")
            rows = [dict(row) for row in cursor.fetchall()]

        display_columns = list(cfg["columns"])
        column_labels = dict(cfg.get("column_labels", {}))
        if table == "bulletins":
            display_columns = ["board", "sender_short_name", "date", "subject", "sync_status", "content"]
            for row in rows:
                expected = int(row.get("_expected_content_length") or len(str(row.get("content") or "")))
                actual = len(str(row.get("content") or ""))
                incomplete = int(row.get("_content_complete") or 0) == 0
                row["sync_status"] = "Incomplete" if incomplete else "OK"
                row["_sync_incomplete"] = incomplete
                row["_sync_status_text"] = f"{actual}/{expected} chars" if incomplete else ""
                row["_resolve_scope"] = "bulletins"
                row["_resolve_key"] = str(row.get("unique_id") or "")
                content = str(row.get("content") or "")
                if len(content) > 200:
                    row["content"] = content[:200] + "…"
        elif table == "mail":
            display_columns = ["sender_short_name", "recipient", "date", "subject", "content"]
            column_labels["sender_short_name"] = "From"
            column_labels["recipient"] = "To"
            for row in rows:
                content = str(row.get("content") or "")
                if len(content) > 200:
                    row["content"] = content[:200] + "…"

        return render_template(
            "table_list.html",
            title=cfg["title"],
            show_nav=True,
            table_title=cfg["title"],
            table_name=table,
            display_columns=display_columns,
            column_labels=column_labels,
            rows=rows,
            search_query=search_query,
            db_path=app.config["DB_PATH"],
            create_url=(url_for("bulletin_new") if table == "bulletins" else url_for("channel_new") if table == "channels" else None),
            create_label=("New Bulletin Post" if table == "bulletins" else "New Channel Entry" if table == "channels" else ""),
            edit_label=("Post/Edit" if table == "channels" else "Edit"),
            comments_enabled=(table == "channels"),
            per_page=25,
            page=1,
            total_pages=None,
            total_count=len(rows),
        )

    @app.route("/bulletins/new", methods=["GET", "POST"])
    @login_required
    def bulletin_new():
      bulletin_boards = app.config["BULLETIN_BOARDS"]
      selected_board = bulletin_boards[0] if bulletin_boards else "General"

      if request.method == "POST":
        board = request.form.get("board", "").strip()
        selected_board = board or selected_board
        sender_short_name = request.form.get("sender_short_name", "").strip()
        subject = request.form.get("subject", "").strip()
        content = request.form.get("content", "").strip()
        local_only = 1 if request.form.get("local_only", "0").strip() == "1" else 0

        if not all([board, sender_short_name, subject, content]):
          flash("All fields are required.", "error")
        elif board not in bulletin_boards:
          flash("Invalid board selected.", "error")
        else:
          from db_operations import add_bulletin
          current_interface = get_runtime_interface()
          bbs_nodes = list(getattr(current_interface, "bbs_nodes", []) or []) if current_interface else []
          add_bulletin(board, sender_short_name, subject, content, bbs_nodes, current_interface, local_only=bool(local_only))
          nudge_sync_after_content_change()
          flash("Bulletin post created.", "success")
          return redirect(url_for("table_list", table="bulletins"))

      return render_template(
        "bulletin_new.html",
        title="New Bulletin",
        show_nav=True,
        bulletin_boards=bulletin_boards,
        selected_board=selected_board,
      )

    @app.route("/channels/new", methods=["GET", "POST"])
    @login_required
    def channel_new():
      if request.method == "POST":
        name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        local_only = 1 if request.form.get("local_only", "0").strip() == "1" else 0

        if not all([name, url]):
          flash("All fields are required.", "error")
        else:
          from db_operations import add_channel
          current_interface = get_runtime_interface()
          bbs_nodes = list(getattr(current_interface, "bbs_nodes", []) or []) if current_interface else []
          add_channel(name, url, bbs_nodes, current_interface, local_only=bool(local_only))
          nudge_sync_after_content_change()
          flash("Channel entry created.", "success")
          return redirect(url_for("table_list", table="channels"))

      return render_template("channel_new.html", title="New Channel", show_nav=True)

    @app.route("/channels/<int:channel_id>/comments", methods=["GET", "POST"])
    @login_required
    def channel_comments(channel_id: int):
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url FROM channels WHERE id = ?", (channel_id,))
        channel = cursor.fetchone()

        if channel is None:
          flash("Channel post not found.", "error")
          return redirect(url_for("table_list", table="channels"))

        if request.method == "POST":
          sender_short_name = request.form.get("sender_short_name", "").strip()
          comment_content = request.form.get("content", "").strip()
          if not sender_short_name or not comment_content:
            flash("Sender and comment are required.", "error")
          else:
            from db_operations import add_channel_comment
            current_interface = get_runtime_interface()
            bbs_nodes = list(getattr(current_interface, "bbs_nodes", []) or []) if current_interface else []
            add_channel_comment(channel_id, sender_short_name, comment_content, bbs_nodes=bbs_nodes, interface=current_interface)
            flash("Comment added.", "success")
            return redirect(url_for("channel_comments", channel_id=channel_id))

        cursor.execute(
          "SELECT id, sender_short_name, date, content, unique_id, "
          "COALESCE(content_complete, 1) AS content_complete, "
          "COALESCE(expected_content_length, LENGTH(content)) AS expected_content_length, "
          "source_node_id, source_timestamp, received_at "
          "FROM channel_comments WHERE channel_id = ? ORDER BY date DESC, unique_id DESC, id DESC",
          (channel_id,)
        )
        comments = []
        for row in cursor.fetchall():
          comment = dict(row)
          expected = int(comment.get("expected_content_length") or len(str(comment.get("content") or "")))
          actual = len(str(comment.get("content") or ""))
          comment["mesh_id"] = str(comment.get("unique_id") or "")[:12]
          comment["sync_status"] = f"Incomplete ({actual}/{expected})" if int(comment.get("content_complete") or 0) == 0 else ""
          comments.append(comment)

      return render_template(
        "channel_comments.html",
        title="Channel Comments",
        show_nav=True,
        channel_id=channel["id"],
        channel_name=channel["name"],
        channel_url=channel["url"],
        comments=comments,
      )

    @app.post("/channels/<int:channel_id>/comments/<int:comment_id>/delete")
    @login_required
    def channel_comment_delete(channel_id: int, comment_id: int):
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT unique_id FROM channel_comments WHERE id = ? AND channel_id = ?", (comment_id, channel_id))
        row = cursor.fetchone()
      if row and row["unique_id"]:
        from db_operations import delete_channel_comment
        current_interface = get_runtime_interface()
        bbs_nodes = list(getattr(current_interface, "bbs_nodes", []) or []) if current_interface else []
        delete_channel_comment(str(row["unique_id"]), bbs_nodes, current_interface)
      else:
        execute_write(
          "DELETE FROM channel_comments WHERE id = ? AND channel_id = ?",
          (comment_id, channel_id)
        )
      flash("Comment deleted.", "success")
      return redirect(url_for("channel_comments", channel_id=channel_id))

    @app.route("/channels/<int:channel_id>/comments/<int:comment_id>/edit", methods=["GET", "POST"])
    @login_required
    def channel_comment_edit(channel_id: int, comment_id: int):
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url FROM channels WHERE id = ?", (channel_id,))
        channel = cursor.fetchone()
        if channel is None:
          flash("Channel not found.", "error")
          return redirect(url_for("table_list", table="channels"))
        cursor.execute(
          "SELECT id, sender_short_name, date, content FROM channel_comments WHERE id = ? AND channel_id = ?",
          (comment_id, channel_id)
        )
        comment = cursor.fetchone()
        if comment is None:
          flash("Comment not found.", "error")
          return redirect(url_for("channel_comments", channel_id=channel_id))

      if request.method == "POST":
        new_content = request.form.get("content", "").strip()
        if not new_content:
          flash("Content cannot be empty.", "error")
        else:
          execute_write(
            "UPDATE channel_comments SET content = ? WHERE id = ? AND channel_id = ?",
            (new_content, comment_id, channel_id)
          )
          flash("Comment updated.", "success")
          return redirect(url_for("channel_comments", channel_id=channel_id))

      return render_template(
        "channel_comment_edit.html",
        title="Edit Comment",
        show_nav=True,
        channel_id=channel_id,
        channel_name=channel["name"],
        comment=dict(comment),
      )

    @app.route("/<table>/<int:row_id>/edit", methods=["GET", "POST"])
    @login_required
    def table_edit(table: str, row_id: int):
        try:
            cfg = get_table_config(table)
        except KeyError:
            flash("Unknown table.", "error")
            return redirect(url_for("table_list", table="bulletins"))

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT {', '.join(cfg['columns'])} FROM {table} WHERE id = ?", (row_id,))
            row = cursor.fetchone()

            if row is None:
                flash("Row not found.", "error")
                return redirect(url_for("table_list", table=table))

            if request.method == "POST":
                values = [request.form.get(field, "").strip() for field in cfg["editable"]]
                if any(v == "" for v in values):
                    flash("All fields are required.", "error")
                elif table == "bulletins" and values[0] not in app.config["BULLETIN_BOARDS"]:
                  flash("Invalid board selected.", "error")
                else:
                    set_clause = ", ".join([f"{field} = ?" for field in cfg["editable"]])
                    cursor.execute(
                        f"UPDATE {table} SET {set_clause} WHERE id = ?",
                        (*values, row_id),
                    )
                    conn.commit()
                    nudge_sync_after_content_change()
                    flash(f"{cfg['title']} row updated.", "success")
                    return redirect(url_for("table_list", table=table))

            cursor.execute(f"SELECT {', '.join(cfg['columns'])} FROM {table} WHERE id = ?", (row_id,))
            row = cursor.fetchone()

        if table == "bulletins":
          return render_template(
            "bulletin_edit.html",
            title=f"Edit {cfg['title']}",
            show_nav=True,
            table_title=cfg["title"],
            table_name=table,
            row=row,
            bulletin_boards=app.config["BULLETIN_BOARDS"],
          )
        else:
          return render_template(
            "record_edit.html",
            title=f"Edit {cfg['title']}",
            show_nav=True,
            table_title=cfg["title"],
            table_name=table,
            editable_fields=cfg["editable"],
            row=row,
          )

    @app.post("/<table>/<int:row_id>/delete")
    @login_required
    def table_delete(table: str, row_id: int):
        try:
            cfg = get_table_config(table)
        except KeyError:
            flash("Unknown table.", "error")
            return redirect(url_for("table_list", table="bulletins"))

        if table in ("bulletins", "mail"):
          with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT unique_id FROM {table} WHERE id = ?", (row_id,))
            row = cursor.fetchone()
          if row and row["unique_id"]:
            if table == "bulletins":
              from db_operations import delete_bulletin
              delete_bulletin(str(row["unique_id"]), [], None)
            else:
              from db_operations import delete_mail
              delete_mail(str(row["unique_id"]), None, [], None)
          else:
            execute_write(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        elif table == "channels":
          # A bare DELETE here left no tombstone, so the next sync pass saw a
          # record the peer had and this node did not and put it straight
          # back -- deleting a channel appeared to do nothing.
          with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, url FROM channels WHERE id = ?", (row_id,))
            row = cursor.fetchone()
          if row:
            from db_operations import delete_channel
            delete_channel(str(row["name"]), str(row["url"]), [], None)
          else:
            execute_write(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        else:
          execute_write(f"DELETE FROM {table} WHERE id = ?", (row_id,))

        nudge_sync_after_content_change()
        flash(f"{cfg['title']} row deleted.", "success")
        return redirect(url_for("table_list", table=table))

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("BBS_WEBGUI_PORT", "8081"))
    host = os.getenv("BBS_WEBGUI_HOST", "127.0.0.1")
    app.run(host=host, port=port)
