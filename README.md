# akidebug

akidebug is a server-rendered Flask telemetry collection site backed by MySQL, running in production mode (waitress as the WSGI server, debug off). It provides two features: anonymous upload and an admin panel.

## Setup

```bash
pip install -r requirements.txt   # or: python3 -m pip install -r requirements.txt
```

Edit `config.ini` with your MySQL credentials and admin password (the database and table are created automatically on first start), then:

```bash
python3 app.py
```

## Configuration (config.ini)

| Section | Keys | Defaults | Description |
|---|---|---|---|
| `[mysql]` | `host` / `port` / `user` / `password` / `database` | 127.0.0.1 / 3306 / root / empty / akidebug | MySQL connection |
| `[admin]` | `password` | `admin123` | Admin panel password |
| `[flask]` | `secret_key` | `change-me-to-a-random-string` | Flask session secret — must be changed in production |
| `[server]` | `host` / `port` | 0.0.0.0 / 5000 | Listen address |

## Feature 1: Anonymous Telemetry Upload

Base64-encode a telemetry JSON payload (must contain a `version` field; put the error in an `error` field) and pass it as the GET parameter `data`:

```bash
DATA=$(echo -n '{"version":"1.0.0","error":"NullPointerException at line 42"}' | base64)
curl "http://127.0.0.1:5000/upload?data=${DATA}"
```

This endpoint is meant to be opened in a browser: it returns an HTML result page showing "Submitted" with a reference ID on success (HTTP 200), or the failure reason on error (HTTP 400).

Notes:
- URL-safe base64 (`-` / `_`) is accepted, and missing padding is restored automatically.
- If there is no `error` field, `message` is used instead; otherwise the whole JSON payload is stored.
- Payload size is limited to 64KB.

## Feature 2: Admin Panel

Open `http://127.0.0.1:5000/admin` in a browser and log in with the admin password to:

- View all telemetry records (ID, time, version, error content)
- Set the processing status of each record (Pending / In Progress / Resolved)
