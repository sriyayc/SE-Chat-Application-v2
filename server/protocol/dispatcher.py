"""Validates each decoded request and passes it to the service that handles it."""
import logging

from server.protocol import messages
from server.protocol.validator import PRE_LOGIN_TYPES, ValidationError, validate
from server.services.auth import AuthError
from server.services.rooms import RoomError

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, config, auth, sessions, presence, router, rooms, audit):
        self.max_text_chars = config["max_text_chars"]
        self.auth = auth
        self.sessions = sessions
        self.presence = presence
        self.router = router
        self.rooms = rooms
        self.audit = audit
        self.admin = None  # AdminService, set by ChatServer (it needs end_session)
        self._handlers = {
            "PING": self._ping,
            "REGISTER": self._register,
            "LOGIN": self._login,
            "LOGOUT": self._logout,
            "MSG": self._msg,
            "CREATE_ROOM": self._create_room,
            "JOIN": self._join,
            "LEAVE": self._leave,
            "HISTORY": self._history,
            "ADMIN": self._admin,
        }

    def handle(self, conn, message):
        try:
            request = validate(message, conn.authenticated, self.max_text_chars)
        except ValidationError as e:
            # Only the reason is logged, never the request itself: it may contain a password.
            log.warning("%r request rejected (%s): %s", conn, e.code, e.reason)
            conn.send(messages.error(e.ref_seq, e.code, e.reason))
            return
        if request.type not in PRE_LOGIN_TYPES and not self.sessions.matches(conn.session, request.session):
            conn.send(messages.error(request.seq, messages.SESSION_EXPIRED, "missing or invalid session token"))
            return
        handler = self._handlers.get(request.type)
        if handler is None:
            conn.send(messages.error(request.seq, messages.INVALID_MESSAGE, f"{request.type} is not supported yet"))
            return
        handler(conn, request)

    def disconnected(self, conn):
        self.end_session(conn)

    def _ping(self, conn, request):
        conn.send(messages.pong(request.seq))

    def _register(self, conn, request):
        if conn.authenticated:
            conn.send(messages.error(request.seq, messages.FORBIDDEN, "log out before registering a new account"))
            return
        try:
            self.auth.register(request.payload["username"], request.payload["password"])
        except AuthError as e:
            conn.send(messages.error(request.seq, e.code, e.reason))
            return
        conn.send(messages.ack(request.seq, "REGISTERED"))

    def _login(self, conn, request):
        if conn.authenticated:
            conn.send(messages.error(request.seq, messages.FORBIDDEN, "already logged in"))
            return
        try:
            record = self.auth.authenticate(request.payload["username"], request.payload["password"])
        except AuthError as e:
            conn.send(messages.error(request.seq, e.code, e.reason))
            return
        user = {"id": record["UserID"], "username": record["Username"], "role": record["Role"]}
        conn.user = user
        if not self.presence.add(user["username"], conn):
            conn.user = None
            log.info("login refused for %r: already online", user["username"])
            conn.send(messages.error(request.seq, messages.FORBIDDEN, "this account is already logged in elsewhere"))
            return
        conn.session = self.sessions.start(user["id"])
        conn.set_authenticated()
        log.info("%r logged in as %r", conn, user["username"])
        conn.send(messages.ack(request.seq, "LOGGED_IN", {
            "session": conn.session.token, "username": user["username"], "role": user["role"],
            "online": self.presence.online_users(), "rooms": self.rooms.room_list(user)}))

    def _logout(self, conn, request):
        log.info("%r logged out", conn)
        self.end_session(conn, "logout")
        conn.send(messages.ack(request.seq, "LOGGED_OUT"))
        conn.close_after_flush()

    def _msg(self, conn, request):
        conn.send(self.router.route(conn, request))

    def _admin(self, conn, request):
        conn.send(self.admin.handle(conn, request))

    def _history(self, conn, request):
        conn.send(self.router.history(conn.user, request))

    def _create_room(self, conn, request):
        try:
            room = self.rooms.create(conn.user, request.payload["room"])
        except RoomError as e:
            conn.send(messages.error(request.seq, e.code, e.reason))
            return
        self.audit.record("create_room", "success", user=conn.user, entity=room["RoomName"])
        conn.send(messages.ack(request.seq, "CREATED", {"room": room["RoomName"], "members": [conn.user["username"]]}))

    def _join(self, conn, request):
        try:
            room = self.rooms.join(conn.user, request.payload["room"])
        except RoomError as e:
            conn.send(messages.error(request.seq, e.code, e.reason))
            return
        self.audit.record("join_room", "success", user=conn.user, entity=room["RoomName"])
        conn.send(messages.ack(request.seq, "JOINED", {"room": room["RoomName"], "members": self.rooms.members(room)}))

    def _leave(self, conn, request):
        try:
            room = self.rooms.leave(conn.user, request.payload["room"])
        except RoomError as e:
            conn.send(messages.error(request.seq, e.code, e.reason))
            return
        self.audit.record("leave_room", "success", user=conn.user, entity=room["RoomName"])
        conn.send(messages.ack(request.seq, "LEFT", {"room": room["RoomName"]}))

    def end_session(self, conn, reason="disconnected"):
        """End a connection's session and take its user offline. Safe to call more than once."""
        if conn.user is not None:
            self.presence.remove(conn.user["username"], conn)
        if conn.session is not None:
            self.sessions.end(conn.session)
            conn.session = None
            self.audit.record("session_end", reason, user=conn.user)
