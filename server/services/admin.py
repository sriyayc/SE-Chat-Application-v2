"""Admin-only user and room management (CHAT-F-015, CHAT-F-016, CHAT-SR-003).

Every request is checked against the caller's role before anything happens, and
every attempt, allowed or not, goes to the audit log.

ADMIN {action, target, args}:
    activate | deactivate | restrict      target = username
    open_room | restrict_room | remove_room target = room name
    rename_room                           target = room name, args = {"name": new name}

Deactivating or restricting a user who is online ends their session straight away.
"""
import logging

from server.persistence.repository import DuplicateError
from server.protocol import messages
from server.protocol.validator import ROOM_RE
from server.services.rooms import RoomError, is_admin

log = logging.getLogger(__name__)

USER_ACTIONS = {"activate": "ACTIVE", "deactivate": "DEACTIVATED", "restrict": "RESTRICTED"}
ROOM_STATUS_ACTIONS = {"open_room": "OPEN", "restrict_room": "RESTRICTED"}
ROOM_ACTIONS = (*ROOM_STATUS_ACTIONS, "remove_room", "rename_room")


class AdminService:
    def __init__(self, repo, presence, rooms, audit, end_session):
        self.repo = repo
        self.presence = presence
        self.rooms = rooms
        self.audit = audit
        self.end_session = end_session  # callback(conn): ends a session and removes the user from presence

    def handle(self, conn, request):
        """Carry out a validated ADMIN request and return the ACK or ERROR for the caller."""
        user = conn.user
        action, target = request.payload["action"], request.payload["target"]
        args = request.payload.get("args", {})

        def fail(code, reason, outcome=None):
            self.audit.record(f"admin:{action}", outcome or f"refused: {reason}", user=user, entity=target)
            return messages.error(request.seq, code, reason)

        if not is_admin(user):
            log.warning("%r tried admin action %r without permission", user["username"], action)
            return fail(messages.FORBIDDEN, "only administrators can do that", outcome="denied: not an administrator")
        if action in USER_ACTIONS:
            return self._user_action(request.seq, user, action, target, fail)
        if action in ROOM_ACTIONS:
            return self._room_action(request.seq, user, action, target, args, fail)
        known = ", ".join((*USER_ACTIONS, *ROOM_ACTIONS))
        return fail(messages.INVALID_MESSAGE, f"unknown admin action; use one of: {known}")

    def _user_action(self, seq, admin, action, target, fail):
        record = self.repo.get_user(target)
        if record is None:
            return fail(messages.NOT_FOUND, f"there is no user called {target}")
        if record["UserID"] == admin["id"]:
            return fail(messages.FORBIDDEN, "you cannot change your own account")
        status = USER_ACTIONS[action]
        self.repo.set_user_status(record["UserID"], status)
        name = record["Username"]
        if status != "ACTIVE":
            self._disconnect(name, status)
        self.audit.record(f"admin:{action}", "success", user=admin, entity=name)
        return messages.ack(seq, "DONE", {"action": action, "target": name, "status": status})

    def _disconnect(self, username, status):
        """End the session of a user who has just been deactivated or restricted (CHAT-F-015)."""
        conn = self.presence.get(username)
        if conn is None:
            return
        conn.send(messages.event("account", {"status": status, "reason": "changed by an administrator"}))
        self.end_session(conn, "ended by an administrator")
        conn.close_after_flush()

    def _room_action(self, seq, admin, action, target, args, fail):
        try:
            room = self.rooms.find(target)
        except RoomError as e:
            return fail(e.code, e.reason)
        name = room["RoomName"]
        if action in ROOM_STATUS_ACTIONS:
            status = ROOM_STATUS_ACTIONS[action]
            self.repo.set_room_status(room["RoomID"], status)
            event = {"action": "restricted" if status == "RESTRICTED" else "opened", "room": name}
            result = {"status": status}
        elif action == "remove_room":
            self.repo.remove_room(room["RoomID"])
            event = {"action": "removed", "room": name}
            result = {"status": "REMOVED"}
        else:
            new_name = args.get("name")
            if not isinstance(new_name, str) or not ROOM_RE.fullmatch(new_name) or new_name != new_name.strip():
                return fail(messages.INVALID_MESSAGE, "args.name must be a valid room name")
            try:
                self.repo.rename_room(room["RoomID"], new_name)
            except DuplicateError:
                return fail(messages.DUPLICATE, "a room with that name already exists")
            event = {"action": "renamed", "room": name, "name": new_name}
            result = {"name": new_name}
        self.presence.broadcast(messages.event("room", event))
        self.audit.record(f"admin:{action}", "success", user=admin, entity=name)
        return messages.ack(seq, "DONE", {"action": action, "target": name, **result})
