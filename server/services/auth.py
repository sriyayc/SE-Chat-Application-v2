"""Register, login, password hashing (scrypt).

Passwords are hashed with hashlib.scrypt and a random 16-byte salt per user, and
compared with hmac.compare_digest (CHAT-SR-002). Wrong passwords, unknown users and
blocked accounts all produce the same AUTH_FAILED error, and unknown users still pay
for a hash, so a caller cannot tell which usernames exist (SAD risk R9).
"""
import hashlib
import hmac
import logging
import os

from server.persistence.repository import DuplicateError
from server.protocol import messages

log = logging.getLogger(__name__)

SALT_BYTES = 16
SCRYPT_PARAMS = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 64, "maxmem": 64 * 1024 * 1024}


class AuthError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def hash_password(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT_PARAMS)


class AuthService:
    def __init__(self, repo, audit):
        self.repo = repo
        self.audit = audit
        self._dummy_salt = os.urandom(SALT_BYTES)

    def register(self, username, password):
        salt = os.urandom(SALT_BYTES)
        try:
            user_id = self.repo.create_user(username, hash_password(password, salt), salt)
        except DuplicateError:
            self.audit.record("register", "refused: username taken", entity=username)
            raise AuthError(messages.DUPLICATE, "that username is already taken") from None
        self.audit.record("register", "success", user_id=user_id, entity=username)

    def authenticate(self, username, password):
        """Return the user record, or raise AuthError(AUTH_FAILED)."""
        user = self.repo.get_user(username)
        if user is None:
            hash_password(password, self._dummy_salt)  # same cost as a real check
            reason = "unknown user"
        elif not hmac.compare_digest(hash_password(password, user["Salt"]), user["PasswordHash"]):
            reason = "wrong password"
        elif user["Status"] != "ACTIVE":
            reason = f"account {user['Status'].lower()}"
        else:
            self.audit.record("login", "success", user_id=user["UserID"], entity=user["Username"])
            return user
        log.warning("login failed for %r: %s", username, reason)
        self.audit.record("login", f"failed: {reason}", user_id=user["UserID"] if user else None, entity=username)
        raise AuthError(messages.AUTH_FAILED, "invalid username or password")
