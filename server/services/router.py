"""Message delivery, ordering, delivery status, history.

A message is put straight onto each recipient's outbound queue. Each connection is
read by one thread and written by one thread, so messages from one sender reach a
recipient in the order they were sent (CHAT-F-008). Storing messages for history
comes with #9.
"""
import logging
import time

from server.protocol import messages
from server.services.rooms import RoomError, is_admin

log = logging.getLogger(__name__)


class Router:
    def __init__(self, presence, repo, rooms):
        self.presence = presence
        self.repo = repo
        self.rooms = rooms

    def route(self, sender, request):
        """Deliver a validated MSG request and return the ACK or ERROR for the sender."""
        if "room" in request.payload:
            return self._route_room(sender, request)
        return self._route_direct(sender, request)

    def _route_direct(self, sender, request):
        payload = request.payload
        to = payload["to"]
        sender_name = sender.user["username"]
        if to.casefold() == sender_name.casefold():
            return messages.error(request.seq, messages.INVALID_MESSAGE, "you cannot message yourself")

        ts = time.time()
        recipient = self.presence.get(to)
        if recipient is None:
            if self.repo.get_user(to) is None:
                return messages.error(request.seq, messages.NOT_FOUND, f"there is no user called {to}")
            return self._not_delivered(request.seq, {"to": to}, ts, f"{to} is offline")

        recipient_name = recipient.user["username"]
        delivered = recipient.send(messages.event("msg", {
            "from": sender_name, "to": recipient_name, "seq": request.seq, "text": payload["text"], "ts": ts}))
        if not delivered:
            return self._not_delivered(request.seq, {"to": recipient_name}, ts, f"{recipient_name} is offline")
        return messages.ack(request.seq, "DELIVERED", {"to": recipient_name, "ts": ts})

    def _route_room(self, sender, request):
        user = sender.user
        try:
            room = self.rooms.for_member(user, request.payload["room"])
        except RoomError as e:
            return messages.error(request.seq, e.code, e.reason)
        name = room["RoomName"]
        if room["Status"] == "RESTRICTED" and not is_admin(user):
            return messages.error(request.seq, messages.FORBIDDEN, f"{name} is read-only")

        ts = time.time()
        event = messages.event("msg", {
            "from": user["username"], "room": name, "seq": request.seq, "text": request.payload["text"], "ts": ts})
        delivered = sum(conn.send(event) for conn in self.rooms.online_members(room, exclude=user))
        if not delivered:
            return self._not_delivered(request.seq, {"room": name}, ts, f"no one else in {name} is online")
        return messages.ack(request.seq, "DELIVERED", {"room": name, "ts": ts, "delivered_to": delivered})

    @staticmethod
    def _not_delivered(seq, target, ts, reason):
        # CHAT-F-010: tell the sender no recipient was available.
        return messages.ack(seq, "NOT_DELIVERED", {**target, "ts": ts, "reason": reason})
