"""Later: a small, personal link queue."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request


BACKUP_LIMIT = 10 * 1024 * 1024


def valid_url(url):
    if not isinstance(url, str) or not url or len(url) > 4096:
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
                and parsed.port != 0 and not parsed.username and not parsed.password
                and not any(c.isspace() or ord(c) < 32 for c in url))
    except ValueError:
        return False


def validate_backup(data):
    """Validate everything before starting a transaction or changing saved links."""
    if data.get("app") != "Later" or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Choose a supported Later backup (version 1).")
    rows = data.get("items")
    if not isinstance(rows, list):
        raise ValueError("The backup must contain a list of links.")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "url", "description", "is_read", "starred", "created_at"}:
            raise ValueError("The backup contains an incomplete link.")
        identifier = row["id"]
        if type(identifier) is not int or not 0 < identifier < 2**53 or identifier in ids:
            raise ValueError("The backup contains invalid or duplicate link IDs.")
        ids.add(identifier)
        if (not valid_url(row["url"]) or not isinstance(row["description"], str)
                or len(row["description"]) > 2000 or type(row["is_read"]) is not bool
                or type(row["starred"]) is not bool):
            raise ValueError("The backup contains an invalid link or status.")
        timestamp = row["created_at"]
        if not isinstance(timestamp, str) or len(timestamp) > 40:
            raise ValueError("The backup contains an invalid date.")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            # Reject text that SQLite cannot store, including unpaired surrogates.
            (row["url"] + row["description"] + timestamp).encode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("The backup contains invalid text or dates.") from None
    last_id = data.get("last_item_id")
    if "last_item_id" not in data or (last_id is not None and (type(last_id) is not int or last_id not in ids)):
        raise ValueError("The backup's last pick does not match a saved link.")
    return rows, last_id


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DATABASE=os.environ.get("LATER_DATABASE", str(Path(app.instance_path) / "later.sqlite3")),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE"]).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    app.config["DATABASE"] = str(Path(app.config["DATABASE"]).expanduser().resolve())

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=15)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(error=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    with app.app_context():
        db().executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                starred INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                item_id INTEGER REFERENCES items(id)
            );
        """)

    def item(row):
        if row is None:
            return None
        result = dict(row)
        result["is_read"] = bool(result["is_read"])
        result["starred"] = bool(result["starred"])
        return result

    @app.before_request
    def protect_writes():
        if request.endpoint == "restore_backup":
            request.max_content_length = BACKUP_LIMIT
        if request.method in ("POST", "PATCH", "DELETE"):
            # A cross-origin form cannot supply this header or JSON; no CORS is enabled.
            if request.headers.get("X-Later") != "1" or not request.is_json:
                return jsonify(error="Use the Later app to make changes."), 403
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(error="Expected a JSON object."), 400

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="Backup files must be under 10 MB." if request.endpoint == "restore_backup" else "That entry is too large."), 413

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        counts = db().execute("SELECT COUNT(*) AS total, COALESCE(SUM(NOT is_read),0) AS unread, COALESCE(SUM(starred),0) AS starred FROM items").fetchone()
        last = db().execute("SELECT items.* FROM items JOIN state ON items.id=state.item_id WHERE state.key='last'").fetchone()
        return jsonify(counts=dict(counts), last=item(last))

    @app.get("/api/backup")
    def download_backup():
        connection = db()
        # Both reads share one SQLite snapshot, even while another device picks a link.
        with connection:
            connection.execute("BEGIN")
            rows = [item(row) for row in connection.execute("SELECT * FROM items ORDER BY id")]
            last = connection.execute("SELECT item_id FROM state WHERE key='last'").fetchone()
        response = jsonify(app="Later", version=1, items=rows,
                           last_item_id=last["item_id"] if last else None)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        response.headers["Content-Disposition"] = f'attachment; filename="later-backup-{date}.json"'
        return response

    @app.post("/api/restore")
    def restore_backup():
        try:
            rows, last_id = validate_backup(request.get_json())
        except ValueError as error:
            return jsonify(error=str(error)), 400
        connection = db()
        # Replace data atomically; validation and database errors leave the old queue intact.
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM state")
            connection.execute("DELETE FROM items")
            connection.executemany(
                "INSERT INTO items (id,url,description,is_read,starred,created_at) VALUES (:id,:url,:description,:is_read,:starred,:created_at)", rows)
            if last_id is not None:
                connection.execute("INSERT INTO state (key,item_id) VALUES ('last',?)", (last_id,))
        return jsonify(restored=len(rows))

    @app.post("/api/items")
    def add():
        data = request.get_json()
        url, description = data.get("url"), data.get("description", "")
        if not isinstance(url, str) or not isinstance(description, str):
            return jsonify(error="Enter a URL and an optional text description."), 400
        url, description = url.strip(), description.strip()
        if not valid_url(url) or len(description) > 2000:
            return jsonify(error="Use a full http:// or https:// URL (up to 4,096 characters) and a description up to 2,000 characters."), 400
        with db():
            cursor = db().execute("INSERT INTO items (url, description) VALUES (?, ?)", (url, description))
        return jsonify(item=item(db().execute("SELECT * FROM items WHERE id=?", (cursor.lastrowid,)).fetchone())), 201

    @app.post("/api/pop")
    def pop():
        mode = request.get_json().get("mode", "unread")
        filters = {"unread": "is_read=0", "all": "1=1", "starred": "starred=1"}
        if not isinstance(mode, str) or mode not in filters:
            return jsonify(error="Choose unread, all, or starred."), 400
        connection = db()
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM items WHERE " + filters[mode] + " ORDER BY RANDOM() LIMIT 1").fetchone()
            if row is None:
                return jsonify(error="No links in this selection yet."), 404
            connection.execute("UPDATE items SET is_read=1 WHERE id=?", (row["id"],))
            connection.execute("INSERT INTO state (key,item_id) VALUES ('last',?) ON CONFLICT(key) DO UPDATE SET item_id=excluded.item_id", (row["id"],))
            selected = connection.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone()
        return jsonify(item=item(selected))

    @app.get("/api/starred")
    def starred():
        return jsonify(items=[item(row) for row in db().execute("SELECT * FROM items WHERE starred=1 ORDER BY id DESC")])

    @app.patch("/api/items/<int:item_id>/star")
    def star(item_id):
        value = request.get_json().get("starred")
        if not isinstance(value, bool):
            return jsonify(error="Starred must be true or false."), 400
        with db():
            cursor = db().execute("UPDATE items SET starred=? WHERE id=?", (value, item_id))
        if not cursor.rowcount:
            return jsonify(error="Link not found."), 404
        return jsonify(item=item(db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()))

    return app


if __name__ == "__main__":
    from waitress import serve
    serve(create_app(), host=os.environ.get("LATER_HOST", "0.0.0.0"), port=int(os.environ.get("LATER_PORT", "5050")))
