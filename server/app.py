"""Wires the server layers together."""
import threading

from server.persistence.repository import Repository
from server.protocol.dispatcher import Dispatcher
from server.services.auth import AuthService
from server.services.presence import Presence
from server.services.rooms import RoomService
from server.services.router import Router
from server.services.sessions import SessionService
from server.transport.connection_manager import ConnectionManager


class ChatServer:
    def __init__(self, config):
        self.config = config
        self.repo = Repository(config["db_path"])
        self.presence = Presence()
        self.rooms = RoomService(self.repo, self.presence, config["max_rooms"])
        self.dispatcher = Dispatcher(
            config,
            auth=AuthService(self.repo),
            sessions=SessionService(self.repo),
            presence=self.presence,
            router=Router(self.presence, self.repo, self.rooms),
            rooms=self.rooms,
        )
        self.connections = ConnectionManager(config, self.dispatcher.handle, self.dispatcher.disconnected)
        self._thread = None

    def serve_forever(self):
        self.connections.serve_forever()

    def start(self):
        """Run in a background thread and return the bound (host, port)."""
        self._thread = threading.Thread(target=self.serve_forever, name="accept", daemon=True)
        self._thread.start()
        if not self.connections.ready.wait(5):
            raise RuntimeError("server did not start")
        return self.connections.address

    def stop(self):
        self.connections.stop()
        if self._thread is not None:
            self._thread.join(5)
        self.repo.close()
