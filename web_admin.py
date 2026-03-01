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
    input[type=text], input[type=password], textarea, select { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 6px; }
    textarea { min-height: 180px; }
    .row-actions { display: flex; gap: 8px; }
    .btn { border: 1px solid #bbb; border-radius: 6px; padding: 6px 10px; background: #fff; cursor: pointer; }
    .btn-primary { border-color: #0056d6; color: #fff; background: #0056d6; }
    .btn-danger { border-color: #b91c1c; color: #fff; background: #b91c1c; }
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
  {% if can_create %}
  <div style=\"margin-bottom: 12px;\">
    <a class=\"btn btn-primary\" href=\"{{ url_for('bulletin_new') }}\">New Bulletin Post</a>
  </div>
  {% endif %}
  <form method=\"get\" class=\"search-bar\">
    <input type=\"text\" name=\"q\" placeholder=\"Search {{ table_name }}\" value=\"{{ search_query }}\">
    <button class=\"btn\" type=\"submit\">Search</button>
    <a class=\"btn\" href=\"{{ url_for('table_list', table=table_name) }}\">Clear</a>
  </form>
  <p class=\"muted\">Rows: {{ rows|length }} | DB: <code>{{ db_path }}</code></p>
  <table>
    <thead>
      <tr>
        {% for col in columns %}
          <th>{{ col }}</th>
        {% endfor %}
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
        <tr>
          {% for col in columns %}
            <td>{{ row[col] }}</td>
          {% endfor %}
          <td>
            <div class=\"row-actions\">
              <a class=\"btn\" href=\"{{ url_for('table_edit', table=table_name, row_id=row['id']) }}\">Edit</a>
              <form method=\"post\" action=\"{{ url_for('table_delete', table=table_name, row_id=row['id']) }}\" class=\"inline\" onsubmit=\"return confirm('Delete this row?');\">
                <button type=\"submit\" class=\"btn btn-danger\">Delete</button>
              </form>
            </div>
          </td>
        </tr>
      {% endfor %}
      {% if not rows %}
        <tr>
          <td colspan=\"{{ columns|length + 1 }}\" class=\"muted\">No rows found.</td>
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


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.getenv("BBS_WEBGUI_SECRET", "change-this-secret")
    app.config["DB_PATH"] = os.getenv("BBS_DB_PATH", "bulletins.db")
    app.config["ADMIN_USER"] = os.getenv("BBS_WEBGUI_USER", "admin")
    app.config["ADMIN_PASSWORD"] = os.getenv("BBS_WEBGUI_PASSWORD", "change-me")
    app.config["BULLETIN_BOARDS"] = load_bulletin_boards()

    def initialize_db_safety() -> None:
      with sqlite3.connect(app.config["DB_PATH"], timeout=30) as conn:
          conn.execute("PRAGMA journal_mode=WAL;")
          conn.execute("PRAGMA synchronous=FULL;")
          conn.execute("PRAGMA busy_timeout=5000;")

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
            can_create=(table == "bulletins"),
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
