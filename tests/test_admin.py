"""Issue #10: administrator controls and audit logging (CHAT-F-015, F-016, F-017, CHAT-SR-002,
CHAT-SR-003, CHAT-SR-005; TC-ADMIN-01, TC-ADMIN-02, TC-LOG-01, TC-SEC-04, TC-SEC-06)."""
import io
import json
import logging
import sqlite3

import pytest

from server import manage
from server.audit.audit_logger import REDACTED, RedactionFilter, redact
from server.protocol.framing import ConnectionClosed

PASSWORD = "password-123"


@pytest.fixture
def admin_setup(make_server, connect):
    """A server with an administrator 'root' logged in, and a function to log in more users."""
    server, address = make_server()
    first = connect(address)
    first.reply_to(first.request("REGISTER", {"username": "root", "password": PASSWORD}))
    server.repo.set_user_role("root", "ADMIN")

    def user(name):
        return connect(address).sign_in(name, PASSWORD)

    return server, user("root"), user


def admin(client, action, target, **args):
    payload = {"action": action, "target": target}
    if args:
        payload["args"] = args
    return client.reply_to(client.request("ADMIN", payload))


def login(client, name):
    return client.reply_to(client.request("LOGIN", {"username": name, "password": PASSWORD}))


# --- authorisation ---

def test_ordinary_users_cannot_use_admin_actions(admin_setup):
    server, _, user = admin_setup
    alice, bob = user("alice"), user("bob")
    for action, target in (("deactivate", "bob"), ("remove_room", "general"), ("activate", "alice")):
        reply = admin(alice, action, target)
        assert reply["type"] == "ERROR" and reply["code"] == "FORBIDDEN"
    ping = bob.request("PING")
    assert bob.recv() == {"type": "PONG", "ref_seq": ping}  # bob was not touched
    denied = [e for e in server.repo.audit_entries() if e["Outcome"] == "denied: not an administrator"]
    assert [(e["Username"], e["Action"]) for e in denied] == [
        ("alice", "admin:deactivate"), ("alice", "admin:remove_room"), ("alice", "admin:activate")]


def test_admin_role_is_reported_at_login(admin_setup, connect):
    server, root, _ = admin_setup
    root.reply_to(root.request("LOGOUT"))
    again = connect(server.connections.address)
    assert login(again, "root")["data"]["role"] == "ADMIN"


def test_unknown_action_and_targets(admin_setup):
    _, root, _ = admin_setup
    assert admin(root, "explode", "bob")["code"] == "INVALID_MESSAGE"
    assert admin(root, "deactivate", "ghost")["code"] == "NOT_FOUND"
    assert admin(root, "remove_room", "nowhere")["code"] == "NOT_FOUND"
    assert admin(root, "deactivate", "root")["code"] == "FORBIDDEN"


# --- account control ---

def test_deactivating_an_online_user_logs_them_out(admin_setup, connect):
    server, root, user = admin_setup
    bob = user("bob")
    reply = admin(root, "deactivate", "BOB")
    assert reply["status"] == "DONE"
    assert reply["data"] == {"action": "deactivate", "target": "bob", "status": "DEACTIVATED"}

    assert bob.next_event("account")["data"]["status"] == "DEACTIVATED"
    with pytest.raises((ConnectionClosed, ConnectionError)):
        bob.recv(notifications=True)
    assert server.presence.get("bob") is None
    assert "bob" not in server.presence.online_users()


@pytest.mark.parametrize("action", ["deactivate", "restrict"])
def test_blocked_accounts_cannot_log_in_until_reactivated(admin_setup, connect, action):
    server, root, user = admin_setup
    user("bob").close()
    admin(root, action, "bob")
    retry = connect(server.connections.address)
    assert login(retry, "bob")["code"] == "AUTH_FAILED"  # same error as a wrong password (SAD R9)
    assert admin(root, "activate", "bob")["data"]["status"] == "ACTIVE"
    assert login(retry, "bob")["status"] == "LOGGED_IN"


# --- room management ---

def test_restrict_and_reopen_a_room(admin_setup):
    _, root, user = admin_setup
    alice = user("alice")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    root.reply_to(root.request("JOIN", {"room": "general"}))

    assert admin(root, "restrict_room", "general")["data"]["status"] == "RESTRICTED"
    assert alice.next_event("room")["data"] == {"action": "restricted", "room": "general"}
    assert alice.reply_to(alice.request("MSG", {"room": "general", "text": "hi"}))["code"] == "FORBIDDEN"
    assert root.reply_to(root.request("MSG", {"room": "general", "text": "admins can still post"}))["status"] \
        in ("DELIVERED", "STORED_NOT_DELIVERED")

    assert admin(root, "open_room", "general")["data"]["status"] == "OPEN"
    assert alice.reply_to(alice.request("MSG", {"room": "general", "text": "hi again"}))["type"] == "ACK"


def test_rename_a_room(admin_setup):
    _, root, user = admin_setup
    alice = user("alice")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "random"}))

    assert admin(root, "rename_room", "general", name="lobby")["data"]["name"] == "lobby"
    while (event := alice.next_event("room")["data"])["action"] != "renamed":
        pass
    assert event == {"action": "renamed", "room": "general", "name": "lobby"}
    assert alice.reply_to(alice.request("MSG", {"room": "lobby", "text": "same room"}))["type"] == "ACK"
    assert admin(root, "rename_room", "lobby", name="random")["code"] == "DUPLICATE"
    assert admin(root, "rename_room", "lobby", name=" bad")["code"] == "INVALID_MESSAGE"
    assert admin(root, "rename_room", "lobby")["code"] == "INVALID_MESSAGE"


def test_remove_a_room(admin_setup):
    server, root, user = admin_setup
    alice = user("alice")
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    alice.reply_to(alice.request("MSG", {"room": "general", "text": "kept for the record"}))

    assert admin(root, "remove_room", "general")["data"]["status"] == "REMOVED"
    while (event := alice.next_event("room")["data"])["action"] != "removed":
        pass
    assert alice.reply_to(alice.request("JOIN", {"room": "general"}))["code"] == "NOT_FOUND"
    assert alice.reply_to(alice.request("HISTORY", {"room": "general"}))["code"] == "NOT_FOUND"
    with sqlite3.connect(server.config["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM RoomMember").fetchone()[0] == 0
        assert db.execute("SELECT Content FROM Message").fetchall() == [("kept for the record",)]


# --- audit log ---

def test_security_and_admin_events_are_audited(admin_setup, connect):
    server, root, user = admin_setup
    alice = user("alice")
    bad = connect(server.connections.address)
    bad.reply_to(bad.request("LOGIN", {"username": "alice", "password": "wrong password"}))
    bad.reply_to(bad.request("LOGIN", {"username": "nobody", "password": "whatever1"}))
    alice.reply_to(alice.request("CREATE_ROOM", {"room": "general"}))
    admin(root, "restrict", "alice")

    entries = [(e["Username"], e["Action"], e["Entity"], e["Outcome"]) for e in server.repo.audit_entries()]
    assert ("alice", "register", "alice", "success") in entries
    assert ("alice", "login", "alice", "success") in entries
    assert ("alice", "login", "alice", "failed: wrong password") in entries
    assert (None, "login", "nobody", "failed: unknown user") in entries
    assert ("alice", "create_room", "general", "success") in entries
    assert ("root", "admin:restrict", "alice", "success") in entries
    assert ("alice", "session_end", None, "ended by an administrator") in entries


def test_audit_log_is_append_only(admin_setup):
    server, _, _ = admin_setup
    server.repo.flush()
    with sqlite3.connect(server.config["db_path"]) as db:
        assert db.execute("SELECT COUNT(*) FROM AuditLog").fetchone()[0] > 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE AuditLog SET Outcome = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM AuditLog")


def test_no_passwords_or_tokens_in_the_audit_log(admin_setup):
    server, root, user = admin_setup
    alice = user("alice")
    server.repo.flush()
    with sqlite3.connect(server.config["db_path"]) as db:
        dump = "\n".join(db.iterdump())
    assert PASSWORD not in dump
    assert alice.session not in dump and root.session not in dump


# --- redaction ---

@pytest.mark.parametrize("raw", [
    'payload {"username": "alice", "password": "hunter2-secret"}',
    "password=hunter2-secret",
    "Password: hunter2-secret",
    "'session': 'abc-secret-token'",
    "token=abc-secret-token",
])
def test_redact_masks_secret_fields(raw):
    cleaned = redact(raw)
    assert "hunter2-secret" not in cleaned and "abc-secret-token" not in cleaned
    assert REDACTED in cleaned


def test_redact_masks_bare_session_tokens():
    import secrets
    token = secrets.token_urlsafe(32)
    assert redact(f"request with {token} attached") == f"request with {REDACTED} attached"
    assert redact("an ordinary log line about alice") == "an ordinary log line about alice"


def test_redaction_filter_applies_to_formatted_messages():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("test.redaction")
    logger.addHandler(handler)
    try:
        logger.warning("login with %s", {"username": "alice", "password": "hunter2-secret"})
    finally:
        logger.removeHandler(handler)
    assert "hunter2-secret" not in stream.getvalue()
    assert REDACTED in stream.getvalue()


def test_server_log_file_is_redacted(tmp_path):
    from server.main import configure_logging
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging({"log_path": str(tmp_path / "logs" / "server.log")})
        logging.getLogger("test.file").warning("oops password=hunter2-secret")
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                handler.close()
                root.removeHandler(handler)
    text = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    assert "hunter2-secret" not in text and REDACTED in text


# --- server console command ---

def test_make_admin_command(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"db_path": str(tmp_path / "chat.db"), "log_path": str(tmp_path / "s.log")}))
    from server.persistence.repository import Repository
    repo = Repository(str(tmp_path / "chat.db"))
    repo.create_user("alice", b"h", b"s")
    repo.close()

    assert manage.main(["--config", str(config), "make-admin", "ALICE"]) == 0
    assert "alice is now ADMIN" in capsys.readouterr().out
    assert manage.main(["--config", str(config), "make-admin", "ghost"]) == 1
    assert manage.main(["--config", str(config), "audit"]) == 0
    assert "set_role:ADMIN" in capsys.readouterr().out

    repo = Repository(str(tmp_path / "chat.db"))
    assert repo.get_user("alice")["Role"] == "ADMIN"
    repo.close()
