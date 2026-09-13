"""Shared helpers for the scripts: environment, connections, SQL splitting and the
append-only VERIFIED.md log.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
VERIFIED_MD = ROOT / "VERIFIED.md"
VERIFIED_JSON = ROOT / "verified.json"
DIALECT_MD = ROOT / "sql" / "DIALECT.md"
SCRATCH = "PROBE_SCRATCH"

load_dotenv(ROOT / ".env")

IDENTITIES: dict[str, tuple[str, str]] = {
    "admin": ("EXA_ADMIN_USER", "EXA_ADMIN_PASSWORD"),
    "svc": ("DRYDOCK_SVC_USER", "DRYDOCK_SVC_PASSWORD"),
    "agent": ("DRYDOCK_AGENT_USER", "DRYDOCK_AGENT_PASSWORD"),
}


class IdentityMissing(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def dsn() -> str:
    raw = os.environ.get("EXA_DSN", "").strip()
    if not raw:
        raise IdentityMissing("EXA_DSN is not set in .env")
    if "/" in raw:  # fingerprint or nocertcheck already embedded
        return raw
    host, _, port = raw.rpartition(":")
    if not host:
        host, port = raw, "8563"
    fp = os.environ.get("EXA_CERT_FINGERPRINT", "").strip()
    if fp:
        return f"{host}/{fp}:{port}"
    if os.environ.get("EXA_TLS_NOCERTCHECK", "0").strip() in ("1", "true", "TRUE"):
        return f"{host}/nocertcheck:{port}"
    return f"{host}:{port}"


def identity_user(identity: str) -> str:
    user_var, _ = IDENTITIES[identity]
    return os.environ.get(user_var, "").strip()


def identity_configured(identity: str) -> bool:
    user_var, pw_var = IDENTITIES[identity]
    return bool(os.environ.get(user_var, "").strip() and os.environ.get(pw_var, ""))


def connect(identity: str, *, autocommit: bool = True, schema: str = "",
            query_timeout: int = 0):
    """One connection per identity. Raises IdentityMissing if not in .env."""
    import pyexasol

    if identity not in IDENTITIES:
        raise ValueError(f"unknown identity {identity!r}")
    user_var, pw_var = IDENTITIES[identity]
    user, pw = os.environ.get(user_var, "").strip(), os.environ.get(pw_var, "")
    if not user or not pw:
        raise IdentityMissing(f"{user_var}/{pw_var} not set in .env")
    return pyexasol.connect(
        dsn=dsn(), user=user, password=pw, schema=schema, autocommit=autocommit,
        query_timeout=query_timeout, fetch_dict=False, compression=True,
        client_name="drydock-phase0",
    )


@dataclass
class Outcome:
    sql: str
    ok: bool
    ms: float
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    rowcount: int | None = None
    error_code: str | None = None
    error_text: str | None = None

    def render(self, max_rows: int = 50) -> str:
        if not self.ok:
            return f"ERROR [{self.error_code}] {self.error_text}  ({self.ms:.0f}ms)"
        if not self.columns:
            return f"OK, {self.rowcount} rows affected, {self.ms:.0f}ms"
        lines = [" | ".join(self.columns)]
        for r in self.rows[:max_rows]:
            lines.append(" | ".join("NULL" if v is None else str(v) for v in r))
        more = len(self.rows) - max_rows
        if more > 0:
            lines.append(f"... {more} more rows")
        lines.append(f"({len(self.rows)} rows, {self.ms:.0f}ms)")
        return "\n".join(lines)


_PASSWORD = re.compile(r"""(?i)(IDENTIFIED\s+BY\s+)("[^"]*"|'[^']*')""")


def redact(text: str | None) -> str | None:
    """Mask passwords so nothing secret lands in VERIFIED.md or a terminal paste."""
    return None if text is None else _PASSWORD.sub(r'\1"***"', text)


def run(conn, sql: str, *, fetch_limit: int = 10_000) -> Outcome:
    """Execute one statement; never raises on SQL errors, returns an Outcome.
    The Outcome carries the REDACTED statement; the real one is only sent to Exasol."""
    o = _run(conn, sql, fetch_limit)
    o.sql, o.error_text = redact(o.sql) or "", redact(o.error_text)
    return o


def _run(conn, sql: str, fetch_limit: int) -> Outcome:
    import pyexasol

    t0 = time.perf_counter()
    try:
        st = conn.execute(sql)
        ms = (time.perf_counter() - t0) * 1000
        if st.result_type == "resultSet":
            cols = list(st.column_names())
            rows = []
            for i, r in enumerate(st):
                if i >= fetch_limit:
                    break
                rows.append(tuple(r))
            return Outcome(sql, True, ms, cols, rows, rowcount=st.rowcount())
        return Outcome(sql, True, ms, rowcount=st.rowcount())
    except pyexasol.ExaQueryError as e:
        ms = (time.perf_counter() - t0) * 1000
        return Outcome(sql, False, ms, error_code=str(e.code), error_text=e.message)
    except pyexasol.ExaError as e:
        ms = (time.perf_counter() - t0) * 1000
        return Outcome(sql, False, ms, error_code=type(e).__name__, error_text=str(e))


# --------------------------------------------------------------------------
# Splitting SQL files. Exasol's client convention: statements end with ';'
# outside quotes; a script body (CREATE ... SCRIPT) is wrapped as
#   --/
#   CREATE ... AS
#   <body>
#   /
# and the terminating '/' line is a client-side delimiter, not SQL.
# --------------------------------------------------------------------------

_SCRIPT_BLOCK = re.compile(r"^--/\s*\n(.*?)\n/\s*$", re.S | re.M)


def split_sql(text: str) -> list[str]:
    out: list[str] = []
    pos = 0
    for m in _SCRIPT_BLOCK.finditer(text):
        out.extend(_split_plain(text[pos:m.start()]))
        out.append(m.group(1).strip())
        pos = m.end()
    out.extend(_split_plain(text[pos:]))
    return [s for s in out if s]


def _split_plain(text: str) -> list[str]:
    stmts, buf, i, n = [], [], 0, len(text)
    quote: str | None = None
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == quote:
                if i + 1 < n and text[i + 1] == quote:  # doubled quote escape
                    buf.append(text[i + 1])
                    i += 1
                else:
                    quote = None
        elif c in ("'", '"'):
            quote = c
            buf.append(c)
        elif c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif c == ";":
            stmts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    stmts.append("".join(buf).strip())
    return [s for s in stmts if s]


def strip_literals_and_comments(sql: str) -> str:
    """Replace string-literal contents with '' and drop comments, so pattern
    checks do not fire on data (e.g. '::' inside a literal)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            out.append("''")
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# VERIFIED.md is append-only. Never rewrite it; only add.
# --------------------------------------------------------------------------

def append_verified(section: str, body: str) -> None:
    VERIFIED_MD.parent.mkdir(parents=True, exist_ok=True)
    existing = VERIFIED_MD.read_text() if VERIFIED_MD.exists() else ""
    with VERIFIED_MD.open("a") as f:
        if f"\n## {section}\n" not in "\n" + existing:
            f.write(f"\n## {section}\n")
        f.write("\n" + body.rstrip() + "\n")
