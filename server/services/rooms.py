"""Room create/join/leave, membership, join/leave events (CHAT-F-005, F-006, F-013).

Rooms and memberships are stored in the database, so a user stays in their rooms
across logins. Join and leave notifications go to the room's other online members.
A RESTRICTED room is read-only for everyone except administrators and accepts no
new members; a REMOVED room behaves as if it does not exist.
"""
import logging

from server.persistence.repository import DuplicateError
from server.protocol import messages

log = logging.getLogger(__name__)


class RoomError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def is_admin(user):
    return user["role"] == "ADMIN"


class RoomService:
    def __init__(self, repo, presence, max_rooms):
        self.repo = repo
        self.presence = presence
        self.max_rooms = max_rooms

    def find(self, name):
        """Return the room record, or raise NOT_FOUND."""
        room = self.repo.get_room(name)
        if room is None or room["Status"] == "REMOVED":
            raise RoomError(messages.NOT_FOUND, f"there is no room called {name}")
        return room

    def for_member(self, user, name):
        """Return the room if `user` is a member of it, or raise NOT_FOUND / FORBIDDEN (CHAT-SR-003)."""
        room = self.find(name)
        if not self.repo.is_member(room["RoomID"], user["id"]):
            raise RoomError(messages.FORBIDDEN, f"you are not a member of {room['RoomName']}")
        return room

    def create(self, user, name):
        if self.repo.count_rooms() >= self.max_rooms:
            raise RoomError(messages.FORBIDDEN, f"the server already has the maximum of {self.max_rooms} rooms")
        try:
            self.repo.create_room(name, user["id"])
        except DuplicateError:
            raise RoomError(messages.DUPLICATE, "a room with that name already exists") from None
        room = self.find(name)
        log.info("%r created room %r", user["username"], room["RoomName"])
        self.presence.broadcast(messages.event("room", {"action": "created", "room": room["RoomName"]}))
        return room

    def join(self, user, name):
        room = self.find(name)
        if room["Status"] == "RESTRICTED" and not is_admin(user):
            raise RoomError(messages.FORBIDDEN, f"{room['RoomName']} is closed to new members")
        if self.repo.add_member(room["RoomID"], user["id"]):
            log.info("%r joined room %r", user["username"], room["RoomName"])
            self.notify(room, "join", {"room": room["RoomName"], "user": user["username"]}, exclude=user)
        return room

    def leave(self, user, name):
        room = self.find(name)
        if not self.repo.remove_member(room["RoomID"], user["id"]):
            raise RoomError(messages.NOT_FOUND, f"you are not a member of {room['RoomName']}")
        log.info("%r left room %r", user["username"], room["RoomName"])
        self.notify(room, "leave", {"room": room["RoomName"], "user": user["username"]}, exclude=user)
        return room

    def members(self, room):
        return self.repo.member_names(room["RoomID"])

    def online_members(self, room, exclude=None):
        """Connections of the room's members who are online, except the user `exclude`."""
        skip = exclude["username"].casefold() if exclude else None
        conns = (self.presence.get(name) for name in self.members(room) if name.casefold() != skip)
        return [conn for conn in conns if conn is not None]

    def notify(self, room, kind, data, exclude=None):
        event = messages.event(kind, data)
        for conn in self.online_members(room, exclude):
            conn.send(event)

    def room_list(self, user):
        return self.repo.list_rooms(user["id"])
