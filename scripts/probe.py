"""Run SQL against the live instance and record the outcome.

    uv run python scripts/probe.py --sql "SELECT ..."
    uv run python scripts/probe.py --file sql/01_drydock_ddl.sql --ddl --into-scratch DRYDOCK
    uv run python scripts/probe.py --reset-scratch
    uv run python scripts/probe.py --as admin --no-autocommit --then rollback --sql "..."

Runs inside schema PROBE_SCRATCH, prints the result set or the full Exasol error with
its code, and appends statement and outcome to VERIFIED.md. It also prints the static
lint findings, but never refuses to run: probing a pattern is how it gets proven.

Default identity is svc (DRYDOCK_SVC); before that user exists, pass --as admin.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    SCRATCH, IdentityMissing, append_verified, connect, identity_user, now_iso,
    redact, run, split_sql,
)
from dialect_lint import lint_sql  # noqa: E402


def into_scratch(sql: str, schemas: list[str]) -> str:
    """Substitute qualified references to the given schemas with PROBE_SCRATCH,
    so DDL destined for sql/ can be proven without touching the real schema.
    Also rewrites CREATE SCHEMA [IF NOT EXISTS] <s> to a no-op comment."""
    for s in schemas:
        s = s.upper()
        sql = re.sub(rf"(?i)\bCREATE\s+SCHEMA\s+(IF\s+NOT\s+EXISTS\s+)?{s}\b",
                     f"-- (scratch) create schema {s} skipped\nSELECT 1", sql)
        sql = re.sub(rf"(?i)\b{s}\s*\.", f"{SCRATCH}.", sql)
    return sql


def ensure_scratch(conn) -> str | None:
    out = run(conn, f"OPEN SCHEMA {SCRATCH}")
    if out.ok:
        return None
    made = run(conn, f"CREATE SCHEMA {SCRATCH}")
    if not made.ok:
        return f"cannot create {SCRATCH}: [{made.error_code}] {made.error_text}"
    again = run(conn, f"OPEN SCHEMA {SCRATCH}")
    return None if again.ok else f"cannot open {SCRATCH}: {again.error_text}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--sql", help="one or more ';'-separated statements")
    src.add_argument("--file", help="a .sql file (supports --/ ... / script blocks)")
    ap.add_argument("--as", dest="identity", default="svc",
                    choices=["svc", "admin", "agent"])
    ap.add_argument("--ddl", action="store_true",
                    help="label: this DDL is intended to be kept in sql/")
    ap.add_argument("--into-scratch", default="",
                    help="comma list of schemas to rewrite to PROBE_SCRATCH")
    ap.add_argument("--no-scratch", action="store_true",
                    help="do not OPEN SCHEMA PROBE_SCRATCH first")
    ap.add_argument("--reset-scratch", action="store_true",
                    help=f"drop and recreate {SCRATCH}, then exit unless "
                         "--sql/--file is also given")
    ap.add_argument("--no-autocommit", action="store_true")
    ap.add_argument("--then", choices=["commit", "rollback"],
                    help="with --no-autocommit: finish with COMMIT or ROLLBACK")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue after a failing statement")
    ap.add_argument("--title", default="", help="label for the VERIFIED.md entry")
    ap.add_argument("--no-record", action="store_true",
                    help="do not append to VERIFIED.md (use sparingly)")
    ap.add_argument("--max-rows", type=int, default=50)
    a = ap.parse_args()

    if not (a.sql or a.file or a.reset_scratch):
        ap.error("give --sql, --file or --reset-scratch")

    try:
        conn = connect(a.identity, autocommit=not a.no_autocommit)
    except IdentityMissing as e:
        print(f"IDENTITY MISSING: {e}\nBefore DRYDOCK_SVC exists, "
              f"re-run with --as admin.", file=sys.stderr)
        return 2
    except Exception as e:  # connection failure: print it whole
        print(f"CONNECTION FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    who = f"{a.identity} ({identity_user(a.identity)})"
    records: list[str] = []

    if a.reset_scratch:
        for s in (f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE", f"CREATE SCHEMA {SCRATCH}"):
            o = run(conn, s)
            print(f"> {s}\n{o.render()}")
            records.append(_fmt(s, o))

    if a.sql or a.file:
        text = a.sql if a.sql else Path(a.file).read_text()
        if a.into_scratch:
            text = into_scratch(text, [s.strip() for s in a.into_scratch.split(",") if s.strip()])
        stmts = split_sql(text)
        if not a.no_scratch:
            err = ensure_scratch(conn)
            if err:
                print(f"WARNING: {err} — running without a scratch schema", file=sys.stderr)

        failed = False
        for i, s in enumerate(stmts, 1):
            findings = lint_sql(s, origin="probe")
            print(f"\n=== [{i}/{len(stmts)}] as {who} ===\n{redact(s)}\n---")
            for f in findings:
                print(f"  lint {f.level}: {f.gate} {f.message}")
            o = run(conn, s)
            print(o.render(a.max_rows))
            records.append(_fmt(s, o, findings))
            if not o.ok:
                failed = True
                if not a.keep_going:
                    break
        if a.no_autocommit and a.then:
            o = run(conn, a.then.upper())
            print(f"\n> {a.then.upper()}\n{o.render()}")
            records.append(_fmt(a.then.upper(), o))
    else:
        failed = False

    conn.close()

    if not a.no_record and records:
        title = a.title or ("DDL probe" if a.ddl else "probe")
        mode = "autocommit" if not a.no_autocommit else f"no-autocommit, then {a.then or 'nothing'}"
        src_label = f"file {a.file}" if a.file else "inline"
        header = f"### {now_iso()} · {title} · as {who} · {mode} · {src_label}"
        if a.into_scratch:
            header += f" · into-scratch {a.into_scratch}"
        append_verified("Probes", header + "\n" + "\n".join(records))
        print("\nrecorded -> VERIFIED.md (## Probes)")
    return 1 if failed else 0


def _fmt(sql: str, o, findings=()) -> str:
    lint = "".join(f"\nLINT:   {f.level} {f.gate} {f.message}" for f in findings)
    result = o.render(20).replace("\n", "\n        ")
    return f"```sql\n{redact(sql)}\n```\nRESULT: {result}{lint}\n"


if __name__ == "__main__":
    raise SystemExit(main())
