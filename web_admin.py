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
  <h2>Message System Flowchart - Detailed Command Handlers</h2>
  <p class=\"muted\">Tree view showing command routing and communication messages between system components.</p>
</div>

<div class=\"card\" style=\"overflow-x: auto; background: #fafafa;\">
  <svg viewBox=\"0 0 1600 2200\" style=\"width: 100%; min-height: 2200px; border: 1px solid #ddd; border-radius: 8px;\">
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
      
      <rect x=\"1300\" y=\"360\" width=\"200\" height=\"60\" fill=\"#e8f5e9\" stroke=\"#4caf50\" stroke-width=\"2\" rx=\"5\"/>
      <text x=\"1400\" y=\"380\" font-size=\"10\" text-anchor=\"middle\" font-weight=\"bold\">Utilities [U]</text>
      <text x=\"1400\" y=\"395\" font-size=\"8\" text-anchor=\"middle\" fill=\"#666\">Various utilities</text>
      <line x1=\"1400\" y1=\"420\" x2=\"1400\" y2=\"460\" stroke=\"#4caf50\" stroke-width=\"1\"/>
      
      <!-- Utility sub-options -->
      <g id=\"utility-suboptions\">
        <!-- Stats -->
        <rect x=\"1300\" y=\"460\" width=\"90\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1345\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[S] Stats</text>
        <text x=\"1345\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">Count messages</text>
        <line x1=\"1345\" y1=\"510\" x2=\"1345\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1300\" y=\"540\" width=\"90\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1345\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send</text>
        <text x=\"1345\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">statistics</text>
        
        <!-- Fortune -->
        <rect x=\"1400\" y=\"460\" width=\"90\" height=\"50\" fill=\"#fff0f5\" stroke=\"#9c27b0\" stroke-width=\"1\" rx=\"3\"/>
        <text x=\"1445\" y=\"475\" font-size=\"8\" text-anchor=\"middle\" font-weight=\"bold\">[F] Fortune</text>
        <text x=\"1445\" y=\"488\" font-size=\"7\" text-anchor=\"middle\" fill=\"#666\">Random quote</text>
        <line x1=\"1445\" y1=\"510\" x2=\"1445\" y2=\"540\" stroke=\"#9c27b0\" stroke-width=\"1\"/>
        <rect x=\"1400\" y=\"540\" width=\"90\" height=\"40\" fill=\"#ffebee\" stroke=\"#b91c1c\" stroke-width=\"1\" rx=\"2\"/>
        <text x=\"1445\" y=\"553\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">📤 Send</text>
        <text x=\"1445\" y=\"565\" font-size=\"7\" text-anchor=\"middle\" fill=\"#333\">fortune</text>
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
  </svg>
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
