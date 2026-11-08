"""Audit logging with password/token redaction (CHAT-F-017, CHAT-SR-002, CHAT-SR-005).

Security and administrative events are written to the AuditLog table, which the
database only lets the server append to, and to the server log. Every log handler
gets a RedactionFilter, so a password or session token that ends up in a log
message is masked before it is written anywhere.
"""
import logging
import re

REDACTED = "[REDACTED]"

# key=value / "key": "value" pairs whose value is a secret.
_SECRET_FIELD = re.compile(r"""(?i)(["']?(?:password|passwd|session|token)["']?\s*[:=]\s*)(["']?)[^"',\s}]+""")
# Session tokens are secrets.token_urlsafe(32): 43 URL-safe characters.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")


def redact(text):
    text = _SECRET_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return _TOKEN.sub(REDACTED, text)


class RedactionFilter(logging.Filter):
    """Masks passwords and session tokens in every record a handler writes."""

    def filter(self, record):
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


class AuditLogger:
    def __init__(self, repo):
        self.repo = repo
        self.log = logging.getLogger("audit")

    def record(self, action, outcome, user=None, entity=None, user_id=None):
        """Record who did what to which entity, and the outcome. `user` is a logged-in user dict."""
        if user is not None:
            user_id = user["id"]
        self.repo.add_audit(user_id, action, entity, outcome)
        who = user["username"] if user is not None else (f"user {user_id}" if user_id else "-")
        self.log.info("%s %s: %s on %s", who, action, outcome, entity or "-")
