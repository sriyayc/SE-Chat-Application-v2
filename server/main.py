"""Server entry point: python -m server.main [--config PATH] [--host HOST] [--port PORT]"""
import argparse
import logging
import logging.handlers
import os
import sys

from server.app import ChatServer
from server.audit.audit_logger import RedactionFilter
from server.config import ConfigError, load_config

log = logging.getLogger("server")


def configure_logging(config):
    """Rotating log file plus console, both with password/token redaction (CHAT-SR-005)."""
    if os.name == "posix":
        os.umask(0o077)  # log files and the database are readable by the server account only
    os.makedirs(os.path.dirname(config["log_path"]), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        config["log_path"], maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    if os.name == "posix":
        os.chmod(config["log_path"], 0o600)
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(RedactionFilter())
        root.addHandler(handler)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Chat server")
    parser.add_argument("--config", help="path to config.json (default: ./config.json if present)")
    parser.add_argument("--host", help="override the configured listen address")
    parser.add_argument("--port", type=int, help="override the configured port")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    overrides = {k: v for k, v in (("host", args.host), ("port", args.port)) if v is not None}
    try:
        config = load_config(args.config, **overrides)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    configure_logging(config)
    server = ChatServer(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    except OSError as e:
        log.error("cannot listen on %s:%s: %s", config["host"], config["port"], e)
        return 1
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
