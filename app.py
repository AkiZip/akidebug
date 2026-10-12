import base64
import binascii
import configparser
import json
import os
from datetime import datetime

import pymysql
from flask import (Flask, abort, g, redirect, render_template, request,
                   session, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- Configuration ----------

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")

MYSQL_HOST = config.get("mysql", "host", fallback="127.0.0.1")
MYSQL_PORT = config.getint("mysql", "port", fallback=3306)
MYSQL_USER = config.get("mysql", "user", fallback="root")
MYSQL_PASSWORD = config.get("mysql", "password", fallback="")
MYSQL_DATABASE = config.get("mysql", "database", fallback="akidebug")

ADMIN_PASSWORD = config.get("admin", "password", fallback="admin123")
SECRET_KEY = config.get("flask", "secret_key", fallback=os.urandom(24).hex())

SERVER_HOST = config.get("server", "host", fallback="0.0.0.0")
SERVER_PORT = config.getint("server", "port", fallback=5000)

MAX_DATA_LEN = 64 * 1024  # max length of the base64 parameter
STATUSES = ["Pending", "In Progress", "Resolved"]

app = Flask(__name__)
app.secret_key = SECRET_KEY


def connect_db(database=None):
    kwargs = dict(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    if database:
        kwargs["database"] = database
    return pymysql.connect(**kwargs)


def get_db():
    """One connection per request, closed when the request ends."""
    if "db" not in g:
        g.db = connect_db(MYSQL_DATABASE)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the database and table if they do not exist."""
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(f"USE `{MYSQL_DATABASE}`")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    version VARCHAR(255) NOT NULL,
                    error TEXT,
                    raw TEXT,
                    status VARCHAR(16) NOT NULL DEFAULT 'Pending',
                    created_at DATETIME NOT NULL
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """
            )
        conn.commit()
    finally:
        conn.close()


def admin_required():
    return bool(session.get("admin"))


# ---------- Feature 1: anonymous telemetry upload ----------

def fail(message):
    """Render the result page with a 400 status on upload failure."""
    return render_template("upload_result.html", success=False, message=message), 400


@app.route("/upload")
def upload():
    data = request.args.get("data", "")
    if not data:
        return fail("Missing 'data' parameter.")
    if len(data) > MAX_DATA_LEN:
        return fail("Payload too large.")

    # Accept URL-safe base64 and restore any stripped padding
    raw_b64 = data.replace("-", "+").replace("_", "/")
    raw_b64 += "=" * (-len(raw_b64) % 4)
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError):
        return fail("Invalid base64 data.")

    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fail("Invalid JSON payload.")

    if not isinstance(payload, dict) or "version" not in payload:
        return fail("JSON payload must contain a 'version' field.")

    version = str(payload["version"])
    error = payload.get("error", payload.get("message"))
    if error is None:
        error = json.dumps(payload, ensure_ascii=False)
    error = str(error)
    raw = json.dumps(payload, ensure_ascii=False)

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry (version, error, raw, created_at) VALUES (%s, %s, %s, %s)",
            (version, error, raw, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        record_id = cur.lastrowid
    conn.commit()
    return render_template(
        "upload_result.html", success=True,
        message="Telemetry submitted successfully. Thank you for your feedback!",
        record_id=record_id,
    )


# ---------- Feature 2: admin panel ----------

@app.route("/admin/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
def admin():
    if not admin_required():
        return redirect(url_for("login"))
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM telemetry ORDER BY id DESC")
        rows = cur.fetchall()
    return render_template("admin.html", rows=rows, statuses=STATUSES)


@app.route("/admin/status", methods=["POST"])
def set_status():
    if not admin_required():
        return redirect(url_for("login"))
    row_id = request.form.get("id", type=int)
    status = request.form.get("status", "")
    if row_id is None or status not in STATUSES:
        abort(400)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE telemetry SET status = %s WHERE id = %s", (status, row_id))
    conn.commit()
    return redirect(url_for("admin"))


init_db()

if __name__ == "__main__":
    # Production mode: waitress as the WSGI server, debug off
    from waitress import serve
    serve(app, host=SERVER_HOST, port=SERVER_PORT)
