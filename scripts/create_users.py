"""Create the two Drydock database users. Run once, with the Exasol admin account from .env.

    uv run python scripts/create_users.py

  DRYDOCK_SVC    the only writer: CREATE SESSION, SCHEMA, TABLE, VIEW, SCRIPT
  DRYDOCK_AGENT  CREATE SESSION only; read access to specific schemas is granted by setup.py

Passwords come from DRYDOCK_SVC_PASSWORD and DRYDOCK_AGENT_PASSWORD in .env and are never printed.
Users that already exist are left unchanged.
"""

from __future__ import annotations

__dialect_lint__ = "exempt"  # admin DDL (CREATE USER, GRANT), recorded in sql/DIALECT.md

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import IdentityMissing, connect, run  # noqa: E402

USERS = {
    "DRYDOCK_SVC": ("DRYDOCK_SVC_PASSWORD", "CREATE SESSION, CREATE SCHEMA, CREATE TABLE, CREATE VIEW, CREATE SCRIPT"),
    "DRYDOCK_AGENT": ("DRYDOCK_AGENT_PASSWORD", "CREATE SESSION"),
}


def main() -> int:
    missing = [var for var, _ in USERS.values() if not os.environ.get(var)]
    if missing:
        print(f"Set {', '.join(missing)} in .env first (any strong password).", file=sys.stderr)
        return 2
    for var, _ in USERS.values():
        if '"' in os.environ[var]:
            print(f"{var} must not contain a double quote.", file=sys.stderr)
            return 2
    try:
        conn = connect("admin")
    except IdentityMissing as e:
        print(f"Admin account missing: {e}", file=sys.stderr)
        return 2
    existing = {r[0] for r in run(conn, "SELECT USER_NAME FROM EXA_ALL_USERS").rows or []}
    for user, (var, privileges) in USERS.items():
        if user in existing:
            print(f"  {user}: already exists, left unchanged")
            continue
        for sql in (f'CREATE USER {user} IDENTIFIED BY "{os.environ[var]}"', f"GRANT {privileges} TO {user}"):
            o = run(conn, sql)
            if not o.ok:
                print(f"  FAILED: {o.sql}\n  [{o.error_code}] {o.error_text}", file=sys.stderr)
                return 1
        print(f"  {user}: created ({privileges})")
    conn.close()
    print("Done. Next: uv run python scripts/verify.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
