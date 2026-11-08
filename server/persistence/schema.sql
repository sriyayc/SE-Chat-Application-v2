-- Tables: User, ChatRoom, RoomMember, Message, Session, AuditLog (see SAD)
-- Message and AuditLog are added with history (#9) and audit logging (#10).

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
