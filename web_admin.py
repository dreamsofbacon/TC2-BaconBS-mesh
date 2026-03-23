import os
import sqlite3
import uuid
import configparser
from datetime import datetime
from functools import wraps

from flask import Flask, flash, redirect, render_template_string, request, session, url_for


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


def load_bulletin_boards() -> list[str]:
  env_value = os.getenv("BBS_BULLETIN_BOARDS", "").strip()
  if env_value:
    boards = [item.strip() for item in env_value.split(",") if item.strip()]
    if boards:
      return boards

  config = configparser.ConfigParser()
  config.read("config.ini")
  config_value = config.get("boards", "bulletin_boards", fallback="").strip()
  if config_value:
    boards = [item.strip() for item in config_value.split(",") if item.strip()]
    if boards:
      return boards

  return ["General", "Info", "News", "Urgent"]


BASE_TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{{ title }}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; background: #f6f7fb; color: #222; }
    .container { max-width: 1200px; margin: 0 auto; }
    .card { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
    .nav a { margin-right: 12px; text-decoration: none; color: #0056d6; }
    .nav { margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border: 1px solid #ddd; padding: 8px; vertical-align: top; text-align: left; }
    th { background: #f0f3fa; }
    tr.dragging { opacity: 0.5; background: #f9f9f9; }
    tr.drag-over { border-top: 3px solid #0056d6; }
    input[type=text], input[type=password], textarea, select { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 6px; }
    textarea { min-height: 180px; }
    .row-actions { display: flex; gap: 8px; }
    .btn { border: 1px solid #bbb; border-radius: 6px; padding: 6px 10px; background: #fff; cursor: pointer; }
    .btn-primary { border-color: #0056d6; color: #fff; background: #0056d6; }
    .btn-danger { border-color: #b91c1c; color: #fff; background: #b91c1c; }
    .btn-small { padding: 4px 6px; font-size: 12px; }
    .reorder-handle { cursor: grab; color: #999; padding: 4px 8px; }
    .reorder-handle:hover { color: #0056d6; }
    .reorder-handle:active { cursor: grabbing; }
    .muted { color: #666; font-size: 12px; }
    .flash { padding: 10px; border-radius: 6px; margin-bottom: 12px; border: 1px solid #ddd; background: #fafafa; }
    .flash-error { border-color: #b91c1c; background: #fff1f2; }
    .flash-success { border-color: #0f766e; background: #f0fdfa; }
    .search-bar { display: flex; gap: 8px; margin-bottom: 12px; }
    .inline { display: inline; }
    code { background: #f3f4f6; padding: 2px 6px; border-radius: 4px; }
  </style>
</head>
<body>
  <div class=\"container\">
    {% if show_nav %}
    <div class=\"nav\">
      <a href=\"{{ url_for('table_list', table='bulletins') }}\">Bulletins</a>
      <a href=\"{{ url_for('table_list', table='mail') }}\">Mail</a>
      <a href=\"{{ url_for('table_list', table='channels') }}\">Channels</a>
      <a href=\"{{ url_for('clients_summary') }}\">Clients</a>
      <a href=\"{{ url_for('board_settings') }}\">Boards</a>
      <a href=\"{{ url_for('admin_settings') }}\">Admin</a>
      <a href=\"{{ url_for('system_flowchart') }}\">System Flowchart</a>
      <a href=\"{{ url_for('logout') }}\">Logout</a>
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
    
    document.addEventListener('DOMContentLoaded', () => {
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


ADMIN_SETTINGS_CONTENT = """
<div class=\"card\" style=\"max-width: 600px;\">
  <h2>Admin Credentials</h2>
  <p class=\"muted\">Change the username and password for the web admin interface.</p>
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
"""


FLOWCHART_CONTENT = """
<div class=\"card\">
  <h2>Message System Flowchart</h2>
  <p class=\"muted\">Visual representation of how messages flow through the TC²-BBS system.</p>
</div>

<div class=\"card\" style=\"overflow-x: auto; background: #fafafa;\">
  <svg viewBox=\"0 0 1200 1400\" style=\"width: 100%; min-height: 1400px; border: 1px solid #ddd; border-radius: 8px;\">
    <!-- Title -->
    <text x=\"600\" y=\"30\" font-size=\"24\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#222\">TC²-BBS Message Flow Architecture</text>
    
    <!-- Input Layer -->
    <g id=\"input-layer\">
      <text x=\"600\" y=\"70\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">INPUT LAYER</text>
      
      <!-- User Input -->
      <rect x=\"450\" y=\"100\" width=\"300\" height=\"60\" fill=\"#e8f0ff\" stroke=\"#0056d6\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"600\" y=\"135\" font-size=\"12\" text-anchor=\"middle\" font-weight=\"bold\">User Message</text>
      <text x=\"600\" y=\"150\" font-size=\"10\" text-anchor=\"middle\" fill=\"#666\">(Text from Meshtastic device)</text>
      
      <!-- Vertical line down -->
      <line x1=\"600\" y1=\"160\" x2=\"600\" y2=\"200\" stroke=\"#0056d6\" stroke-width=\"2\"/>
    </g>
    
    <!-- Process Layer -->
    <g id=\"process-layer\">
      <text x=\"600\" y=\"220\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">MESSAGE PROCESSING</text>
      
      <!-- Main processor -->
      <rect x=\"400\" y=\"240\" width=\"400\" height=\"80\" fill=\"#fff3cd\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"600\" y=\"265\" font-size=\"12\" text-anchor=\"middle\" font-weight=\"bold\">process_message()</text>
      <text x=\"600\" y=\"283\" font-size=\"10\" text-anchor=\"middle\" fill=\"#666\">Parse command & get user state</text>
      <text x=\"600\" y=\"298\" font-size=\"10\" text-anchor=\"middle\" fill=\"#666\">Route to appropriate handler</text>
      <text x=\"600\" y=\"313\" font-size=\"10\" text-anchor=\"middle\" fill=\"#666\">(Main Menu, BBS, Utilities, etc.)</text>
      
      <!-- Lines to handlers -->
      <line x1=\"600\" y1=\"320\" x2=\"600\" y2=\"360\" stroke=\"#ff9800\" stroke-width=\"2\"/>
    </g>
    
    <!-- Handler Branching -->
    <g id=\"handlers-layer\">
      <text x=\"600\" y=\"380\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">COMMAND HANDLERS</text>
      
      <!-- Bulletin Handler -->
      <rect x=\"80\" y=\"410\" width=\"180\" height=\"80\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"170\" y=\"430\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Bulletin Commands</text>
      <text x=\"170\" y=\"447\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Check bulletins</text>
      <text x=\"170\" y=\"460\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Read bulletin</text>
      <text x=\"170\" y=\"473\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Post bulletin</text>
      
      <!-- Mail Handler -->
      <rect x=\"310\" y=\"410\" width=\"180\" height=\"80\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"400\" y=\"430\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Mail Commands</text>
      <text x=\"400\" y=\"447\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Check mail</text>
      <text x=\"400\" y=\"460\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Read mail</text>
      <text x=\"400\" y=\"473\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Send mail</text>
      
      <!-- Channel Handler -->
      <rect x=\"540\" y=\"410\" width=\"180\" height=\"80\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"630\" y=\"430\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Channel Commands</text>
      <text x=\"630\" y=\"447\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• List channels</text>
      <text x=\"630\" y=\"460\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Read channel</text>
      <text x=\"630\" y=\"473\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Post to channel</text>
      
      <!-- Other Handlers -->
      <rect x=\"770\" y=\"410\" width=\"180\" height=\"80\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"860\" y=\"430\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Other Commands</text>
      <text x=\"860\" y=\"447\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Stats</text>
      <text x=\"860\" y=\"460\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Fortune</text>
      <text x=\"860\" y=\"473\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• JS8Call</text>
      
      <!-- JS8 Handler -->
      <rect x=\"1000\" y=\"410\" width=\"150\" height=\"80\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"1075\" y=\"430\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">JS8Call</text>
      <text x=\"1075\" y=\"447\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Integration</text>
      <text x=\"1075\" y=\"460\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Group Msgs</text>
      <text x=\"1075\" y=\"473\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">• Forwarding</text>
      
      <!-- Lines from processor to handlers -->
      <line x1=\"470\" y1=\"365\" x2=\"170\" y2=\"410\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <line x1=\"520\" y1=\"360\" x2=\"400\" y2=\"410\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <line x1=\"600\" y1=\"360\" x2=\"630\" y2=\"410\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <line x1=\"700\" y1=\"360\" x2=\"860\" y2=\"410\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <line x1=\"730\" y1=\"365\" x2=\"1075\" y2=\"410\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
    </g>
    
    <!-- Database Layer -->
    <g id=\"database-layer\">
      <text x=\"600\" y=\"540\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">DATABASE OPERATIONS</text>
      
      <!-- Bulletins DB -->
      <rect x=\"100\" y=\"560\" width=\"160\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"180\" y=\"580\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Bulletins DB</text>
      <text x=\"180\" y=\"597\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">add_bulletin()</text>
      <text x=\"180\" y=\"610\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">get_bulletins()</text>
      <text x=\"180\" y=\"623\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">delete_bulletin()</text>
      
      <!-- Mail DB -->
      <rect x=\"330\" y=\"560\" width=\"160\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"410\" y=\"580\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Mail DB</text>
      <text x=\"410\" y=\"597\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">add_mail()</text>
      <text x=\"410\" y=\"610\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">get_mail()</text>
      <text x=\"410\" y=\"623\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">delete_mail()</text>
      
      <!-- Channel DB -->
      <rect x=\"560\" y=\"560\" width=\"160\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"640\" y=\"580\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Channels DB</text>
      <text x=\"640\" y=\"597\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">add_channel()</text>
      <text x=\"640\" y=\"610\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">get_channels()</text>
      <text x=\"640\" y=\"623\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">add_comment()</text>
      
      <!-- SQLite Core -->
      <rect x=\"790\" y=\"560\" width=\"160\" height=\"80\" fill=\"#f3e5f5\" stroke=\"#9c27b0\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"870\" y=\"580\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">SQLite DB</text>
      <text x=\"870\" y=\"597\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">bulletins.db</text>
      <text x=\"870\" y=\"610\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">Persistent storage</text>
      <text x=\"870\" y=\"623\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">Sync messages</text>
    </g>
    
    <!-- Sync Layer -->
    <g id=\"sync-layer\">
      <text x=\"600\" y=\"710\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">SYNC OPERATIONS (Optional)</text>
      
      <!-- Sync decision -->
      <polygon points=\"600,730 650,760 600,790 550,760\" fill=\"#ffe0b2\" stroke=\"#ff9800\" stroke-width=\"2\"/>
      <text x=\"600\" y=\"765\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Sync Enabled?</text>
      
      <!-- Yes branch - Build message -->
      <line x1=\"650\" y1=\"760\" x2=\"800\" y2=\"760\" stroke=\"#ff9800\" stroke-width=\"2\"/>
      <text x=\"720\" y=\"752\" font-size=\"9\" fill=\"#ff9800\">YES</text>
      
      <rect x=\"800\" y=\"730\" width=\"180\" height=\"60\" fill=\"#fff9e6\" stroke=\"#ff9800\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"890\" y=\"750\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">Build Sync Message</text>
      <text x=\"890\" y=\"765\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">BULLETIN|MAIL|DELETE|etc</text>
      
      <!-- BROADCAST message -->
      <line x1=\"890\" y1=\"790\" x2=\"890\" y2=\"830\" stroke=\"#ff9800\" stroke-width=\"2\"/>
      
      <rect x=\"800\" y=\"830\" width=\"180\" height=\"80\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"890\" y=\"850\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">send_message()</text>
      <text x=\"890\" y=\"867\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">→ Each BBS Node</text>
      <text x=\"890\" y=\"882\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">⚠ Multiple messages sent</text>
      <text x=\"890\" y=\"897\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">(One per sync node)</text>
      
      <!-- No branch -->
      <line x1=\"550\" y1=\"760\" x2=\"400\" y2=\"760\" stroke=\"#666\" stroke-width=\"2\" stroke-dasharray=\"5,5\"/>
      <text x=\"470\" y=\"752\" font-size=\"9\" fill=\"#666\">NO</text>
      
      <rect x=\"270\" y=\"730\" width=\"130\" height=\"60\" fill=\"#f0f0f0\" stroke=\"#999\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"335\" y=\"755\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">No Sync</text>
      <text x=\"335\" y=\"770\" font-size=\"9\" text-anchor=\"middle\" fill=\"#666\">(Local only)</text>
    </g>
    
    <!-- Response Layer -->
    <g id=\"response-layer\">
      <text x=\"600\" y=\"970\" font-size=\"14\" font-weight=\"bold\" text-anchor=\"middle\" fill=\"#0056d6\">RESPONSE TO USER</text>
      
      <rect x=\"350\" y=\"990\" width=\"500\" height=\"100\" fill=\"#c8e6c9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"600\" y=\"1010\" font-size=\"11\" text-anchor=\"middle\" font-weight=\"bold\">send_message() via Meshtastic</text>
      <text x=\"600\" y=\"1028\" font-size=\"10\" text-anchor=\"middle\" fill=\"#333\">Response sent back to user device</text>
      <text x=\"600\" y=\"1043\" font-size=\"10\" text-anchor=\"middle\" fill=\"#333\">Max 200 bytes per chunk (chunked if needed)</text>
      <text x=\"600\" y=\"1058\" font-size=\"10\" text-anchor=\"middle\" fill=\"#333\">2 second delay between chunks</text>
      <text x=\"600\" y=\"1073\" font-size=\"10\" text-anchor=\"middle\" fill=\"#333\">Message acknowledgement requested</text>
    </g>
    
    <!-- Summary Legend -->
    <g id=\"legend\">
      <text x=\"50\" y=\"1190\" font-size=\"12\" font-weight=\"bold\" fill=\"#222\">Key Data Flows:</text>
      
      <line x1=\"50\" y1=\"1210\" x2=\"90\" y2=\"1210\" stroke=\"#0056d6\" stroke-width=\"2\"/>
      <text x=\"100\" y=\"1215\" font-size=\"10\" fill=\"#333\">Primary flow (one message)</text>
      
      <line x1=\"50\" y1=\"1240\" x2=\"90\" y2=\"1240\" stroke=\"#666\" stroke-width=\"1\" stroke-dasharray=\"5,5\"/>
      <text x=\"100\" y=\"1245\" font-size=\"10\" fill=\"#333\">Branching/conditional paths</text>
      
      <rect x=\"50\" y=\"1260\" width=\"20\" height=\"20\" fill=\"#ffccbc\" stroke=\"#ff5722\" stroke-width=\"2\"/>
      <text x=\"100\" y=\"1275\" font-size=\"10\" fill=\"#333\">Bottleneck: Multiple messages sent (see notes below)</text>
      
      <text x=\"50\" y=\"1320\" font-size=\"11\" font-weight=\"bold\" fill=\"#b91c1c\">⚠ Current Issue - Message Multiplication:</text>
      <text x=\"50\" y=\"1340\" font-size=\"9\" fill=\"#333\">• Each sync-enabled operation sends N messages (one per BBS node in network)</text>
      <text x=\"50\" y=\"1355\" font-size=\"9\" fill=\"#333\">• Example: 1 user posts bulletin + 3 sync nodes = 3 messages sent to mesh</text>
      <text x=\"50\" y=\"1370\" font-size=\"9\" fill=\"#333\">• This can be optimized by: batching, local-only mode, broadcast instead of unicast</text>
    </g>
  </svg>
</div>

<div class=\"card\">
  <h3>How to Use This View</h3>
  <ul style=\"margin: 0; padding-left: 20px;\">
    <li><strong>Blue boxes</strong> = Core processing functions</li>
    <li><strong>Green boxes</strong> = Command handlers and database operations</li>
    <li><strong>Orange/Red boxes</strong> = Sync operations (where message multiplication happens)</li>
    <li><strong>Solid lines</strong> = Primary data flow</li>
    <li><strong>Dashed lines</strong> = Branching/conditional paths</li>
  </ul>
  <p class=\"muted\" style=\"margin-top: 16px;\">Next step: You can modify the system to reduce message counts by configuring sync behavior, implementing batch messages, or switching to broadcast mode.</p>
</div>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("BBS_WEBGUI_SECRET", "change-this-secret")
    app.config["DB_PATH"] = os.getenv("BBS_DB_PATH", "bulletins.db")
    app.config["CONFIG_PATH"] = os.getenv("BBS_CONFIG_PATH", "config.ini")
    app.config["ADMIN_USER"] = os.getenv("BBS_WEBGUI_USER", "admin")
    app.config["ADMIN_PASSWORD"] = os.getenv("BBS_WEBGUI_PASSWORD", "change-me")
    app.config["BULLETIN_BOARDS"] = load_bulletin_boards()

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
      config = configparser.ConfigParser()
      config.read(app.config["CONFIG_PATH"])
      if not config.has_section("boards"):
        config.add_section("boards")
      config.set("boards", "bulletin_boards", ",".join(boards))
      with open(app.config["CONFIG_PATH"], "w", encoding="utf-8") as config_file:
        config.write(config_file)

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

    @app.route("/settings/boards", methods=["GET", "POST"])
    @login_required
    def board_settings():
      boards = app.config["BULLETIN_BOARDS"]
      boards_text = ",".join(boards)
      env_override = bool(os.getenv("BBS_BULLETIN_BOARDS", "").strip())

      if request.method == "POST":
        raw_boards = request.form.get("bulletin_boards", "")
        normalized = raw_boards.replace("\n", ",")
        updated_boards = [item.strip() for item in normalized.split(",") if item.strip()]

        if not updated_boards:
          flash("At least one board is required.", "error")
        else:
          save_bulletin_boards(updated_boards)
          app.config["BULLETIN_BOARDS"] = updated_boards
          boards_text = ",".join(updated_boards)
          flash("Board list saved.", "success")

      content = render_template_string(
        BOARD_SETTINGS_CONTENT,
        boards_text=boards_text,
        env_override=env_override,
      )
      return render_template_string(BASE_TEMPLATE, title="Board Settings", content=content, show_nav=True)

    @app.route("/settings/admin", methods=["GET", "POST"])
    @login_required
    def admin_settings():
      if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_username = request.form.get("new_username", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        # Verify current password
        if current_password != app.config["ADMIN_PASSWORD"]:
          flash("Current password is incorrect.", "error")
        elif new_password != confirm_password:
          flash("New passwords do not match.", "error")
        elif new_password and len(new_password) < 4:
          flash("New password must be at least 4 characters.", "error")
        else:
          # Update credentials
          config = configparser.ConfigParser()
          config.read(app.config["CONFIG_PATH"])
          
          if not config.has_section("admin"):
            config.add_section("admin")
          
          if new_username:
            config.set("admin", "username", new_username)
            app.config["ADMIN_USER"] = new_username
          
          if new_password:
            config.set("admin", "password", new_password)
            app.config["ADMIN_PASSWORD"] = new_password
          
          with open(app.config["CONFIG_PATH"], "w", encoding="utf-8") as config_file:
            config.write(config_file)
          
          flash("Credentials updated successfully. Use your new credentials on next login.", "success")
          return redirect(url_for("logout"))

      content = render_template_string(
        ADMIN_SETTINGS_CONTENT,
        current_username=app.config["ADMIN_USER"],
      )
      return render_template_string(BASE_TEMPLATE, title="Admin Settings", content=content, show_nav=True)

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

      content = render_template_string(
        CLIENTS_CONTENT,
        rows=rows,
        total_posts=total_posts,
      )
      return render_template_string(BASE_TEMPLATE, title="Clients", content=content, show_nav=True)

    @app.route("/system/flowchart")
    @login_required
    def system_flowchart():
      content = render_template_string(FLOWCHART_CONTENT)
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
