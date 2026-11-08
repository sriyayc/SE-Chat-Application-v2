# Implementation Notes and Deviations from the SAD

The SAD, Mini-Project Part-2 and the role-based task division ask for any difference between the
implementation and the submitted documents to be written down and explained at the final demo.
This file tracks those differences. Update it whenever the code and the SAD disagree.

## Sprint 1 (issues #1-#6)

### Planned for later sprints (not deviations, just not built yet)
| SAD item | Status | Issue |
|---|---|---|
| TLS 1.2+ with a pinned server certificate | Done in Sprint 2 (PR #24). | #11 |
| Session idle timeout and sweeper | The token is checked on every request; idle expiry is not enforced yet. | #11 |
| Chat rooms, presence events, join/leave notifications | Done in Sprint 2. | #8 |
| Message persistence and history | Done in Sprint 2. | #9 |
| Admin service and audit log with redaction | Done in Sprint 2. | #10 |

### Differences from the SAD
| # | SAD says | Implementation | Why |
|---|---|---|---|
| D1 | Offline recipient: `ACK {status: STORED_NOT_DELIVERED}` | Resolved in Sprint 2 (#9): messages are stored and the status is `STORED_NOT_DELIVERED`, with a `reason`. | |
| D2 | Oversized frame: rejected before reading the body | Still rejected from the length prefix alone. Bodies up to 1 MB are then skipped (not buffered) so the client can carry on; larger ones get `INVALID_MESSAGE` and the connection is closed. | Without skipping or closing, the TCP stream would be out of sync after the rejection. |
| D3 | Data Repository: all writes through one writer thread | Chat messages go through a dedicated writer thread (#9). Account, session and room changes are written directly under the same lock, so there is still only one writer at a time. | Those changes must be finished before the reply is sent (for example, a new room must exist before `CREATED` is returned). |
| D4 | Module layout lists components only | Added `server/app.py` (wires the layers), `server/config.py` (settings), `server/protocol/dispatcher.py` (passes validated requests to services), `server/protocol/messages.py` (ACK/ERROR/EVENT builders and error codes). | Keeps each SAD component in its own module; no new responsibilities. |
| D5 | Client Network Handler packs/unpacks messages | The client reuses `server/protocol/framing.py` instead of a copy. | One implementation of the wire format for both sides. |
| D6 | Not specified | Usernames are unique ignoring case (`Alice` and `alice` are the same account); the stored spelling is shown. | Prevents look-alike accounts. |
| D7 | Not specified | One active login per account; a second login gets `FORBIDDEN`. | Direct messages are routed by username, so one connection per user keeps delivery unambiguous. |
| D8 | Not specified | Usernames: 3-32 of `A-Z a-z 0-9 _ . -`; passwords 8-128 characters at registration; room names 1-32 of letters, digits, spaces, `_ . -`. | The SRS asks for "valid account information" without defining it. |
| D9 | Error codes table | `REGISTER`/`LOGIN` while already logged in returns `FORBIDDEN`. | Uses an existing code; no new codes were added. |

## Sprint 2

### Differences from the SAD
| # | SAD says | Implementation | Why |
|---|---|---|---|
| D10 | RoomMember has a Status column | No Status column: a row means "is a member" and leaving deletes the row. | Membership has only two states, so a row's presence is enough. |
| D11 | Not specified | The `LOGIN` ACK also returns `online` (users online now) and `rooms` (every room with its member count and whether you are in it). | The client needs both to draw its sidebar; the SAD has no separate list request. |
| D12 | Room Status OPEN / RESTRICTED / REMOVED | RESTRICTED rooms are read-only and closed to new members for everyone except administrators. REMOVED rooms behave as if they do not exist. | The SAD names the states without defining them. |
| D13 | Not specified | A room creator becomes its first member. A server holds at most `max_rooms` rooms; the next `CREATE_ROOM` gets `FORBIDDEN`. Room names are unique ignoring case. | Matches the `max_rooms` setting and the username rule (D6). |
| D14 | HISTORY: `room or with, before_ts, limit` | As in the SAD. `limit` defaults to 50 (maximum 100), `before_ts` defaults to now, and the reply has `more: true` when older messages may exist. Room history needs current membership; a direct conversation can only be read by its two participants. | The SAD lists the fields but not the defaults or the permission rule. |
| D15 | Not specified how an account becomes an administrator | `python -m server.manage make-admin <username>`, run on the server machine. There is no network request that grants the role. | A client must never be able to make itself an administrator (CHAT-SR-003). |
| D16 | ADMIN: `action, target, args` | Actions: `activate`, `deactivate`, `restrict` (target is a username); `open_room`, `restrict_room`, `remove_room`, `rename_room` with `args.name` (target is a room). Reply `ACK DONE {action, target, ...}`. Admins cannot change their own account. Deactivated or restricted users who are online get `EVENT account {status}` and are disconnected. | The SAD lists the request fields but not the actions. |
| D17 | AuditLog insert-only | Enforced by database triggers that reject UPDATE and DELETE on AuditLog. Records are written by the background writer thread, like messages. Log files get a redaction filter for passwords and session tokens, and on Linux/macOS are created readable by the server account only (umask 077). Windows has no equivalent permission bits, so there the folder's own permissions apply. | Makes "insert-only" a database guarantee, not just a convention. |

## Protocol summary (as implemented)
Every frame is a 4-byte big-endian length followed by a UTF-8 JSON object, at most `max_frame_bytes`.

Client requests: `{"type", "seq", "payload", "session"}`. `session` is required after login.

| Request | Payload | Success reply |
|---|---|---|
| `REGISTER` | `username`, `password` | `ACK REGISTERED` |
| `LOGIN` | `username`, `password` | `ACK LOGGED_IN {session, username, role, online, rooms}` |
| `MSG` | `to`, `text` | `ACK DELIVERED {to, ts}` or `ACK STORED_NOT_DELIVERED {to, ts, reason}` |
| `MSG` | `room`, `text` | `ACK DELIVERED {room, ts, delivered_to}` or `ACK STORED_NOT_DELIVERED {room, ts, reason}`; `FORBIDDEN` if not a member |
| `CREATE_ROOM` | `room` | `ACK CREATED {room, members}` |
| `JOIN` | `room` | `ACK JOINED {room, members}` |
| `LEAVE` | `room` | `ACK LEFT {room}` |
| `ADMIN` | `action`, `target`, optional `args` | `ACK DONE {action, target, status or name}`; `FORBIDDEN` for non-administrators |
| `HISTORY` | `room` or `with`, optional `before_ts`, `limit` | `ACK HISTORY {room or with, messages: [{id, from, to or room, text, ts}], more}` (oldest first) |
| `LOGOUT` | none | `ACK LOGGED_OUT`, then the server closes the connection |
| `PING` | none | `PONG` |

The recipient gets `EVENT {kind: "msg", data: {from, to, seq, text, ts}}`, or `{from, room, seq, text, ts}`
for a room message.

Other events:
- `presence {user, status: online|offline}` goes to everyone online.
- `room {action: created, room}` goes to everyone online.
- `join {room, user}` and `leave {room, user}` go to the room's other online members.
- `room {action: restricted|opened|removed, room}` and `room {action: renamed, room, name}` go to everyone online.
- `account {status, reason}` goes to a user an administrator has just deactivated or restricted, before they are disconnected.
Errors are `ERROR {ref_seq, code, reason}` with the codes from the SAD.
