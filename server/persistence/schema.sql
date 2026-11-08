-- Tables: User, ChatRoom, RoomMember, Message, Session, AuditLog (see SAD)

CREATE TABLE IF NOT EXISTS User (
    UserID       INTEGER PRIMARY KEY AUTOINCREMENT,
    Username     TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    PasswordHash BLOB    NOT NULL,
    Salt         BLOB    NOT NULL,
    Role         TEXT    NOT NULL DEFAULT 'USER'   CHECK (Role IN ('USER', 'ADMIN')),
    Status       TEXT    NOT NULL DEFAULT 'ACTIVE' CHECK (Status IN ('ACTIVE', 'RESTRICTED', 'DEACTIVATED')),
    CreatedAt    TEXT    NOT NULL
);

-- Only a hash of the session token is stored.
CREATE TABLE IF NOT EXISTS Session (
    SessionID      INTEGER PRIMARY KEY AUTOINCREMENT,
    UserID         INTEGER NOT NULL REFERENCES User(UserID),
    TokenHash      TEXT    NOT NULL UNIQUE,
    ConnectedAt    TEXT    NOT NULL,
    LastActivity   TEXT    NOT NULL,
    DisconnectedAt TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_user ON Session(UserID);

CREATE TABLE IF NOT EXISTS ChatRoom (
    RoomID    INTEGER PRIMARY KEY AUTOINCREMENT,
    RoomName  TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    CreatedBy INTEGER NOT NULL REFERENCES User(UserID),
    Status    TEXT    NOT NULL DEFAULT 'OPEN' CHECK (Status IN ('OPEN', 'RESTRICTED', 'REMOVED')),
    CreatedAt TEXT    NOT NULL
);

-- A row means the user is a member; leaving deletes the row.
CREATE TABLE IF NOT EXISTS RoomMember (
    RoomID   INTEGER NOT NULL REFERENCES ChatRoom(RoomID),
    UserID   INTEGER NOT NULL REFERENCES User(UserID),
    JoinedAt TEXT    NOT NULL,
    PRIMARY KEY (RoomID, UserID)
);

CREATE INDEX IF NOT EXISTS idx_member_user ON RoomMember(UserID);

-- A message goes either to a room or to one user, never both.
CREATE TABLE IF NOT EXISTS Message (
    MessageID   INTEGER PRIMARY KEY AUTOINCREMENT,
    SenderID    INTEGER NOT NULL REFERENCES User(UserID),
    RoomID      INTEGER REFERENCES ChatRoom(RoomID),
    RecipientID INTEGER REFERENCES User(UserID),
    Content     TEXT    NOT NULL CHECK (length(Content) > 0),
    SenderSeq   INTEGER NOT NULL,
    Timestamp   REAL    NOT NULL,
    CHECK ((RoomID IS NULL) != (RecipientID IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_message_room ON Message(RoomID, Timestamp);
CREATE INDEX IF NOT EXISTS idx_message_direct ON Message(SenderID, RecipientID, Timestamp);

-- Security and administrative events. Insert-only: the triggers below reject any
-- UPDATE or DELETE, so a record cannot be changed once written (CHAT-SR-005).
CREATE TABLE IF NOT EXISTS AuditLog (
    AuditID   INTEGER PRIMARY KEY AUTOINCREMENT,
    UserID    INTEGER REFERENCES User(UserID),
    Action    TEXT    NOT NULL,
    Entity    TEXT,
    Outcome   TEXT    NOT NULL,
    Timestamp TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON AuditLog
BEGIN
    SELECT RAISE(ABORT, 'the audit log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON AuditLog
BEGIN
    SELECT RAISE(ABORT, 'the audit log is append-only');
END;
