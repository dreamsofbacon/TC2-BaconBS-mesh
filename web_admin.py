import os
import sqlite3
import uuid
import configparser
from datetime import datetime
from functools import wraps

from flask import Flask, flash, jsonify, redirect, render_template_string, request, session, url_for


TABLE_CONFIG = {
    "bulletins": {
        "title": "Bulletins",
        "columns": ["id", "board", "sender_short_name", "date", "subject", "content", "unique_id"],
        "editable": ["board", "sender_short_name", "date", "subject", "content"],
        "searchable": ["board", "sender_short_name", "subject", "content", "unique_id"],
    },
    "mail": {
        "title": "Mail",
        "columns": ["id", "sender", "sender_short_name", "recipient", "date", "subject", "content", "unique_id"],
        "editable": ["sender", "sender_short_name", "recipient", "date", "subject", "content"],
        "searchable": ["sender", "sender_short_name", "recipient", "subject", "content", "unique_id"],
    },
    "channels": {
        "title": "Channels",
        "columns": ["id", "name", "url"],
        "editable": ["name", "url"],
        "searchable": ["name", "url"],
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


def load_sync_settings(config_path: str) -> tuple[list[str], list[str]]:
  config = read_config_file(config_path)
  bbs_nodes = parse_list_input(config.get("sync", "bbs_nodes", fallback=""))
  allowed_nodes = parse_list_input(config.get("allow_list", "allowed_nodes", fallback=""))
  return bbs_nodes, allowed_nodes


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
    .theme-toggle { margin-left: auto; }
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
  </style>
</head>
<body data-theme="dark">
  <div class=\"container\">
    {% if show_nav %}
    <div class=\"nav\">
      <a href=\"{{ url_for('table_list', table='bulletins') }}\">Bulletins</a>
      <a href=\"{{ url_for('table_list', table='mail') }}\">Mail</a>
      <a href=\"{{ url_for('table_list', table='channels') }}\">Channels</a>
      <a href=\"{{ url_for('clients_summary') }}\">Clients</a>
      <a href=\"{{ url_for('settings_page') }}\">Settings</a>
      <a href=\"{{ url_for('system_flowchart') }}\">System Flowchart</a>
      <a href=\"{{ url_for('logout') }}\">Logout</a>
      <button id="theme-toggle" class="btn btn-small theme-toggle" type="button">Switch to Light</button>
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
    
    document.addEventListener('DOMContentLoaded', () => {
      initializeTheme();
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
  <p class=\"muted\">Manage BBS peer sync targets and the node IDs allowed to post to the Urgent board.</p>
  <form method=\"post\" action=\"{{ url_for('settings_page') }}#sync\">
    <input type=\"hidden\" name=\"settings_section\" value=\"sync\">
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

<div class=\"card\" id=\"diagnostics\" style=\"max-width: 900px;\">
  <h2>Diagnostics</h2>
  <p class=\"muted\">Quick runtime and BBS status details for troubleshooting.</p>

  <h3>Runtime</h3>
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

  <h3>Database</h3>
  <p><strong>Path:</strong> <code>{{ diagnostics.db_path }}</code></p>
  <p><strong>Bulletins:</strong> {{ diagnostics.bulletins_count }}</p>
  <p><strong>Mail:</strong> {{ diagnostics.mail_count }}</p>
  <p><strong>Channels:</strong> {{ diagnostics.channels_count }}</p>
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
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        <td>{{ row['sender_short_name'] }}</td>
        <td>{{ row['post_count'] }}</td>
      </tr>
      {% endfor %}
      {% if not rows %}
      <tr>
        <td colspan=\"2\" class=\"muted\">No client posts found.</td>
      </tr>
      {% endif %}
    </tbody>
  </table>
</div>

<div class=\"card\">
  <h3>Live Connection Log</h3>
  <p class=\"muted\">Terminal-style stream of inbound mesh activity. Auto-refreshes every 2 seconds.</p>
  <div class=\"terminal-controls\">
    <span class=\"muted\" style=\"font-size:11px;font-family:Consolas,monospace;\">Filter:</span>
    <button class=\"terminal-btn active\" data-filter=\"all\" onclick=\"setFilter(this,'all')\">All</button>
    <button class=\"terminal-btn\" data-filter=\"user\" onclick=\"setFilter(this,'user')\">RX</button>
    <button class=\"terminal-btn\" data-filter=\"sync\" onclick=\"setFilter(this,'sync')\">SYNC</button>
    <button class=\"terminal-btn\" data-filter=\"direct\" onclick=\"setFilter(this,'direct')\">DIRECT</button>
    <button class=\"terminal-btn\" data-filter=\"drop\" onclick=\"setFilter(this,'drop')\">DROP</button>
    <span style=\"flex:1\"></span>
    <button class=\"terminal-btn btn-pause\" id=\"btn-pause\" onclick=\"togglePause()\">Pause</button>
    <button class=\"terminal-btn btn-clear\" onclick=\"clearTerminal()\">Clear</button>
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
    }

    function appendLine(evt) {
      const line = document.createElement('div');
      line.className = 'terminal-line';
      line.dataset.type = evt.message_type;
      if (currentFilter !== 'all' && evt.message_type !== currentFilter) {
        line.style.display = 'none';
      }
      const sender = evt.sender_short_name || evt.sender_node_id || evt.sender_num || '?';
      const to = evt.to_id || 'group';
      line.innerHTML =
        '<span class=\"terminal-time\">[' + evt.event_time + ']</span> ' +
        '<span class=\"terminal-type\">' + evt.message_type.toUpperCase() + '</span> ' +
        sender + ' -> ' + to + ' :: ' + evt.event_text;
      terminal.appendChild(line);
      if (!paused) terminal.scrollTop = terminal.scrollHeight;
    }

    {% for evt in connection_events %}
    appendLine({
      event_time: {{ evt['event_time']|tojson }},
      message_type: {{ evt['message_type']|tojson }},
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
    admin_user, admin_password, username_env_override, password_env_override = load_admin_credentials(app.config["CONFIG_PATH"])
    app.config["ADMIN_USER"] = admin_user
    app.config["ADMIN_PASSWORD"] = admin_password
    app.config["ADMIN_USER_ENV_OVERRIDE"] = username_env_override
    app.config["ADMIN_PASSWORD_ENV_OVERRIDE"] = password_env_override
    app.config["BULLETIN_BOARDS"] = load_bulletin_boards(app.config["CONFIG_PATH"])
    app.config["RUNTIME_UPDATES_ENABLED"] = runtime_interface is not None

    def get_runtime_interface():
        return runtime_interface

    def initialize_db_safety() -> None:
        with sqlite3.connect(app.config["DB_PATH"], timeout=30) as conn:
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

    def get_db_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(app.config["DB_PATH"], timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def execute_write(query: str, params: tuple = ()) -> None:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(query, params)
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

    def save_sync_lists(bbs_nodes: list[str], allowed_nodes: list[str]) -> None:
      config = read_config_file(app.config["CONFIG_PATH"])
      if not config.has_section("sync"):
        config.add_section("sync")
      if not config.has_section("allow_list"):
        config.add_section("allow_list")
      config.set("sync", "bbs_nodes", ",".join(bbs_nodes))
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

    def update_sync_settings(raw_bbs_nodes: str, raw_allowed_nodes: str) -> bool:
      bbs_nodes = parse_list_input(raw_bbs_nodes)
      allowed_nodes = parse_list_input(raw_allowed_nodes)

      save_sync_lists(bbs_nodes, allowed_nodes)
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

    def build_settings_diagnostics() -> dict[str, str]:
      bbs_nodes, allowed_nodes = load_sync_settings(app.config["CONFIG_PATH"])
      diagnostics = {
        "interface_attached": "No",
        "interface_type": "Unavailable",
        "mesh_node_count": "Unavailable",
        "local_node_id": "Unavailable",
        "local_short_name": "Unavailable",
        "local_long_name": "Unavailable",
        "bbs_nodes_count": str(len(bbs_nodes)),
        "allowed_nodes_count": str(len(allowed_nodes)),
        "bbs_nodes_text": ", ".join(bbs_nodes),
        "allowed_nodes_text": ", ".join(allowed_nodes),
        "board_count": str(len(app.config["BULLETIN_BOARDS"])),
        "board_list": ", ".join(app.config["BULLETIN_BOARDS"]),
        "db_path": app.config["DB_PATH"],
        "bulletins_count": "Unknown",
        "mail_count": "Unknown",
        "channels_count": "Unknown",
        "connection_events_count": "Unknown",
        "last_connection_event": "None",
        "error": "",
      }

      interface = get_runtime_interface()
      if interface is not None:
        diagnostics["interface_attached"] = "Yes"
        diagnostics["interface_type"] = interface.__class__.__name__
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
          cursor.execute("SELECT COUNT(*) FROM connection_events")
          diagnostics["connection_events_count"] = str(cursor.fetchone()[0])
          cursor.execute("SELECT event_time, message_type, event_text FROM connection_events ORDER BY id DESC LIMIT 1")
          row = cursor.fetchone()
          if row:
            diagnostics["last_connection_event"] = f"{row['event_time']} | {row['message_type']} | {row['event_text']}"
        finally:
          conn.close()
      except Exception as exc:
        diagnostics["error"] = f"Database diagnostics unavailable: {exc}"

      return diagnostics

    def render_settings_page():
      bbs_nodes, allowed_nodes = load_sync_settings(app.config["CONFIG_PATH"])
      diagnostics = build_settings_diagnostics()
      content = render_template_string(
        SETTINGS_CONTENT,
        boards_text=",".join(app.config["BULLETIN_BOARDS"]),
        env_override=bool(os.getenv("BBS_BULLETIN_BOARDS", "").strip()),
        bbs_nodes_text="\n".join(bbs_nodes),
        allowed_nodes_text="\n".join(allowed_nodes),
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
          update_sync_settings(request.form.get("bbs_nodes", ""), request.form.get("allowed_nodes", ""))
          return redirect(url_for("settings_page") + "#sync")

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
        update_sync_settings(request.form.get("bbs_nodes", ""), request.form.get("allowed_nodes", ""))
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
        cursor.execute(
          """
          SELECT sender_short_name, COUNT(*) AS post_count
          FROM bulletins
          WHERE sender_short_name IS NOT NULL AND TRIM(sender_short_name) != ''
          GROUP BY sender_short_name
          ORDER BY post_count DESC, sender_short_name ASC
          """
        )
        rows = cursor.fetchall()
        total_posts = sum(row["post_count"] for row in rows)

        cursor.execute(
          """
          SELECT id, event_time, sender_num, sender_node_id, sender_short_name, to_id, message_type, event_text
          FROM connection_events
          ORDER BY id DESC
          LIMIT 120
          """
        )
        events_desc = cursor.fetchall()

      connection_events = list(reversed(events_desc))
      last_event_id = connection_events[-1]["id"] if connection_events else 0

      content = render_template_string(
        CLIENTS_CONTENT,
        rows=rows,
        total_posts=total_posts,
        connection_events=connection_events,
        last_event_id=last_event_id,
      )
      return render_template_string(BASE_TEMPLATE, title="Clients", content=content, show_nav=True)

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
        events = [dict(row) for row in cursor.fetchall()]
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

        if not all([board, sender_short_name, subject, content]):
          flash("All fields are required.", "error")
        elif board not in bulletin_boards:
          flash("Invalid board selected.", "error")
        else:
          post_date = datetime.now().strftime("%Y-%m-%d %H:%M")
          unique_id = str(uuid.uuid4())
          execute_write(
            "INSERT INTO bulletins (board, sender_short_name, date, subject, content, unique_id) VALUES (?, ?, ?, ?, ?, ?)",
            (board, sender_short_name, post_date, subject, content, unique_id),
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

        if not all([name, url]):
          flash("All fields are required.", "error")
        else:
          execute_write(
            "INSERT INTO channels (name, url) VALUES (?, ?)",
            (name, url),
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

        execute_write(f"DELETE FROM {table} WHERE id = ?", (row_id,))

        flash(f"{cfg['title']} row deleted.", "success")
        return redirect(url_for("table_list", table=table))

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("BBS_WEBGUI_PORT", "8081"))
    host = os.getenv("BBS_WEBGUI_HOST", "127.0.0.1")
    app.run(host=host, port=port)
