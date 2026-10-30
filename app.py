import base64
import binascii
import configparser
import json
import logging
import os
import ssl
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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

GOTIFY_URL = config.get("gotify", "url", fallback="")
GOTIFY_TOKEN = config.get("gotify", "token", fallback="")
GOTIFY_PRIORITY = config.getint("gotify", "priority", fallback=5)
GOTIFY_CA_FILE = config.get("gotify", "ca_file", fallback="")
GOTIFY_TIMEOUT = 5  # seconds

logger = logging.getLogger(__name__)


def build_gotify_ssl_context():
    """Build the SSL context used to verify the push server's certificate.

    Uses the user-provided CA file when configured (self-signed setups),
    otherwise falls back to the system trust store.
    """
    if GOTIFY_CA_FILE:
        ca_path = GOTIFY_CA_FILE
        if not os.path.isabs(ca_path):
            ca_path = os.path.join(BASE_DIR, ca_path)
        try:
            context = ssl.create_default_context(cafile=ca_path)
        except (OSError, ssl.SSLError) as exc:
            logger.warning("Gotify CA file %r is unusable (%s); "
                           "push notifications are disabled.", ca_path, exc)
            return None
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return context
    return ssl.create_default_context()


GOTIFY_ENABLED = bool(GOTIFY_URL and GOTIFY_TOKEN)
GOTIFY_SSL_CONTEXT = build_gotify_ssl_context() if GOTIFY_ENABLED else None
if GOTIFY_ENABLED and GOTIFY_SSL_CONTEXT is None:
    GOTIFY_ENABLED = False

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


# ---------- Push notification (Gotify) ----------

def send_gotify_notification(record_id, version, error):
    """Push a notification about a new telemetry record.

    Runs in a background thread; every failure is logged and swallowed so
    it can never affect the user-facing response.
    """
    try:
        error_summary = error if len(error) <= 500 else error[:500] + "..."
        body = urllib.parse.urlencode({
            "title": f"New telemetry #{record_id}",
            "message": f"version: {version}\n{error_summary}",
            "priority": str(GOTIFY_PRIORITY),
        }).encode("utf-8")
        req = urllib.request.Request(
            GOTIFY_URL,
            data=body,
            headers={
                "X-Gotify-Key": GOTIFY_TOKEN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=GOTIFY_TIMEOUT,
                                    context=GOTIFY_SSL_CONTEXT):
            pass
    except Exception as exc:  # noqa: BLE001 - push must never break uploads
        logger.warning("Gotify push for telemetry #%s failed: %s", record_id, exc)


def push_gotify_async(record_id, version, error):
    if GOTIFY_ENABLED:
        threading.Thread(
            target=send_gotify_notification,
            args=(record_id, version, error),
            daemon=True,
        ).start()


# ---------- Feature 1: anonymous telemetry upload ----------

def fail(message_key):
    """Render the result page with a 400 status on upload failure.

    message_key is a stable i18n key translated client-side in the
    upload_result template (see its I18N dictionary).
    """
    return render_template("upload_result.html", success=False,
                           message_key=message_key), 400


@app.route("/upload")
def upload():
    data = request.args.get("data", "")
    if not data:
        return fail("missing_data")
    if len(data) > MAX_DATA_LEN:
        return fail("payload_too_large")

    # Accept URL-safe base64 and restore any stripped padding
    raw_b64 = data.replace("-", "+").replace("_", "/")
    raw_b64 += "=" * (-len(raw_b64) % 4)
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError):
        return fail("invalid_base64")

    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fail("invalid_json")

    if not isinstance(payload, dict) or "version" not in payload:
        return fail("missing_version")

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
    push_gotify_async(record_id, version, error)
    return render_template(
        "upload_result.html", success=True, message_key="success_msg",
        record_id=record_id,
    )


# ---------- Homepage (statistics) ----------

@app.route("/")
def index():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM telemetry")
        total = cur.fetchone()["c"]
        cur.execute("SELECT status, COUNT(*) AS c FROM telemetry GROUP BY status")
        status_counts = {r["status"]: r["c"] for r in cur.fetchall()}
        cur.execute(
            "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM telemetry "
            "WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 29 DAY) "
            "GROUP BY DATE(created_at)"
        )
        daily_rows = {str(r["d"]): r["c"] for r in cur.fetchall()}
    # Fill in missing days with 0 so the line chart is continuous
    days, counts = [], []
    for i in range(29, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        days.append(day)
        counts.append(daily_rows.get(day, 0))
    return render_template(
        "index.html", total=total, status_counts=status_counts,
        statuses=STATUSES, days=days, counts=counts,
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
