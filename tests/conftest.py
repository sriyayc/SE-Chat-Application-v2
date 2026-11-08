
import socket
import ssl

import pytest

from server.app import ChatServer
from server.config import load_config
from server.protocol.framing import encode, read_frame


class RawClient:
    """A TLS client that speaks the framed protocol for server tests."""

    def __init__(self, address, cert_path, timeout=5):
        context = ssl.create_default_context(cafile=cert_path)
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        raw_sock = socket.create_connection(address, timeout=timeout)
        try:
            self.sock = context.wrap_socket(
                raw_sock,
                server_hostname="127.0.0.1",
            )
        except Exception:
            raw_sock.close()
            raise

        self._seq = 0
        self.session = None

    def send(self, message):
        self.sock.sendall(encode(message))

    def request(self, message_type, payload=None, **extra):
        self._seq += 1
        message = {
            "type": message_type,
            "seq": self._seq,
            "payload": payload or {},
        }
        if self.session:
            message["session"] = self.session
        message.update(extra)
        self.send(message)
        return self._seq

    # Notifications most tests do not care about (presence, rooms, join/leave).
    NOTIFICATIONS = frozenset({"presence", "room", "join", "leave"})

    def recv(self, notifications=False):
        """Read the next message, skipping notification events unless asked for them."""
        while True:
            message = read_frame(self.sock, 1 << 20)
            if notifications or message.get("kind") not in self.NOTIFICATIONS:
                return message

    def next_event(self, kind):
        """Read until an EVENT of the given kind arrives."""
        while True:
            message = self.recv(notifications=True)
            if message.get("type") == "EVENT" and message.get("kind") == kind:
                return message

    def reply_to(self, seq):
        """Read until the ACK/ERROR for seq arrives, skipping events."""
        while True:
            message = self.recv()
            if message.get("ref_seq") == seq:
                return message

    def sign_in(self, username, password="password-123"):
        """Register if needed and log in."""
        self.reply_to(
            self.request(
                "REGISTER",
                {"username": username, "password": password},
            )
        )
        reply = self.reply_to(
            self.request(
                "LOGIN",
                {"username": username, "password": password},
            )
        )
        assert reply["type"] == "ACK", reply
        self.session = reply["data"]["session"]
        return self

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def make_server(tmp_path):
    servers = []

    def start(**overrides):
        project_root = __import__("pathlib").Path(__file__).resolve().parents[1]
        cert_path = project_root / "certs" / "server.crt"
        key_path = project_root / "certs" / "server.key"

        settings = {
            "host": "127.0.0.1",
            "port": 0,
            "tls_cert_path": str(cert_path),
            "tls_key_path": str(key_path),
            "db_path": str(tmp_path / "chat.db"),
            "log_path": str(tmp_path / "server.log"),
        }
        settings.update(overrides)

        empty = tmp_path / "config.json"
        empty.write_text("{}", encoding="utf-8")

        server = ChatServer(load_config(empty, **settings))
        address = server.start()
        servers.append(server)
        return server, address

    yield start

    for server in servers:
        server.stop()


@pytest.fixture
def server_address(make_server):
    return make_server()[1]


@pytest.fixture
def connect(server_address):
    clients = []

    def open_client(address=server_address):
        from pathlib import Path

        cert_path = Path(__file__).resolve().parents[1] / "certs" / "server.crt"
        client = RawClient(address, cert_path=str(cert_path))
        clients.append(client)
        return client

    yield open_client

    for client in clients:
        client.close()
