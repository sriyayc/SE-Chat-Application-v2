"""Only module that touches SQLite; single writer thread.

All queries are parameterised and foreign keys are enforced. Access is serialised
through one lock, so there is only ever one writer. Chat messages are saved by a
background writer thread, so delivering a message never waits for the disk
(SAD: forward first, then persist).
"""
import logging
import os
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")

log = logging.getLogger(__name__)


class DuplicateError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Repository:
    def __init__(self, db_path):
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA.read_text(encoding="utf-8"))
        self._writes = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, name="db-writer", daemon=True)
        self._writer.start()

    def close(self):
        """Finish any queued writes, then close the database."""
        self._writes.put(None)
        self._writer.join(10)
        with self._lock:
            self._db.close()

    def _write_loop(self):
        while True:
            job = self._writes.get()
            try:
                if job is None:
                    return
                sql, params = job
                with self._lock:
                    self._db.execute(sql, params)
            except sqlite3.Error:
                # A failed write must not stop the writer or the server (CHAT-NF-006).
                log.exception("could not save to the database")
            finally:
                self._writes.task_done()

    def flush(self):
        """Wait until every queued write has been saved."""
        self._writes.join()

    def create_user(self, username, password_hash, salt, role="USER"):
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO User (Username, PasswordHash, Salt, Role, CreatedAt) VALUES (?, ?, ?, ?, ?)",
                    (username, password_hash, salt, role, now()))
        except sqlite3.IntegrityError as e:
            raise DuplicateError(username) from e
        return cur.lastrowid

    def get_user(self, username):
        with self._lock:
            row = self._db.execute(
                "SELECT UserID, Username, PasswordHash, Salt, Role, Status FROM User WHERE Username = ?",
                (username,)).fetchone()
        return dict(row) if row else None

    def create_session(self, user_id, token_hash):
        ts = now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO Session (UserID, TokenHash, ConnectedAt, LastActivity) VALUES (?, ?, ?, ?)",
                (user_id, token_hash, ts, ts))
        return cur.lastrowid

    def end_session(self, session_id):
        with self._lock:
            self._db.execute(
                "UPDATE Session SET DisconnectedAt = ? WHERE SessionID = ? AND DisconnectedAt IS NULL",
                (now(), session_id))

    # ----- rooms (#8) -----

    def create_room(self, name, created_by):
        """Create a room and make its creator a member, in one transaction."""
        ts = now()
        try:
            with self._lock:
                self._db.execute("BEGIN")
                try:
                    cur = self._db.execute(
                        "INSERT INTO ChatRoom (RoomName, CreatedBy, CreatedAt) VALUES (?, ?, ?)",
                        (name, created_by, ts))
                    self._db.execute(
                        "INSERT INTO RoomMember (RoomID, UserID, JoinedAt) VALUES (?, ?, ?)",
                        (cur.lastrowid, created_by, ts))
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as e:
            raise DuplicateError(name) from e
        return cur.lastrowid

    def get_room(self, name):
        with self._lock:
            row = self._db.execute(
                "SELECT RoomID, RoomName, CreatedBy, Status FROM ChatRoom WHERE RoomName = ?",
                (name,)).fetchone()
        return dict(row) if row else None

    def count_rooms(self):
        """Rooms that still count towards max_rooms (everything not removed)."""
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM ChatRoom WHERE Status != 'REMOVED'").fetchone()[0]

    def list_rooms(self, user_id):
        """Every room that has not been removed, with its member count and whether user_id is a member."""
        with self._lock:
            rows = self._db.execute(
                """SELECT r.RoomName, r.Status,
                          (SELECT COUNT(*) FROM RoomMember m WHERE m.RoomID = r.RoomID) AS Members,
                          EXISTS (SELECT 1 FROM RoomMember m
                                  WHERE m.RoomID = r.RoomID AND m.UserID = ?) AS Joined
                   FROM ChatRoom r WHERE r.Status != 'REMOVED' ORDER BY r.RoomName COLLATE NOCASE""",
                (user_id,)).fetchall()
        return [{"name": r["RoomName"], "status": r["Status"], "members": r["Members"],
                 "joined": bool(r["Joined"])} for r in rows]

    def add_member(self, room_id, user_id):
        """Returns False if the user was already a member."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO RoomMember (RoomID, UserID, JoinedAt) VALUES (?, ?, ?)",
                (room_id, user_id, now()))
        return cur.rowcount == 1

    def remove_member(self, room_id, user_id):
        """Returns False if the user was not a member."""
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM RoomMember WHERE RoomID = ? AND UserID = ?", (room_id, user_id))
        return cur.rowcount == 1

    def is_member(self, room_id, user_id):
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM RoomMember WHERE RoomID = ? AND UserID = ?",
                (room_id, user_id)).fetchone() is not None

    def member_names(self, room_id):
        with self._lock:
            rows = self._db.execute(
                """SELECT u.Username FROM RoomMember m JOIN User u ON u.UserID = m.UserID
                   WHERE m.RoomID = ? ORDER BY u.Username COLLATE NOCASE""",
                (room_id,)).fetchall()
        return [r["Username"] for r in rows]

    # ----- messages and history (#9) -----

    def save_message(self, sender_id, text, seq, ts, room_id=None, recipient_id=None):
        """Queue a chat message for the writer thread. Exactly one of room_id / recipient_id is set."""
        self._writes.put((
            "INSERT INTO Message (SenderID, RoomID, RecipientID, Content, SenderSeq, Timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sender_id, room_id, recipient_id, text, seq, ts)))

    def room_history(self, room_id, before_ts, limit):
        """Up to `limit` room messages sent before `before_ts`, oldest first."""
        self.flush()
        with self._lock:
            rows = self._db.execute(
                """SELECT m.MessageID, u.Username AS Sender, m.Content, m.Timestamp
                   FROM Message m JOIN User u ON u.UserID = m.SenderID
                   WHERE m.RoomID = ? AND m.Timestamp < ?
                   ORDER BY m.Timestamp DESC, m.MessageID DESC LIMIT ?""",
                (room_id, before_ts, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def direct_history(self, user_id, other_id, before_ts, limit):
        """Up to `limit` direct messages between two users sent before `before_ts`, oldest first."""
        self.flush()
        with self._lock:
            rows = self._db.execute(
                """SELECT m.MessageID, s.Username AS Sender, r.Username AS Recipient, m.Content, m.Timestamp
                   FROM Message m
                   JOIN User s ON s.UserID = m.SenderID
                   JOIN User r ON r.UserID = m.RecipientID
                   WHERE ((m.SenderID = ? AND m.RecipientID = ?) OR (m.SenderID = ? AND m.RecipientID = ?))
                     AND m.Timestamp < ?
                   ORDER BY m.Timestamp DESC, m.MessageID DESC LIMIT ?""",
                (user_id, other_id, other_id, user_id, before_ts, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
