# Chat Application using Socket Programming
Software Engineering project: a Python client-server chat app over TCP sockets with length-prefixed JSON messages.

## Layout
- `server/transport` - connections and threads
- `server/protocol` - message framing and validation
- `server/services` - auth, sessions, presence, rooms, routing, admin
- `server/persistence` - SQLite access
- `server/audit` - audit log
- `server/config.py` - settings loader
- `client/ui`, `client/net` - Tkinter client
- `tests` - pytest suite
- `documents` - SRS, SAD, test plan and diagrams

## Requirements
Python 3.11 or later. Everything except pytest comes from the standard library.

## Setup
Windows (PowerShell):
```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
Linux / macOS:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration
The server runs with the defaults in `config.example.json`. To change them, copy it to `config.json`
(git-ignored) and edit the values you need, or pass `--config PATH`. Unknown keys are rejected.

| Setting | Default | Meaning |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `5050` | Where the server listens |
| `max_frame_bytes` | `8192` | Largest message the server accepts |
| `max_text_chars` | `2000` | Longest chat message text |
| `max_connections` | `100` | Simultaneous client connections |
| `heartbeat_seconds` | `30` | Client heartbeat interval |
| `db_path` / `log_path` | `data/chat.db` / `logs/server.log` | Database and log locations |

The server certificate goes in `certs/`. The private key is git-ignored and must never be committed.

## Running
Run these from the project root, with the virtual environment active.
```
python -m server.main                 # start the server
python -m client.main                 # start a client (connects to 127.0.0.1:5050)
python -m client.main --host 192.168.1.20 --port 5050
```

## Administration
Run these on the server machine, from the project root:
```
python -m server.manage make-admin alice     # give alice the administrator role (next login)
python -m server.manage remove-admin alice
python -m server.manage audit --limit 50     # latest audit log entries
```
An administrator can then type commands into the client's message box, for example
`/admin deactivate bob`, `/admin restrict_room general` or `/admin rename_room general lobby`.
Type `/admin` on its own for the list.

## Tests
```
pytest
```
