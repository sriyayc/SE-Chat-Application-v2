"""Who is online; presence events (CHAT-F-004).

Keeps one connection per logged-in user, keyed case-insensitively to match the
username column, and tells everyone else online when a user comes or goes.
"""
import threading

from server.protocol import messages


class Presence:
    def __init__(self):
        self._online = {}  # casefolded username -> (username as stored, connection)
        self._lock = threading.Lock()

    def add(self, username, conn):
        """Register a logged-in connection. Returns False if the user is already online elsewhere."""
        with self._lock:
            key = username.casefold()
            if key in self._online:
                return False
            self._online[key] = (username, conn)
        self.broadcast(messages.event("presence", {"user": username, "status": "online"}), exclude=conn)
        return True

    def remove(self, username, conn):
        """Unregister a connection. Returns True if the user went offline."""
        with self._lock:
            key = username.casefold()
            entry = self._online.get(key)
            if entry is None or entry[1] is not conn:
                return False
            del self._online[key]
        self.broadcast(messages.event("presence", {"user": entry[0], "status": "offline"}))
        return True

    def get(self, username):
        with self._lock:
            entry = self._online.get(username.casefold())
        return entry[1] if entry else None

    def online_users(self):
        with self._lock:
            return sorted((name for name, _ in self._online.values()), key=str.casefold)

    def broadcast(self, message, exclude=None):
        """Send a message to every logged-in connection except `exclude`."""
        with self._lock:
            targets = [conn for _, conn in self._online.values() if conn is not exclude]
        for conn in targets:
            conn.send(message)
