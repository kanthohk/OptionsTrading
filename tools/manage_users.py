"""Manage dashboard logins in trading.db's ``users`` table.

Run ON THE DB SERVER, from the repo root — that's where trading.db lives.
Passwords are prompted for, never taken on the command line, so they don't
land in shell history or ``ps`` output.

    python tools/manage_users.py add sashi
    python tools/manage_users.py passwd sashi      # also clears a lockout
    python tools/manage_users.py disable sashi
    python tools/manage_users.py enable sashi       # also clears a lockout
    python tools/manage_users.py list
    python tools/manage_users.py list --db /path/to/trading.db
"""

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                os.pardir)))

from helpers import user_store  # noqa: E402


def _prompt_password():
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Confirm password: "):
        raise ValueError("passwords do not match")
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=user_store.DB_FILE,
                        help="path to trading.db (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "passwd", "disable", "enable"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"{args.db} not found. Run this from the repo root on the DB "
              f"server, or pass --db.", file=sys.stderr)
        return 1

    try:
        if args.command == "add":
            user_store.create_user(args.username, _prompt_password(), args.db)
            print(f"Created {args.username}.")
        elif args.command == "passwd":
            if not user_store.set_password(args.username, _prompt_password(),
                                           args.db):
                raise ValueError(f"no such user {args.username!r}")
            print(f"Password updated for {args.username}.")
        elif args.command in ("disable", "enable"):
            if not user_store.set_active(args.username,
                                         args.command == "enable", args.db):
                raise ValueError(f"no such user {args.username!r}")
            print(f"{args.username} {args.command}d. Tokens already issued "
                  f"stay valid until they expire.")
        else:
            users = user_store.list_users(args.db)
            if not users:
                print("No users. Add one with: "
                      "python tools/manage_users.py add <username>")
            for u in users:
                state = "active" if u["is_active"] else "DISABLED"
                if u["locked_until"]:
                    state += f", locked until {u['locked_until']}"
                print(f"{u['username']:<20} {state:<40} "
                      f"last login: {u['last_login_at'] or 'never'}")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
