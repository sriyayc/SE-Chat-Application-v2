"""Message delivery, ordering, delivery status, history.

A message is put straight onto each recipient's outbound queue and then queued for
saving, so delivery never waits for the database. Each connection is read by one
thread and written by one thread, so messages from one sender reach a recipient in
the order they were sent (CHAT-F-008). Saved messages can be read back with HISTORY
by the people allowed to see them: the two people in a direct conversation, or the
current members of a room (CHAT-F-011, CHAT-F-012).
"""
import logging
import time

from server.protocol import messages
from server.services.rooms import RoomError, is_admin

log = logging.getLogger(__name__)

HISTORY_DEFAULT_LIMIT = 50


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
            record = self.repo.get_user(to)
            if record is None:
                return messages.error(request.seq, messages.NOT_FOUND, f"there is no user called {to}")
            recipient_id, recipient_name, delivered = record["UserID"], record["Username"], False
        else:
            recipient_id, recipient_name = recipient.user["id"], recipient.user["username"]
            delivered = recipient.send(messages.event("msg", {
                "from": sender_name, "to": recipient_name, "seq": request.seq, "text": payload["text"], "ts": ts}))

        self.repo.save_message(sender.user["id"], payload["text"], request.seq, ts, recipient_id=recipient_id)
        if not delivered:
            return self._stored(request.seq, {"to": recipient_name}, ts, f"{recipient_name} is offline")
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
        self.repo.save_message(user["id"], request.payload["text"], request.seq, ts, room_id=room["RoomID"])
        if not delivered:
            return self._stored(request.seq, {"room": name}, ts, f"no one else in {name} is online")
        return messages.ack(request.seq, "DELIVERED", {"room": name, "ts": ts, "delivered_to": delivered})

    def history(self, user, request):
        """Answer a validated HISTORY request with the permitted messages, oldest first."""
        payload = request.payload
        before = payload.get("before_ts", time.time() + 1)
        limit = payload.get("limit", HISTORY_DEFAULT_LIMIT)
        if "room" in payload:
            try:
                room = self.rooms.for_member(user, payload["room"])
            except RoomError as e:
                return messages.error(request.seq, e.code, e.reason)
            rows = self.repo.room_history(room["RoomID"], before, limit)
            target = {"room": room["RoomName"]}
            found = [{"id": r["MessageID"], "from": r["Sender"], "room": room["RoomName"],
                      "text": r["Content"], "ts": r["Timestamp"]} for r in rows]
        else:
            other = self.repo.get_user(payload["with"])
            if other is None:
                return messages.error(request.seq, messages.NOT_FOUND, f"there is no user called {payload['with']}")
            if other["UserID"] == user["id"]:
                return messages.error(request.seq, messages.INVALID_MESSAGE, "there is no conversation with yourself")
            rows = self.repo.direct_history(user["id"], other["UserID"], before, limit)
            target = {"with": other["Username"]}
            found = [{"id": r["MessageID"], "from": r["Sender"], "to": r["Recipient"],
                      "text": r["Content"], "ts": r["Timestamp"]} for r in rows]
        return messages.ack(request.seq, "HISTORY", {**target, "messages": found, "more": len(found) == limit})

    @staticmethod
    def _stored(seq, target, ts, reason):
        # CHAT-F-010: tell the sender no recipient was available; the message is kept for history.
        return messages.ack(seq, "STORED_NOT_DELIVERED", {**target, "ts": ts, "reason": reason})
