"""CLI: `python -m app.create_operator <username>` -- creates a new
operator account (prompts for a password, hidden input via `getpass`) so
they can log in via `POST /auth/login`. See `backend/CLAUDE.md`'s Auth
section and `docs/plan/P1_core_backend.md`'s Prompt 4.1.
"""

import argparse
import getpass
import sys

from sqlalchemy.exc import IntegrityError

from app import storage
from app.auth import hash_password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    args = parser.parse_args()

    password = getpass.getpass(f"Password for {args.username!r}: ")
    confirm = getpass.getpass("Confirm password: ")
    if not password:
        print("Password cannot be empty.", file=sys.stderr)
        return 1
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        return 1

    storage.init_db()
    try:
        operator_id = storage.create_operator(args.username, hash_password(password))
    except IntegrityError:
        print(f"Operator {args.username!r} already exists.", file=sys.stderr)
        return 1

    print(f"Created operator {args.username!r} (id={operator_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
