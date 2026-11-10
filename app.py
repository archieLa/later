"""Later: a small, personal link queue."""
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request


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
        return jsonify(error="That entry is too large."), 413

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        counts = db().execute("SELECT COUNT(*) AS total, COALESCE(SUM(NOT is_read),0) AS unread, COALESCE(SUM(starred),0) AS starred FROM items").fetchone()
        last = db().execute("SELECT items.* FROM items JOIN state ON items.id=state.item_id WHERE state.key='last'").fetchone()
        return jsonify(counts=dict(counts), last=item(last))

    @app.post("/api/items")
    def add():
        data = request.get_json()
        url, description = data.get("url"), data.get("description", "")
        if not isinstance(url, str) or not isinstance(description, str):
            return jsonify(error="Enter a URL and an optional text description."), 400
        url, description = url.strip(), description.strip()
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname) and parsed.port != 0
            valid = valid and not parsed.username and not parsed.password and not any(c.isspace() or ord(c) < 32 for c in url)
        except ValueError:
            valid = False
        if not valid or len(url) > 4096 or len(description) > 2000:
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
