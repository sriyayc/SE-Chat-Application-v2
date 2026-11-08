"""Server maintenance commands, run on the server machine with access to the database.

    python -m server.manage make-admin <username>     give an account the ADMIN role
    python -m server.manage remove-admin <username>   back to an ordinary USER
    python -m server.manage audit [--limit N]         show the latest audit log entries

There is no network request that grants the ADMIN role, so a client can never make
itself an administrator (CHAT-SR-003). Role changes take effect at the user's next login.
"""
import argparse
import sys

from server.audit.audit_logger import AuditLogger
from server.config import ConfigError, load_config
from server.persistence.repository import Repository


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Chat server maintenance")
    parser.add_argument("--config", help="path to config.json (default: ./config.json if present)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("make-admin", "remove-admin"):
        commands.add_parser(name).add_argument("username")
    commands.add_parser("audit").add_argument("--limit", type=int, default=50)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    repo = Repository(config["db_path"])
    try:
        if args.command == "audit":
            for e in repo.audit_entries(args.limit):
                print(f"{e['Timestamp']}  {e['Username'] or '-':<16} {e['Action']:<22} "
                      f"{e['Entity'] or '-':<20} {e['Outcome']}")
            return 0
        role = "ADMIN" if args.command == "make-admin" else "USER"
        if not repo.set_user_role(args.username, role):
            print(f"There is no user called {args.username}.", file=sys.stderr)
            return 1
        record = repo.get_user(args.username)
        AuditLogger(repo).record(f"set_role:{role}", "success (server console)",
                                 user_id=record["UserID"], entity=record["Username"])
        print(f"{record['Username']} is now {role}. This takes effect at their next login.")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    sys.exit(main())
