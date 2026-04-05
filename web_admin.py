import os
import json
import logging
import sqlite3
import uuid
import configparser
from datetime import datetime
from functools import wraps

from flask import Flask, flash, jsonify, redirect, render_template_string, request, session, url_for

from db_operations import install_connection_log_handler
from utils import get_sync_runtime_settings


TABLE_CONFIG = {
    "bulletins": {
        "title": "Bulletins",
    "columns": ["id", "board", "sender_short_name", "date", "subject", "content", "local_only", "unique_id"],
    "editable": ["board", "sender_short_name", "date", "subject", "content", "local_only"],
    "searchable": ["board", "sender_short_name", "subject", "content", "unique_id", "local_only"],
    },
    "mail": {
        "title": "Mail",
        "columns": ["id", "sender", "sender_short_name", "recipient", "date", "subject", "content", "unique_id"],
        "editable": ["sender", "sender_short_name", "recipient", "date", "subject", "content"],
        "searchable": ["sender", "sender_short_name", "recipient", "subject", "content", "unique_id"],
    },
    "channels": {
        "title": "Channels",
      "columns": ["id", "name", "url", "local_only"],
      "editable": ["name", "url", "local_only"],
      "searchable": ["name", "url", "local_only"],
    },
}



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

def load_sync_settings(config_path: str) -> tuple[list[str], list[str], int]:
  config = read_config_file(config_path)
  bbs_nodes = parse_list_input(config.get("sync", "bbs_nodes", fallback=""))
  allowed_nodes = parse_list_input(config.get("allow_list", "allowed_nodes", fallback=""))
  interval_raw = config.get("sync", "sync_interval_minutes", fallback="5").strip()
  try:
    sync_interval_minutes = int(interval_raw)
  except ValueError:
    sync_interval_minutes = 5
  sync_interval_minutes = max(1, sync_interval_minutes)
  return bbs_nodes, allowed_nodes, sync_interval_minutes


def _parse_bool_setting(raw_value: str | None, default: bool = False) -> bool:
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
  return os.getenv("BBS_MANUAL_SYNC_TRIGGER_PATH", "manual_sync.trigger")


def request_manual_sync_trigger() -> None:
  trigger_path = get_manual_sync_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(datetime.utcnow().isoformat())
  os.replace(tmp_path, trigger_path)


def get_force_check_trigger_path() -> str:
  return os.getenv("BBS_FORCE_CHECK_TRIGGER_PATH", "force_check.trigger")


def request_force_check_trigger() -> None:
  trigger_path = get_force_check_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(datetime.utcnow().isoformat())
  os.replace(tmp_path, trigger_path)


def get_peer_resync_trigger_path() -> str:
  return os.getenv("BBS_PEER_RESYNC_TRIGGER_PATH", "resync_peer.trigger")


def request_peer_resync_trigger(peer_node_id: str) -> None:
  """Write a trigger file containing the peer node ID so server.py clears it
  from its in-memory synced_nodes set and runs a fresh full sync for that peer."""
  trigger_path = get_peer_resync_trigger_path()
  tmp_path = f"{trigger_path}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as trigger_file:
    trigger_file.write(str(peer_node_id).strip())
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
  if normalized in {"SYNCSTATE", "HASHREQ", "HASHMISS", "HASHEND", "DELETE_BULLETIN", "DELETE_MAIL", "BULLETIN", "MAIL", "CHANNEL", "PROFILESYNC", "SCORESYNC", "ZORKSAVE"}:
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
    .flowchart-controls { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; }
    .flowchart-controls .zoom-label { color: var(--muted); font-size: 12px; }
    .flowchart-viewport {
      overflow: hidden;
      border: 1px solid var(--table-border);
      border-radius: 8px;
      background: var(--bg);
      cursor: grab;
      touch-action: none;
    }
    .flowchart-viewport.dragging { cursor: grabbing; }
    .flowchart-svg {
      width: 100%;
      min-height: 2400px;
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
      <a href=\"{{ url_for('system_flowchart') }}\">System Flowchart</a>
      <a href=\"{{ url_for('system_transmissions') }}\">Transmission Stats</a>
      <a href=\"{{ url_for('logout') }}\">Logout</a>
      <div class="nav-right">
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
                headers: { 'Content-Type': 'application/json' },
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
      const contentGroup = document.getElementById('flowchart-content-group');
      if (!viewport || !contentGroup) {
        return;
      }

      const zoomIn = document.getElementById('flowchart-zoom-in');
      const zoomOut = document.getElementById('flowchart-zoom-out');
      const reset = document.getElementById('flowchart-reset');
      const zoomLabel = document.getElementById('flowchart-zoom-label');

      let scale = 1;
      let panX = 0;
      let panY = 0;
      let isDragging = false;
      let lastX = 0;
      let lastY = 0;

      function updateTransform() {
        contentGroup.setAttribute('transform', `translate(${panX} ${panY}) scale(${scale})`);
        if (zoomLabel) {
          zoomLabel.textContent = `${Math.round(scale * 100)}%`;
        }
      }

      function clampScale(value) {
        return Math.min(3.5, Math.max(0.4, value));
      }

      viewport.addEventListener('wheel', (event) => {
        event.preventDefault();
        const factor = event.deltaY < 0 ? 1.1 : 0.9;
        scale = clampScale(scale * factor);
        updateTransform();
      }, { passive: false });

      viewport.addEventListener('mousedown', (event) => {
        isDragging = true;
        lastX = event.clientX;
        lastY = event.clientY;
        viewport.classList.add('dragging');
      });

      window.addEventListener('mousemove', (event) => {
        if (!isDragging) {
          return;
        }
        panX += event.clientX - lastX;
        panY += event.clientY - lastY;
        lastX = event.clientX;
        lastY = event.clientY;
        updateTransform();
      });

      window.addEventListener('mouseup', () => {
        isDragging = false;
        viewport.classList.remove('dragging');
      });

      if (zoomIn) {
        zoomIn.addEventListener('click', () => {
          scale = clampScale(scale * 1.15);
          updateTransform();
        });
      }

      if (zoomOut) {
        zoomOut.addEventListener('click', () => {
          scale = clampScale(scale * 0.87);
          updateTransform();
        });
      }

      if (reset) {
        reset.addEventListener('click', () => {
          scale = 1;
          panX = 0;
          panY = 0;
          updateTransform();
        });
      }

      updateTransform();
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
          const resp = await fetch('/api/sync/manual', { method: 'POST' });
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
  <h2>TC²-BBS Database Admin</h2>
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


LIST_CONTENT = """
<div class=\"card\">
  <h2>{{ table_title }}</h2>
  <p class=\"muted\">Drag and drop rows to reorder them. New posts appear at the top after refresh.</p>
  {% if create_url %}
  <div style=\"margin-bottom: 12px;\">
    <a class=\"btn btn-primary\" href=\"{{ create_url }}\">{{ create_label }}</a>
  </div>
  {% endif %}
  <form method=\"get\" class=\"search-bar\">
    <input type=\"text\" name=\"q\" placeholder=\"Search {{ table_name }}\" value=\"{{ search_query }}\">
    <button class=\"btn\" type=\"submit\">Search</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Clear</a>
  </form>
  <p class=\"muted\">Rows: {{ rows|length }} | DB: <code>{{ db_path }}</code></p>
  <table draggable=\"true\" data-draggable=\"true\" data-table-name=\"{{ table_name }}\">
    <thead>
      <tr>
        <th></th>
        {% for col in columns %}
          <th>{{ col }}</th>
        {% endfor %}
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
        <tr draggable=\"true\" data-row-id=\"{{ row['id'] }}\">
          <td class=\"reorder-handle\">⋮⋮</td>
          {% for col in columns %}
            <td>{{ row[col] }}</td>
          {% endfor %}
          <td>
            <div class=\"row-actions\">
              <a class=\"btn\" href=\"{{ url_for('table_edit', table=table_name, row_id=row['id']) }}\">{{ edit_label }}</a>
              {% if comments_enabled %}
              <a class="btn" href="{{ url_for('channel_comments', channel_id=row['id']) }}">Comments</a>
              {% endif %}
              <form method=\"post\" action=\"{{ url_for('table_delete', table=table_name, row_id=row['id']) }}\" class=\"inline\" onsubmit=\"return confirm('Delete this row?');\">
                <button type=\"submit\" class=\"btn btn-danger\">Delete</button>
              </form>
            </div>
          </td>
        </tr>
      {% endfor %}
      {% if not rows %}
        <tr>
          <td colspan=\"{{ columns|length + 2 }}\" class=\"muted\">No rows found.</td>
        </tr>
      {% endif %}
    </tbody>
  </table>
</div>
"""


NEW_BULLETIN_CONTENT = """
<div class=\"card\">
  <h2>New Bulletin Post</h2>
  <p class=\"muted\">Creates a new row in <code>bulletins</code> with generated date and unique_id.</p>
  <form method=\"post\">
    <label>Board</label><br>
    <select name="board" required>
      {% for board in bulletin_boards %}
      <option value="{{ board }}" {% if selected_board == board %}selected{% endif %}>{{ board }}</option>
      {% endfor %}
    </select><br><br>

    <label>Sender Short Name</label><br>
    <input type=\"text\" name=\"sender_short_name\" placeholder=\"BBS\" required><br><br>

    <label>Subject</label><br>
    <input type=\"text\" name=\"subject\" required><br><br>

    <label>Content</label><br>
    <textarea name=\"content\" required></textarea><br><br>

    <label>local_only</label><br>
    <select name=\"local_only\" required>
      <option value=\"0\" {% if row['local_only']|int == 0 %}selected{% endif %}>0 (sync)</option>
      <option value=\"1\" {% if row['local_only']|int == 1 %}selected{% endif %}>1 (local only)</option>
    </select><br><br>

    <button class=\"btn btn-primary\" type=\"submit\">Create Post</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='bulletins') }}\">Back</a>
  </form>
</div>
"""


NEW_CHANNEL_CONTENT = """
<div class=\"card\">
  <h2>New Channel Entry</h2>
  <p class=\"muted\">Creates a new row in <code>channels</code>.</p>
  <form method=\"post\">
    <label>Channel Name</label><br>
    <input type=\"text\" name=\"name\" required><br><br>

    <label>Channel URL / PSK</label><br>
    <textarea name=\"url\" required></textarea><br><br>

    <label>Local only</label><br>
    <select name=\"local_only\">
      <option value=\"0\" selected>No (sync to peers)</option>
      <option value=\"1\">Yes (keep local)</option>
    </select><br><br>

    <button class=\"btn btn-primary\" type=\"submit\">Create Channel</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='channels') }}\">Back</a>
  </form>
</div>
"""


EDIT_CONTENT = """
<div class=\"card\">
  <h2>Edit {{ table_title }} #{{ row['id'] }}</h2>
  <p class=\"muted\">Primary key and sync IDs are read-only for safety.</p>
  <form method=\"post\">
    {% for field in editable_fields %}
      <label>{{ field }}</label><br>
      {% if field == 'content' %}
        <textarea name=\"{{ field }}\" required>{{ row[field] }}</textarea><br><br>
      {% elif field == 'local_only' %}
        <select name=\"{{ field }}\" required>
          <option value=\"0\" {% if row[field]|int == 0 %}selected{% endif %}>0 (sync)</option>
          <option value=\"1\" {% if row[field]|int == 1 %}selected{% endif %}>1 (local only)</option>
        </select><br><br>
      {% else %}
        <input type=\"text\" name=\"{{ field }}\" value=\"{{ row[field] }}\" required><br><br>
      {% endif %}
    {% endfor %}
    <button class=\"btn btn-primary\" type=\"submit\">Save</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Back</a>
  </form>
</div>
"""


EDIT_BULLETIN_CONTENT = """
<div class=\"card\">
  <h2>Edit {{ table_title }} #{{ row['id'] }}</h2>
  <p class=\"muted\">Primary key and sync IDs are read-only for safety.</p>
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

    <label>Local only</label><br>
    <select name="local_only">
      <option value="0" selected>No (sync to peers)</option>
      <option value="1">Yes (keep local)</option>
    </select><br><br>

    <button class=\"btn btn-primary\" type=\"submit\">Save</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Back</a>
  </form>
</div>
"""


BOARD_SETTINGS_CONTENT = """
<div class=\"card\">
  <h2>Board Settings</h2>
  <p class=\"muted\">Manage bulletin board categories used by create/edit dropdowns.</p>
  {% if env_override %}
  <p class=\"muted\">`BBS_BULLETIN_BOARDS` is set in environment and overrides config file at startup.</p>
  {% endif %}
  <form method=\"post\">
    <label>Boards (comma separated)</label><br>
    <textarea name=\"bulletin_boards\" required>{{ boards_text }}</textarea><br><br>
    <button class=\"btn btn-primary\" type=\"submit\">Save Boards</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='bulletins') }}\">Back</a>
  </form>
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

    <hr>
    <h3 style=\"margin-top: 16px;\">Transmission Pacing</h3>
    <p class=\"muted\">Raise speed for debugging, or slow it down if you need cleaner over-the-air pacing. Environment variables still override GUI settings while they are present.</p>

    <label><input type=\"checkbox\" name=\"sync_turbo\" value=\"1\" {% if sync_speed_settings.sync_turbo %}checked{% endif %}> Enable turbo pacing</label><br>
    <p class=\"muted\">Turbo uses the smallest normal delays and is useful when you want sync traffic to move as fast as possible.</p>

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
    Sending — {{ diagnostics.sync_progress_percent }}% ({{ diagnostics.sync_completed_items }}/{{ diagnostics.sync_total_items }} items) to {{ diagnostics.sync_target_nodes_text }}
  {% elif diagnostics.sync_current_phase == "never_run" %}
    Not yet run since startup
  {% else %}
    Complete &mdash; sent {{ diagnostics.sync_total_items }} item(s) to {{ diagnostics.sync_target_nodes_text }}
  {% endif %}
  </p>
  <p><strong>Sync phase:</strong> {{ diagnostics.sync_current_phase }} &nbsp;|&nbsp; <strong>Last update:</strong> {{ diagnostics.sync_last_updated_at }}</p>
  <p><strong>Last sync result:</strong> {{ diagnostics.sync_last_result }}</p>
  {% if diagnostics.sync_in_progress == "Yes" %}
  <p class="muted"><strong>Notice:</strong> Outbound sync is running. Some historical posts may not be available on peers yet.</p>
  {% endif %}
  <p><strong>Peer consistency:</strong> {{ diagnostics.peer_sync_status }}</p>
  <p><strong>Mismatch re-sync attempts:</strong> {{ diagnostics.mismatch_retry_summary }}</p>
  <p><strong>Peer-advertised record counts:</strong></p>
  <pre style="white-space: pre-wrap; margin-top: 4px;">{{ diagnostics.peer_sync_counts }}</pre>
  <p><strong>Per-scope mismatch reasons:</strong></p>
  <pre style="white-space: pre-wrap; margin-top: 4px;">{{ diagnostics.peer_scope_mismatches }}</pre>
  <p class="muted">Outbound progress can be 100% while peer consistency is mismatched. Peer counts above indicate missing records between nodes.</p>
  {% if diagnostics.mismatch_retry_details %}
  <pre style="white-space: pre-wrap; margin-top: 4px;">{{ diagnostics.mismatch_retry_details }}</pre>
  {% endif %}

  <h3>Database</h3>
  <p><strong>Path:</strong> <code>{{ diagnostics.db_path }}</code></p>
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


SYNC_SETTINGS_CONTENT = """
<div class=\"card\" style=\"max-width: 800px;\">
  <h2>Sync Settings</h2>
  <p class=\"muted\">Manage BBS peer sync targets and the node IDs allowed to post to the Urgent board.</p>
  <form method=\"post\">
    <label>Sync BBS Nodes</label><br>
    <textarea name=\"bbs_nodes\" placeholder=\"One per line or comma separated\">{{ bbs_nodes_text }}</textarea><br>
    <p class=\"muted\">These nodes receive bulletin, mail, delete, and channel sync traffic.</p>

    <label>Allowed Urgent Board Nodes</label><br>
    <textarea name=\"allowed_nodes\" placeholder=\"Leave blank to allow all nodes\">{{ allowed_nodes_text }}</textarea><br>
    <p class=\"muted\">If left blank, any node can post to the Urgent board.</p>

    {% if runtime_updates_enabled %}
    <p class=\"muted\">Changes are also applied to the active interface immediately.</p>
    {% else %}
    <p class=\"muted\">Changes are saved to config.ini. Restart server.py if the BBS process is running separately from this web GUI.</p>
    {% endif %}

    <button class=\"btn btn-primary\" type=\"submit\">Save Sync Settings</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table='bulletins') }}\">Back</a>
  </form>
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
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for comment in comments %}
      <tr>
        <td>{{ comment['id'] }}</td>
        <td>{{ comment['sender_short_name'] }}</td>
        <td>{{ comment['date'] }}</td>
        <td>{{ comment['content'] }}</td>
        <td>
          <form method=\"post\" action=\"{{ url_for('channel_comment_delete', channel_id=channel_id, comment_id=comment['id']) }}\" class=\"inline\" onsubmit=\"return confirm('Delete this comment?');\">
            <button type=\"submit\" class=\"btn btn-danger\">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
      {% if not comments %}
      <tr>
        <td colspan=\"5\" class=\"muted\">No comments yet.</td>
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
<div class=\"card\">
  <h2>Sync Transmission Stats</h2>
  <form method=\"post\" action=\"{{ url_for('system_transmissions_reset') }}\" onsubmit=\"return confirm('Reset transmission stats history now?');\" style=\"margin:8px 0 14px 0;\">
    <button type=\"submit\" class=\"btn btn-danger\">Reset Stats</button>
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
        if (!data.events || !data.events.length) return;
        data.events.forEach((entry) => {
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


FLOWCHART_CONTENT = """
<div class=\"card\">
  <h2>Message System Flowchart - Detailed Command Handlers</h2>
  <p class=\"muted\">Tree view showing command routing and communication messages between system components.</p>
</div>

<div class=\"card\" style=\"background: transparent;\">
  <div class=\"flowchart-controls\">
    <button id=\"flowchart-zoom-in\" class=\"btn btn-small\" type=\"button\">Zoom In</button>
    <button id=\"flowchart-zoom-out\" class=\"btn btn-small\" type=\"button\">Zoom Out</button>
    <button id=\"flowchart-reset\" class=\"btn btn-small\" type=\"button\">Reset View</button>
    <span id=\"flowchart-zoom-label\" class=\"zoom-label\">100%</span>
  </div>
  <div id=\"flowchart-viewport\" class=\"flowchart-viewport\">
  <svg id=\"flowchart-svg\" class=\"flowchart-svg\" viewBox=\"0 0 1600 2500\">
    <g id=\"flowchart-content-group\">
    <!-- Title -->
    <text x=\"800\" y=\"30\" font-size=\"24\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#222\">TC²-BBS Command Handler Tree & Message Flow</text>
    
    <!-- Input -->
    <g id=\"input\">
      <rect x=\"650\" y=\"50\" width=\"300\" height=\"50\" fill=\"#e8f0ff\" stroke=\"#0056d6\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"800\" y=\"80\" font-size=\"12\" text-anchor=\"middle\" font-weight=\"bold\">User Input Message</text>
      <line x1=\"800\" y1=\"100\" x2=\"800\" y2=\"140\" stroke=\"#0056d6\" stroke-width=\"2\"/>
    </g>
    
    <!-- Parser -->
    <g id=\"parser\">
      <rect x=\"650\" y=\"140\" width=\"300\" height=\"60\" fill=\"#fff3cd\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"800\" y=\"160\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">process_message()</text>
      <text x=\"800\" y=\"177\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">Parse command, get user state</text>
      <line x1=\"800\" y1=\"200\" x2=\"800\" y2=\"240\" stroke=\"#ff9800\" stroke-width=\"2\"/>
    </g>
    
    <!-- Main Branching -->
    <g id=\"main-branch\">
      <polygon points=\"800,240 900,270 800,300 700,270\" fill=\"#ffe0b2\" stroke=\"#ff9800\" stroke-width=\"2\"/>
      <text x=\"800\" y=\"273\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Command?</text>
    </g>
    
    <!-- QUICK HELP BRANCH -->
    <g id=\"quick-help\">
      <line x1=\"700\" y1=\"270\" x2=\"150\" y2=\"320\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <text x=\"420\" y=\"290\" font-size=\"9\" fill=\"#666\">Q</text>
      
      <rect x=\"50\" y=\"320\" width=\"200\" height=\"60\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"150\" y=\"340\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Quick Commands [Q]</text>
      <text x=\"150\" y=\"355\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">handle_quick_help_command()</text>
      <line x1=\"150\" y1=\"380\" x2=\"150\" y2=\"430\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      
      <rect x=\"50\" y=\"430\" width=\"200\" height=\"50\" fill=\"#c8e6c9\" stroke=\"#4caf50\" stroke-width=\"1\" rx=\"3\"/>
      <text x=\"150\" y=\"448\" font-size=\"8\" text-anchor=\"middle\" fill=\"#333\">📤 Send Menu:</text>
      <text x=\"150\" y=\"460\" font-size=\"8\" text-anchor=\"middle\" fill=\"#333\">Q,B,U,X options</text>
    </g>
    
    <!-- BULLETINS BRANCH -->
    <g id=\"bulletins\">
      <line x1=\"750\" y1=\"300\" x2=\"400\" y2=\"360\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <text x=\"580\" y=\"320\" font-size=\"9\" fill=\"#666\">B</text>
      
      <rect x=\"300\" y=\"360\" width=\"200\" height=\"60\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"400\" y=\"380\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Bulletins [B]</text>
      <text x=\"400\" y=\"395\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">handle_bulletin_command()</text>
      <line x1=\"400\" y1=\"420\" x2=\"400\" y2=\"460\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      
      <!-- Bulletin sub-options -->
      <g id=\"bulletin-suboptions\">
        <!-- Read -->
        <rect x=\"250\" y=\"460\" width=\"120\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"310\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[R] Read</text>
        <text x=\"310\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">get_bulletins()</text>
        <line x1=\"310\" y1=\"510\" x2=\"310\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"250\" y=\"540\" width=\"120\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"310\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 List bulletins</text>
        <text x=\"310\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">or bulletin content</text>
        
        <!-- Post -->
        <rect x=\"390\" y=\"460\" width=\"120\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"450\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[P] Post</text>
        <text x=\"450\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">add_bulletin()</text>
        <line x1=\"450\" y1=\"510\" x2=\"450\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"390\" y=\"540\" width=\"120\" height=\"50\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\" rx=\"2\"/>
        <text x=\"450\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">💾 Save bulletin</text>
        <text x=\"450\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Sync to BBS nodes</text>
        <text x=\"450\" y=\"577\" font-size=\"7\" text-anchor=\"middle\" fill=\"#b91c1c\">(MULTI-MSG)</text>
      </g>
    </g>
    
    <!-- MAIL BRANCH -->
    <g id=\"mail\">
      <line x1=\"800\" y1=\"300\" x2=\"800\" y2=\"360\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <text x=\"820\" y=\"320\" font-size=\"9\" fill=\"#666\">M</text>
      
      <rect x=\"700\" y=\"360\" width=\"200\" height=\"60\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"800\" y=\"380\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Mail [M]</text>
      <text x=\"800\" y=\"395\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">handle_mail_command()</text>
      <line x1=\"800\" y1=\"420\" x2=\"800\" y2=\"460\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      
      <!-- Mail sub-options -->
      <g id=\"mail-suboptions\">
        <!-- Check -->
        <rect x=\"650\" y=\"460\" width=\"110\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"705\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[C] Check</text>
        <text x=\"705\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">get_mail()</text>
        <line x1=\"705\" y1=\"510\" x2=\"705\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"650\" y=\"540\" width=\"110\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"705\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send mail</text>
        <text x=\"705\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">list/count</text>
        
        <!-- Read -->
        <rect x=\"770\" y=\"460\" width=\"110\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"825\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[R] Read</text>
        <text x=\"825\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">get_mail_content()</text>
        <line x1=\"825\" y1=\"510\" x2=\"825\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"770\" y=\"540\" width=\"110\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"825\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send mail</text>
        <text x=\"825\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">content</text>
        
        <!-- Send -->
        <rect x=\"890\" y=\"460\" width=\"110\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"945\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[S] Send</text>
        <text x=\"945\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">add_mail()</text>
        <line x1=\"945\" y1=\"510\" x2=\"945\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"890\" y=\"540\" width=\"110\" height=\"50\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\" rx=\"2\"/>
        <text x=\"945\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">💾 Save mail</text>
        <text x=\"945\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Sync to nodes</text>
        <text x=\"945\" y=\"577\" font-size=\"7\" text-anchor=\"middle\" fill=\"#b91c1c\">(MULTI-MSG)</text>
      </g>
    </g>
    
    <!-- CHANNELS BRANCH -->
    <g id=\"channels\">
      <line x1=\"850\" y1=\"300\" x2=\"1200\" y2=\"360\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <text x=\"1020\" y=\"320\" font-size=\"9\" fill=\"#666\">C</text>
      
      <rect x=\"1100\" y=\"360\" width=\"200\" height=\"60\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"1200\" y=\"380\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Channels [C]</text>
      <text x=\"1200\" y=\"395\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">handle_channel_directory_command()</text>
      <line x1=\"1200\" y1=\"420\" x2=\"1200\" y2=\"460\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      
      <!-- Channel sub-options -->
      <g id=\"channel-suboptions\">
        <!-- List -->
        <rect x=\"1050\" y=\"460\" width=\"110\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1105\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[L] List</text>
        <text x=\"1105\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">get_channels()</text>
        <line x1=\"1105\" y1=\"510\" x2=\"1105\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1050\" y=\"540\" width=\"110\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1105\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send channel</text>
        <text x=\"1105\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">directory</text>
        
        <!-- Read -->
        <rect x=\"1170\" y=\"460\" width=\"110\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1225\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[R] Read</text>
        <text x=\"1225\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">get_channel_by_id()</text>
        <line x1=\"1225\" y1=\"510\" x2=\"1225\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1170\" y=\"540\" width=\"110\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1225\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send channel</text>
        <text x=\"1225\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">details</text>
      </g>
    </g>
    
    <!-- UTILITIES BRANCH -->
    <g id=\"utilities\">
      <line x1=\"750\" y1=\"300\" x2=\"1400\" y2=\"360\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <text x=\"1070\" y=\"320\" font-size=\"9\" fill=\"#666\">U</text>
      
      <rect x=\"1240\" y=\"360\" width=\"310\" height=\"60\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"1400\" y=\"380\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Utilities [U]</text>
      <text x=\"1400\" y=\"395\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">Various utilities</text>
      <line x1=\"1400\" y1=\"420\" x2=\"1400\" y2=\"460\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      
      <!-- Utility sub-options: S, F, W, Z -->
      <line x1=\"1400\" y1=\"420\" x2=\"1400\" y2=\"450\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      <line x1=\"1277\" y1=\"450\" x2=\"1533\" y2=\"450\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      <g id=\"utility-suboptions\">
        <!-- Stats -->
        <line x1=\"1277\" y1=\"450\" x2=\"1277\" y2=\"470\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1237\" y=\"470\" width=\"80\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1277\" y=\"485\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[S] Stats</text>
        <text x=\"1277\" y=\"498\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">Count messages</text>
        <line x1=\"1277\" y1=\"520\" x2=\"1277\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1237\" y=\"540\" width=\"80\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1277\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send</text>
        <text x=\"1277\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">statistics</text>

        <!-- Fortune -->
        <line x1=\"1362\" y1=\"450\" x2=\"1362\" y2=\"470\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1322\" y=\"470\" width=\"80\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1362\" y=\"485\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[F] Fortune</text>
        <text x=\"1362\" y=\"498\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">Random quote</text>
        <line x1=\"1362\" y1=\"520\" x2=\"1362\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1322\" y=\"540\" width=\"80\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1362\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send</text>
        <text x=\"1362\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">fortune</text>

        <!-- Wall of Shame -->
        <line x1=\"1447\" y1=\"450\" x2=\"1447\" y2=\"470\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1407\" y=\"470\" width=\"80\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1447\" y=\"485\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[W] Shame</text>
        <text x=\"1447\" y=\"498\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">Wall of Shame</text>
        <line x1=\"1447\" y1=\"520\" x2=\"1447\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1407\" y=\"540\" width=\"80\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1447\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send</text>
        <text x=\"1447\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">shame list</text>

        <!-- Zork -->
        <line x1=\"1532\" y1=\"450\" x2=\"1532\" y2=\"470\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1492\" y=\"470\" width=\"80\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1532\" y=\"485\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[Z] Zork</text>
        <text x=\"1532\" y=\"498\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">Z-machine game</text>
        <line x1=\"1532\" y1=\"520\" x2=\"1532\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1492\" y=\"540\" width=\"80\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1532\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">▶️ Play</text>
        <text x=\"1532\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">interactive</text>
      </g>
    </g>
    
    <!-- DATABASE LAYER -->
    <g id=\"database\">
      <text x=\"800\" y=\"660\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">DATABASE OPERATIONS</text>
      
      <rect x=\"50\" y=\"680\" width=\"180\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"140\" y=\"700\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Bulletins Table</text>
      <text x=\"140\" y=\"716\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">id, board, sender,</text>
      <text x=\"140\" y=\"728\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">date, subject,</text>
      <text x=\"140\" y=\"740\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">content, unique_id</text>
      
      <rect x=\"280\" y=\"680\" width=\"180\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"370\" y=\"700\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Mail Table</text>
      <text x=\"370\" y=\"716\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">id, sender, recipient,</text>
      <text x=\"370\" y=\"728\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">date, subject,</text>
      <text x=\"370\" y=\"740\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">content, unique_id</text>
      
      <rect x=\"510\" y=\"680\" width=\"180\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"600\" y=\"700\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Channels Table</text>
      <text x=\"600\" y=\"716\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">id, name, url/PSK,</text>
      <text x=\"600\" y=\"728\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">channel_comments</text>
      <text x=\"600\" y=\"740\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">(id, channel_id,</text>
      
      <rect x=\"740\" y=\"680\" width=\"180\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"830\" y=\"700\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">User State Cache</text>
      <text x=\"830\" y=\"716\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">Tracks user progress</text>
      <text x=\"830\" y=\"728\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">through menus</text>
      <text x=\"830\" y=\"740\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">user_states {}</text>
    </g>
    
    <!-- SYNC MESSAGES SECTION -->
    <g id=\"sync-messages\">
      <text x=\"800\" y=\"820\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">SYNC MESSAGE FORMATS (sent to BBS nodes)</text>
      
      <!-- Bulletin Sync -->
      <rect x=\"50\" y=\"840\" width=\"350\" height=\"100\" fill=\"#fff9e6\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"225\" y=\"860\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Bulletin Sync Message</text>
      <text x=\"60\" y=\"877\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">Format:</text>
      <text x=\"60\" y=\"891\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">BULLETIN|board|sender|subject</text>
      <text x=\"60\" y=\"903\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">|content|unique_id</text>
      <text x=\"60\" y=\"920\" font-size=\"8\" fill=\"#666\">📊 Sent once per sync node</text>
      <text x=\"60\" y=\"932\" font-size=\"8\" fill=\"#b91c1c\">⚠️ Multiple sends = flood</text>
      
      <!-- Mail Sync -->
      <rect x=\"425\" y=\"840\" width=\"350\" height=\"100\" fill=\"#fff9e6\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"600\" y=\"860\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Mail Sync Message</text>
      <text x=\"435\" y=\"877\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">Format:</text>
      <text x=\"435\" y=\"891\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">MAIL|sender_id|sender|recipient</text>
      <text x=\"435\" y=\"903\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">|subject|content|unique_id</text>
      <text x=\"435\" y=\"920\" font-size=\"8\" fill=\"#666\">📊 Sent once per sync node</text>
      <text x=\"435\" y=\"932\" font-size=\"8\" fill=\"#b91c1c\">⚠️ Multiple sends = flood</text>
      
      <!-- Delete Sync -->
      <rect x=\"800\" y=\"840\" width=\"350\" height=\"100\" fill=\"#fff9e6\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"975\" y=\"860\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Delete Sync Messages</text>
      <text x=\"810\" y=\"877\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">Delete Bulletin:</text>
      <text x=\"810\" y=\"891\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">DELETE_BULLETIN|unique_id</text>
      <text x=\"810\" y=\"905\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">Delete Mail:</text>
      <text x=\"810\" y=\"919\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">DELETE_MAIL|unique_id</text>
      <text x=\"810\" y=\"932\" font-size=\"8\" fill=\"#b91c1c\">⚠️ Also multiplied by sync nodes</text>
      
      <!-- Channel Sync -->
      <rect x=\"1175\" y=\"840\" width=\"350\" height=\"100\" fill=\"#fff9e6\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"1350\" y=\"860\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Channel Sync Message</text>
      <text x=\"1185\" y=\"877\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">Format:</text>
      <text x=\"1185\" y=\"891\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">CHANNEL|channel_name</text>
      <text x=\"1185\" y=\"903\" font-size=\"8\" fill=\"#333\" font-family=\"monospace\">|channel_url_or_psk</text>
      <text x=\"1185\" y=\"920\" font-size=\"8\" fill=\"#666\">📊 Sent once per sync node</text>
      <text x=\"1185\" y=\"932\" font-size=\"8\" fill=\"#b91c1c\">⚠️ Multiple sends = flood</text>
    </g>
    
    <!-- MESSAGE MULTIPLICATION EXAMPLE -->
    <g id=\"multiplication-example\">
      <text x=\"800\" y=\"1000\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">MESSAGE MULTIPLICATION PROBLEM</text>
      
      <rect x=\"50\" y=\"1020\" width=\"1500\" height=\"200\" fill=\"#ffe0e0\" stroke=\"#b91c1c\" stroke-width=\"2\" rx=\"5\"/>
      
      <text x=\"800\" y=\"1045\" font-size=\"11\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#000\">Scenario: User posts 1 bulletin with 3 sync nodes configured</text>
      
      <!-- Original message -->
      <circle cx=\"100\" cy=\"1085\" r=\"20\" fill=\"#4caf50\" stroke=\"#2e7d32\" stroke-width=\"2\"/>
      <text x=\"100\" y=\"1092\" font-size=\"10\" text-anchor=\"middle\" fill=\"white\" font-weight=\"bold\">1</text>
      <text x=\"135\" y=\"1095\" font-size=\"10\" fill=\"#000\">User posts bulletin</text>
      
      <!-- Arrow -->
      <polygon points=\"160,1085 190,1085 180,1095 200,1085 190,1075\" fill=\"#ff9800\"/>
      
      <!-- Database save -->
      <circle cx=\"220\" cy=\"1085\" r=\"20\" fill=\"#9c27b0\" stroke=\"#6a1b9a\" stroke-width=\"2\"/>
      <text x=\"220\" y=\"1092\" font-size=\"10\" text-anchor=\"middle\" fill=\"white\" font-weight=\"bold\">1</text>
      <text x=\"255\" y=\"1095\" font-size=\"10\" fill=\"#000\">Saved to database</text>
      
      <!-- Arrow -->
      <polygon points=\"280,1085 310,1085 300,1095 320,1085 310,1075\" fill=\"#ff9800\"/>
      
      <!-- Sync check -->
      <circle cx=\"330\" cy=\"1085\" r=\"20\" fill=\"#2196f3\" stroke=\"#1565c0\" stroke-width=\"2\"/>
      <text x=\"330\" y=\"1092\" font-size=\"9\" text-anchor=\"middle\" fill=\"white\" font-weight=\"bold\">3✓</text>
      <text x=\"365\" y=\"1095\" font-size=\"10\" fill=\"#000\">Sync nodes = 3</text>
      
      <!-- Arrow splits -->
      <line x1=\"360\" y1=\"1085\" x2=\"80\" y2=\"1160\" stroke=\"#ff5722\" stroke-width=\"2\"/>
      <line x1=\"360\" y1=\"1085\" x2=\"400\" y2=\"1160\" stroke=\"#ff5722\" stroke-width=\"2\"/>
      <line x1=\"360\" y1=\"1085\" x2=\"720\" y2=\"1160\" stroke=\"#ff5722\" stroke-width=\"2\"/>
      
      <!-- Messages sent to each node -->
      <rect x=\"30\" y=\"1160\" width=\"100\" height=\"40\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\" rx=\"3\"/>
      <text x=\"80\" y=\"1178\" font-size=\"9\" text-anchor=\"middle\" fill=\"#333\" font-weight=\"bold\">Node 1</text>
      <text x=\"80\" y=\"1192\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">📤 Message sent</text>
      
      <rect x=\"350\" y=\"1160\" width=\"100\" height=\"40\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\" rx=\"3\"/>
      <text x=\"400\" y=\"1178\" font-size=\"9\" text-anchor=\"middle\" fill=\"#333\" font-weight=\"bold\">Node 2</text>
      <text x=\"400\" y=\"1192\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">📤 Message sent</text>
      
      <rect x=\"670\" y=\"1160\" width=\"100\" height=\"40\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\" rx=\"3\"/>
      <text x=\"720\" y=\"1178\" font-size=\"9\" text-anchor=\"middle\" fill=\"#333\" font-weight=\"bold\">Node 3</text>
      <text x=\"720\" y=\"1192\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">📤 Message sent</text>
      
      <!-- Result -->
      <text x=\"800\" y=\"1240\" font-size=\"11\" text-anchor=\"middle\" fill=\"#b91c1c\" font-weight=\"bold\">RESULT: 1 user action = 1 database write + 3 mesh messages</text>
      <text x=\"800\" y=\"1260\" font-size=\"10\" text-anchor=\"middle\" fill=\"#333\">With 10 users posting daily and 5 sync nodes: 10 × 5 = 50 unwanted sync messages</text>
    </g>
    
    <!-- RECOMMENDATIONS -->
    <g id=\"recommendations\">
      <text x=\"800\" y=\"1300\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">OPTIMIZATION STRATEGIES</text>
      
      <rect x=\"50\" y=\"1320\" width=\"700\" height=\"200\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"400\" y=\"1345\" font-size=\"11\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#1b5e20\">Strategy 1: Disable Sync Selectively</text>
      <text x=\"60\" y=\"1365\" font-size=\"9\" fill=\"#333\">• Make sync configurable per operation type</text>
      <text x=\"60\" y=\"1380\" font-size=\"9\" fill=\"#333\">• Example: sync bulletins but not commands</text>
      <text x=\"60\" y=\"1395\" font-size=\"9\" fill=\"#333\">• Example: disable delete message sync</text>
      <text x=\"60\" y=\"1410\" font-size=\"9\" fill=\"#333\">• Add config: sync_operations = bulletin,mail,channel</text>
      <text x=\"60\" y=\"1425\" font-size=\"9\" fill=\"#333\">• Result: Reduce by 30-50% depending on usage</text>
      <text x=\"60\" y=\"1445\" font-size=\"8\" font-weight=\"bold\" fill=\"#0056d6\">Code location: command_handlers.py + db_operations.py</text>
      
      <rect x=\"800\" y=\"1320\" width=\"700\" height=\"200\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"1150\" y=\"1345\" font-size=\"11\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#1b5e20\">Strategy 2: Batch Sync Messages</text>
      <text x=\"810\" y=\"1365\" font-size=\"9\" fill=\"#333\">• Instead of 1 message per node per action</text>
      <text x=\"810\" y=\"1380\" font-size=\"9\" fill=\"#333\">• Collect sync messages and send in batches</text>
      <text x=\"810\" y=\"1395\" font-size=\"9\" fill=\"#333\">• Send every 5 minutes or on buffer fill</text>
      <text x=\"810\" y=\"1410\" font-size=\"9\" fill=\"#333\">• Use pipe-delimited format: MSG1|MSG2|MSG3</text>
      <text x=\"810\" y=\"1425\" font-size=\"9\" fill=\"#333\">• Result: Reduce by 80-90% in high-volume scenarios</text>
      <text x=\"810\" y=\"1445\" font-size=\"8\" font-weight=\"bold\" fill=\"#0056d6\">Code location: utils.py send_message functions</text>
    </g>
    
    <!-- LEGEND -->
    <g id=\"legend\">
      <text x=\"50\" y=\"1580\" font-size=\"12\" font-weight=\"bold\" fill=\"#222\">Legend:</text>
      
      <rect x=\"50\" y=\"1600\" width=\"15\" height=\"15\" fill=\"#e8f0ff\" stroke=\"#0056d6\" stroke-width=\"1\"/>
      <text x=\"75\" y=\"1612\" font-size=\"9\" fill=\"#333\">Input/Output</text>
      
      <rect x=\"250\" y=\"1600\" width=\"15\" height=\"15\" fill=\"#fff3cd\" stroke=\"#ff9800\" stroke-width=\"1\"/>
      <text x=\"275\" y=\"1612\" font-size=\"9\" fill=\"#333\">Processing</text>
      
      <rect x=\"500\" y=\"1600\" width=\"15\" height=\"15\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      <text x=\"525\" y=\"1612\" font-size=\"9\" fill=\"#333\">Handlers/Queries</text>
      
      <rect x=\"800\" y=\"1600\" width=\"15\" height=\"15\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
      <text x=\"825\" y=\"1612\" font-size=\"9\" fill=\"#333\">Database</text>
      
      <rect x=\"1050\" y=\"1600\" width=\"15\" height=\"15\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"1\"/>
      <text x=\"1075\" y=\"1612\" font-size=\"9\" fill=\"#333\">⚠️ Bottleneck</text>
      
      <rect x=\"1350\" y=\"1600\" width=\"15\" height=\"15\" fill=\"#ffe0e0\" stroke=\"#b91c1c\" stroke-width=\"1\"/>
      <text x=\"1375\" y=\"1612\" font-size=\"9\" fill=\"#333\">Problem Area</text>
    </g>

    <!-- LIVE BBS BRANCHING TREE -->
    <g id=\"live-bbs-tree\">
      <text x=\"800\" y=\"1685\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">Live Bulletin Topic Tree</text>
      <text x=\"800\" y=\"1702\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">Actual posts grouped by board/topic directly inside the flowchart.</text>

      <rect x=\"700\" y=\"1720\" width=\"200\" height=\"38\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"800\" y=\"1743\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">BBS Boards</text>

      {% set branch_count = topic_branches|length %}
      {% if branch_count > 0 %}
        {% set step = 1400 // (branch_count + 1) %}
        {% for branch in topic_branches %}
          {% set branch_x = 100 + (step * loop.index) %}

          <line x1=\"800\" y1=\"1758\" x2=\"{{ branch_x }}\" y2=\"1804\" stroke=\"#4caf50\" stroke-width=\"1.5\"/>
          <rect x=\"{{ branch_x - 90 }}\" y=\"1804\" width=\"180\" height=\"42\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"1.2\" rx=\"4\"/>
          <text x=\"{{ branch_x }}\" y=\"1821\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\" fill=\"#333\">{{ branch.board }}</text>
          <text x=\"{{ branch_x }}\" y=\"1835\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">{{ branch.posts|length }} latest posts</text>

          {% for post in branch.posts %}
            {% set post_y = 1860 + (loop.index0 * 58) %}
            <line x1=\"{{ branch_x }}\" y1=\"1846\" x2=\"{{ branch_x }}\" y2=\"{{ post_y }}\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
            <rect x=\"{{ branch_x - 120 }}\" y=\"{{ post_y }}\" width=\"240\" height=\"46\" fill=\"#ffffff\" stroke=\"#cfd8dc\" stroke-width=\"1\" rx=\"4\"/>
            <text x=\"{{ branch_x - 112 }}\" y=\"{{ post_y + 16 }}\" font-size=\"8\" fill=\"#222\">#{{ post.id }} {{ post.preview }}</text>
            <text x=\"{{ branch_x - 112 }}\" y=\"{{ post_y + 31 }}\" font-size=\"7\" fill=\"#666\">{{ post.sender }} | {{ post.date }}</text>
          {% endfor %}
        {% endfor %}
      {% else %}
        <rect x=\"590\" y=\"1808\" width=\"420\" height=\"40\" fill=\"#f7f7f7\" stroke=\"#bbb\" stroke-width=\"1\" rx=\"4\"/>
        <text x=\"800\" y=\"1832\" font-size=\"10\" text-anchor=\"middle\" fill=\"#666\">No bulletin posts yet. Create a bulletin and refresh to see branches.</text>
      {% endif %}
    </g>

    <!-- LIVE CHANNEL COMMENTS TREE -->
    <g id=\"live-channel-comments-tree\">
      <text x=\"800\" y=\"2140\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">Live Channel Comments Tree</text>
      <text x=\"800\" y=\"2157\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">Comments grouped by channel topic.</text>

      <rect x=\"700\" y=\"2175\" width=\"200\" height=\"38\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"800\" y=\"2198\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Channels</text>

      {% set comment_branch_count = comment_branches|length %}
      {% if comment_branch_count > 0 %}
        {% set cstep = 1400 // (comment_branch_count + 1) %}
        {% for branch in comment_branches %}
          {% set branch_x = 100 + (cstep * loop.index) %}
          <line x1=\"800\" y1=\"2213\" x2=\"{{ branch_x }}\" y2=\"2255\" stroke=\"#4caf50\" stroke-width=\"1.5\"/>
          <rect x=\"{{ branch_x - 90 }}\" y=\"2255\" width=\"180\" height=\"42\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"1.2\" rx=\"4\"/>
          <text x=\"{{ branch_x }}\" y=\"2272\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\" fill=\"#333\">{{ branch.channel }}</text>
          <text x=\"{{ branch_x }}\" y=\"2286\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">{{ branch.comments|length }} latest comments</text>

          {% for item in branch.comments %}
            {% set comment_y = 2310 + (loop.index0 * 52) %}
            <line x1=\"{{ branch_x }}\" y1=\"2298\" x2=\"{{ branch_x }}\" y2=\"{{ comment_y }}\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
            <rect x=\"{{ branch_x - 120 }}\" y=\"{{ comment_y }}\" width=\"240\" height=\"42\" fill=\"#ffffff\" stroke=\"#cfd8dc\" stroke-width=\"1\" rx=\"4\"/>
            <text x=\"{{ branch_x - 112 }}\" y=\"{{ comment_y + 15 }}\" font-size=\"7\" fill=\"#222\">#{{ item.id }} {{ item.preview }}</text>
            <text x=\"{{ branch_x - 112 }}\" y=\"{{ comment_y + 29 }}\" font-size=\"7\" fill=\"#666\">{{ item.sender }} | {{ item.date }}</text>
          {% endfor %}
        {% endfor %}
      {% else %}
        <rect x=\"560\" y=\"2258\" width=\"480\" height=\"40\" fill=\"#f7f7f7\" stroke=\"#bbb\" stroke-width=\"1\" rx=\"4\"/>
        <text x=\"800\" y=\"2283\" font-size=\"10\" text-anchor=\"middle\" fill=\"#666\">No channel comments yet. Add comments on a channel post to populate this branch.</text>
      {% endif %}
    </g>
    </g>
  </svg>
  </div>
</div>

<div class=\"card\">
  <h3>Understanding the Flow</h3>
  <ul style=\"margin: 0; padding-left: 20px;\">
    <li><strong>User Input</strong> → Command parser extracts command letter (Q/B/M/C/U)</li>
    <li><strong>Command Handlers</strong> → Specific code block runs (Bulletins, Mail, Channels, etc.)</li>
    <li><strong>Sub-options</strong> → Further refine what action to take (Check, Read, Post, Send)</li>
    <li><strong>Database Operations</strong> → Data is stored/retrieved from SQLite</li>
    <li><strong>Sync Messages</strong> → If sync enabled, formatted message sent to each BBS node</li>
    <li><strong>Message Multiplication</strong> → 1 action × N sync nodes = N messages to mesh</li>
    <li><strong>Response</strong> → User gets answer back (chunked if >200 bytes)</li>
  </ul>
</div>

<div class=\"card\">
  <h3>Live Messages From Database</h3>
  <p class=\"muted\">Latest posted messages currently stored in your database.</p>

  <h4>Recent Bulletins</h4>
  <table>
    <thead>
      <tr>
        <th>id</th>
        <th>board</th>
        <th>sender</th>
        <th>date</th>
        <th>subject</th>
        <th>content</th>
      </tr>
    </thead>
    <tbody>
      {% for row in recent_bulletins %}
      <tr>
        <td>{{ row['id'] }}</td>
        <td>{{ row['board'] }}</td>
        <td>{{ row['sender_short_name'] }}</td>
        <td>{{ row['date'] }}</td>
        <td>{{ row['subject'] }}</td>
        <td>{{ row['content'] }}</td>
      </tr>
      {% endfor %}
      {% if not recent_bulletins %}
      <tr>
        <td colspan=\"6\" class=\"muted\">No bulletin messages found.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>

  <h4 style=\"margin-top: 20px;\">Recent Mail</h4>
  <table>
    <thead>
      <tr>
        <th>id</th>
        <th>from</th>
        <th>to</th>
        <th>date</th>
        <th>subject</th>
        <th>content</th>
      </tr>
    </thead>
    <tbody>
      {% for row in recent_mail %}
      <tr>
        <td>{{ row['id'] }}</td>
        <td>{{ row['sender_short_name'] }}</td>
        <td>{{ row['recipient'] }}</td>
        <td>{{ row['date'] }}</td>
        <td>{{ row['subject'] }}</td>
        <td>{{ row['content'] }}</td>
      </tr>
      {% endfor %}
      {% if not recent_mail %}
      <tr>
        <td colspan=\"6\" class=\"muted\">No mail messages found.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>

  <h4 style=\"margin-top: 20px;\">Recent Channels</h4>
  <table>
    <thead>
      <tr>
        <th>id</th>
        <th>name</th>
        <th>url / psk</th>
      </tr>
    </thead>
    <tbody>
      {% for row in recent_channels %}
      <tr>
        <td>{{ row['id'] }}</td>
        <td>{{ row['name'] }}</td>
        <td>{{ row['url'] }}</td>
      </tr>
      {% endfor %}
      {% if not recent_channels %}
      <tr>
        <td colspan=\"3\" class=\"muted\">No channels found.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>
</div>
"""


def create_app(runtime_interface=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("BBS_WEBGUI_SECRET", "change-this-secret")
    app.config["DB_PATH"] = os.getenv("BBS_DB_PATH", "bulletins.db")
    app.config["CONFIG_PATH"] = os.getenv("BBS_CONFIG_PATH", "config.ini")
    install_connection_log_handler(app.config["DB_PATH"])
    admin_user, admin_password, username_env_override, password_env_override = load_admin_credentials(app.config["CONFIG_PATH"])
    app.config["ADMIN_USER"] = admin_user
    app.config["ADMIN_PASSWORD"] = admin_password
    app.config["ADMIN_USER_ENV_OVERRIDE"] = username_env_override
    app.config["ADMIN_PASSWORD_ENV_OVERRIDE"] = password_env_override
    app.config["BULLETIN_BOARDS"] = load_bulletin_boards(app.config["CONFIG_PATH"])
    app.config["RUNTIME_UPDATES_ENABLED"] = runtime_interface is not None

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
              FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )''')
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
      sync_speed_settings: dict[str, object],
    ) -> None:
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("sync"):
        config.add_section("sync")
      if not config.has_section("allow_list"):
        config.add_section("allow_list")
      config.set("sync", "bbs_nodes", ",".join(bbs_nodes))
      config.set("sync", "sync_interval_minutes", str(sync_interval_minutes))
      config.set("sync", "sync_turbo", "true" if bool(sync_speed_settings.get("sync_turbo", False)) else "false")
      config.set("sync", "sync_pause_seconds", str(sync_speed_settings.get("sync_pause_seconds", 0.75)))
      config.set("sync", "hash_repair_pause_seconds", str(sync_speed_settings.get("hash_repair_pause_seconds", 0.1)))
      config.set("sync", "full_sync_delay_ms", str(sync_speed_settings.get("full_sync_delay_ms", 500)))
      config.set("allow_list", "allowed_nodes", ",".join(allowed_nodes))
      write_config_file(config, app.config["CONFIG_PATH"])

    def apply_runtime_sync_settings(bbs_nodes: list[str], allowed_nodes: list[str]) -> None:
      interface = get_runtime_interface()
      if interface is None:
        return
      interface.bbs_nodes = list(bbs_nodes)
      interface.allowed_nodes = list(allowed_nodes)

    def update_board_settings(raw_boards: str) -> bool:
      updated_boards = parse_list_input(raw_boards)

      if not updated_boards:
        flash("At least one board is required.", "error")
        return False

      save_bulletin_boards(updated_boards)
      app.config["BULLETIN_BOARDS"] = updated_boards
      flash("Board list saved.", "success")
      return True

    def update_sync_settings(
      raw_bbs_nodes: str,
      raw_allowed_nodes: str,
      raw_sync_interval_minutes: str,
      raw_sync_turbo: str,
      raw_sync_pause_seconds: str,
      raw_hash_repair_pause_seconds: str,
      raw_full_sync_delay_ms: str,
    ) -> bool:
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

      save_sync_lists(bbs_nodes, allowed_nodes, sync_interval_minutes, sync_speed_settings)
      apply_runtime_sync_settings(bbs_nodes, allowed_nodes)
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

    def build_settings_diagnostics() -> dict[str, str]:
      bbs_nodes, allowed_nodes, sync_interval_minutes = load_sync_settings(app.config["CONFIG_PATH"])
      diagnostics = {
        "interface_attached": "No",
        "interface_type": "Unavailable",
        "runtime_source": "None",
        "snapshot_updated_at": "Unavailable",
        "mesh_node_count": "Unavailable",
        "local_node_id": "Unavailable",
        "local_short_name": "Unavailable",
        "local_long_name": "Unavailable",
        "bbs_nodes_count": str(len(bbs_nodes)),
        "allowed_nodes_count": str(len(allowed_nodes)),
        "sync_interval_minutes": str(sync_interval_minutes),
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
        "mismatch_retry_summary": "None",
        "mismatch_retry_details": "",
        "db_path": app.config["DB_PATH"],
        "bulletins_count": "Unknown",
        "mail_count": "Unknown",
        "channels_count": "Unknown",
        "zork_saves_count": "Unknown",
        "game_scores_count": "Unknown",
        "connection_events_count": "Unknown",
        "last_connection_event": "None",
        "error": "",
      }

      interface = get_runtime_interface()
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
        snapshot_path = os.getenv("BBS_RUNTIME_DIAG_PATH", "runtime_diagnostics.json")
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
          cursor.execute("SELECT event_time, message_type, event_text FROM connection_events ORDER BY id DESC LIMIT 1")
          row = cursor.fetchone()
          if row:
            diagnostics["last_connection_event"] = f"{row['event_time']} | {row['message_type']} | {row['event_text']}"

          peer_rows = get_peer_mismatch_snapshot(set(bbs_nodes)).get("rows", [])
          if peer_rows:
            from db_operations import get_local_record_counts
            local_hashes = get_local_record_counts()
            lines = []
            mismatch = False
            for peer in peer_rows:
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
              mismatch = mismatch or peer_mismatch
              status = "MISMATCH" if peer_mismatch else "OK"
              lines.append(
                f"{peer[0]} -> B:{pb} M:{pm} C:{pc} Z:{pz} P:{pp} S:{ps} @ {peer[13]} [{status}]"
              )
            diagnostics["peer_sync_status"] = "Mismatch detected" if mismatch else "Counts aligned"
            diagnostics["peer_sync_counts"] = "\n".join(lines)
            mismatch_snapshot = get_peer_mismatch_snapshot(set(bbs_nodes))
            diagnostics["peer_scope_mismatches"] = "\n".join(mismatch_snapshot.get("scope_lines", []))
          else:
            diagnostics["peer_sync_status"] = "No peer reports yet"
            diagnostics["peer_sync_counts"] = "No peer status received yet"
            diagnostics["peer_scope_mismatches"] = "No peer status received yet"
        finally:
          conn.close()
      except Exception as exc:
        diagnostics["error"] = f"Database diagnostics unavailable: {exc}"

      return diagnostics

    def render_settings_page():
      bbs_nodes, allowed_nodes, sync_interval_minutes = load_sync_settings(app.config["CONFIG_PATH"])
      sync_speed_settings = load_sync_speed_settings(app.config["CONFIG_PATH"])
      sync_runtime_settings = get_sync_runtime_settings()
      diagnostics = build_settings_diagnostics()
      content = render_template_string(
        SETTINGS_CONTENT,
        boards_text=",".join(app.config["BULLETIN_BOARDS"]),
        env_override=bool(os.getenv("BBS_BULLETIN_BOARDS", "").strip()),
        bbs_nodes_text="\n".join(bbs_nodes),
        allowed_nodes_text="\n".join(allowed_nodes),
        sync_interval_minutes=str(sync_interval_minutes),
        sync_speed_settings=sync_speed_settings,
        sync_runtime_settings=sync_runtime_settings,
        sync_env_override_flags=get_sync_env_override_flags(),
        runtime_updates_enabled=app.config["RUNTIME_UPDATES_ENABLED"],
        current_username=app.config["ADMIN_USER"],
        username_env_override=app.config["ADMIN_USER_ENV_OVERRIDE"],
        password_env_override=app.config["ADMIN_PASSWORD_ENV_OVERRIDE"],
        diagnostics=diagnostics,
      )
      return render_template_string(BASE_TEMPLATE, title="Settings", content=content, show_nav=True)

    initialize_db_safety()

    def login_required(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return view_func(*args, **kwargs)

        return wrapped

    def get_table_config(table: str) -> dict:
        if table not in TABLE_CONFIG:
            raise KeyError(f"Unknown table: {table}")
        return TABLE_CONFIG[table]

    @app.route("/")
    def index():
        if session.get("logged_in"):
            return redirect(url_for("table_list", table="bulletins"))
        return redirect(url_for("login"))

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

        return render_template_string(
            BASE_TEMPLATE,
            title="Login",
            content=render_template_string(LOGIN_CONTENT),
            show_nav=False,
        )

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

        if section == "sync":
          update_sync_settings(
            request.form.get("bbs_nodes", ""),
            request.form.get("allowed_nodes", ""),
            request.form.get("sync_interval_minutes", "5"),
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
          request.form.get("bbs_nodes", ""),
          request.form.get("allowed_nodes", ""),
          request.form.get("sync_interval_minutes", "5"),
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

    @app.get("/api/sync/status")
    @login_required
    def api_sync_status():
      snapshot_path = os.getenv("BBS_RUNTIME_DIAG_PATH", "runtime_diagnostics.json")
      snapshot = load_runtime_snapshot(snapshot_path)

      _, _, config_interval_minutes = load_sync_settings(app.config["CONFIG_PATH"])
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
      expected_nodes, _, _ = load_sync_settings(app.config["CONFIG_PATH"])
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

      content = render_template_string(
        CLIENTS_CONTENT,
        rows=rows,
        total_posts=total_posts,
        connection_events=connection_events,
        last_event_id=last_event_id,
      )
      return render_template_string(BASE_TEMPLATE, title="Clients", content=content, show_nav=True)

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
      content = render_template_string(
        CLIENT_PROFILE_CONTENT,
        profile=profile,
        node_id=node_id,
      )
      short = profile["short_name"] if profile else node_id
      return render_template_string(BASE_TEMPLATE, title=f"Profile – {short}", content=content, show_nav=True)

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

    @app.route("/system/flowchart")
    @login_required
    def system_flowchart():
      with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
          """
          SELECT id, board, sender_short_name, date, subject, content
          FROM bulletins
          ORDER BY id DESC
          LIMIT 60
          """
        )
        recent_bulletins = cursor.fetchall()

        cursor.execute(
          """
          SELECT id, sender_short_name, recipient, date, subject, content
          FROM mail
          ORDER BY id DESC
          LIMIT 15
          """
        )
        recent_mail = cursor.fetchall()

        cursor.execute(
          """
          SELECT id, name, url
          FROM channels
          ORDER BY id DESC
          LIMIT 15
          """
        )
        recent_channels = cursor.fetchall()

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

      content = render_template_string(
        FLOWCHART_CONTENT,
        recent_bulletins=recent_bulletins,
        recent_mail=recent_mail,
        recent_channels=recent_channels,
        topic_branches=topic_branches,
        comment_branches=comment_branches,
      )
      return render_template_string(BASE_TEMPLATE, title="System Flowchart", content=content, show_nav=True)

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
        'DELETE_BULLETIN': 'Content',
        'DELETE_MAIL':     'Content',
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

        html = render_template_string(
            TRANSMISSION_DASHBOARD_CONTENT,
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

        return render_template_string(BASE_TEMPLATE, title="Transmission Stats", content=html, show_nav=True)

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
            if search_query:
                where_clause = " OR ".join([f"{col} LIKE ?" for col in cfg["searchable"]])
                params = [f"%{search_query}%" for _ in cfg["searchable"]]
                cursor.execute(
                    f"SELECT {', '.join(cfg['columns'])} FROM {table} WHERE {where_clause} ORDER BY id DESC",
                    params,
                )
            else:
                cursor.execute(f"SELECT {', '.join(cfg['columns'])} FROM {table} ORDER BY id DESC")
            rows = cursor.fetchall()

        content = render_template_string(
            LIST_CONTENT,
            table_title=cfg["title"],
            table_name=table,
            columns=cfg["columns"],
            rows=rows,
            search_query=search_query,
            db_path=app.config["DB_PATH"],
          create_url=(url_for("bulletin_new") if table == "bulletins" else url_for("channel_new") if table == "channels" else None),
          create_label=("New Bulletin Post" if table == "bulletins" else "New Channel Entry" if table == "channels" else ""),
          edit_label=("Post/Edit" if table == "channels" else "Edit"),
          comments_enabled=(table == "channels"),
        )
        return render_template_string(BASE_TEMPLATE, title=cfg["title"], content=content, show_nav=True)

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
          post_date = datetime.now().strftime("%Y-%m-%d %H:%M")
          unique_id = str(uuid.uuid4())
          execute_write(
            "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id, local_only) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (board, sender_short_name, post_date, subject, content, unique_id, local_only),
          )
          flash("Bulletin post created.", "success")
          return redirect(url_for("table_list", table="bulletins"))

      content = render_template_string(
        NEW_BULLETIN_CONTENT,
        bulletin_boards=bulletin_boards,
        selected_board=selected_board,
      )
      return render_template_string(BASE_TEMPLATE, title="New Bulletin", content=content, show_nav=True)

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
          execute_write(
            "INSERT INTO channels (name, url, local_only) VALUES (?, ?, ?)",
            (name, url, local_only),
          )
          flash("Channel entry created.", "success")
          return redirect(url_for("table_list", table="channels"))

      content = render_template_string(NEW_CHANNEL_CONTENT)
      return render_template_string(BASE_TEMPLATE, title="New Channel", content=content, show_nav=True)

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
          content = request.form.get("content", "").strip()
          if not sender_short_name or not content:
            flash("Sender and comment are required.", "error")
          else:
            comment_date = datetime.now().strftime("%Y-%m-%d %H:%M")
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
              "INSERT INTO channel_comments (channel_id, sender_short_name, date, content) VALUES (?, ?, ?, ?)",
              (channel_id, sender_short_name, comment_date, content)
            )
            conn.commit()
            flash("Comment added.", "success")
            return redirect(url_for("channel_comments", channel_id=channel_id))

        cursor.execute(
          "SELECT id, sender_short_name, date, content FROM channel_comments WHERE channel_id = ? ORDER BY id DESC",
          (channel_id,)
        )
        comments = cursor.fetchall()

      content = render_template_string(
        CHANNEL_COMMENTS_CONTENT,
        channel_id=channel["id"],
        channel_name=channel["name"],
        channel_url=channel["url"],
        comments=comments,
      )
      return render_template_string(BASE_TEMPLATE, title="Channel Comments", content=content, show_nav=True)

    @app.post("/channels/<int:channel_id>/comments/<int:comment_id>/delete")
    @login_required
    def channel_comment_delete(channel_id: int, comment_id: int):
      execute_write(
        "DELETE FROM channel_comments WHERE id = ? AND channel_id = ?",
        (comment_id, channel_id)
      )
      flash("Comment deleted.", "success")
      return redirect(url_for("channel_comments", channel_id=channel_id))

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
                    flash(f"{cfg['title']} row updated.", "success")
                    return redirect(url_for("table_list", table=table))

            cursor.execute(f"SELECT {', '.join(cfg['columns'])} FROM {table} WHERE id = ?", (row_id,))
            row = cursor.fetchone()

        if table == "bulletins":
          content = render_template_string(
            EDIT_BULLETIN_CONTENT,
            table_title=cfg["title"],
            table_name=table,
            row=row,
            bulletin_boards=app.config["BULLETIN_BOARDS"],
          )
        else:
          content = render_template_string(
            EDIT_CONTENT,
            table_title=cfg["title"],
            table_name=table,
            editable_fields=cfg["editable"],
            row=row,
          )
        return render_template_string(BASE_TEMPLATE, title=f"Edit {cfg['title']}", content=content, show_nav=True)

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
        else:
          execute_write(f"DELETE FROM {table} WHERE id = ?", (row_id,))

        flash(f"{cfg['title']} row deleted.", "success")
        return redirect(url_for("table_list", table=table))

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("BBS_WEBGUI_PORT", "8081"))
    host = os.getenv("BBS_WEBGUI_HOST", "127.0.0.1")
    app.run(host=host, port=port)
