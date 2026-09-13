"""pyexasol, one connection per identity per process, serialised by a lock.

No ORM, no client-side parameter formatting: literals go through lit(),
identifiers through ident(), both of which refuse anything they cannot make
safe. (pyexasol's {name} formatter would collide with the braces in the JSON
we store, so it is not used.)
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

from .config import ROOT  # noqa: F401  (loads .env)

_IDENT = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

IDENTITIES = {
    "admin": ("EXA_ADMIN_USER", "EXA_ADMIN_PASSWORD"),
    "svc": ("DRYDOCK_SVC_USER", "DRYDOCK_SVC_PASSWORD"),
    "agent": ("DRYDOCK_AGENT_USER", "DRYDOCK_AGENT_PASSWORD"),
}


class DbError(RuntimeError):
    def __init__(self, sql: str, code: str | None, message: str):
        super().__init__(f"[{code}] {message}\n--- SQL ---\n{sql[:2000]}")
        self.sql, self.code, self.message = sql, code, message


def ident(name: str) -> str:
    """A validated, unquoted, upper-case identifier. Refuses anything else."""
    up = str(name).upper()
    if not _IDENT.match(up):
        raise ValueError(f"unsafe identifier: {name!r}")
    return up


def qname(schema: str, table: str) -> str:
    return f"{ident(schema)}.{ident(table)}"


def lit(v: Any) -> str:
    """SQL literal for Exasol. Strings single-quoted with '' escaping."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dt.datetime):
        return f"TIMESTAMP '{v.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]}'"
    if isinstance(v, dt.date):
        return f"DATE '{v.isoformat()}'"
    s = str(v)
    if "\x00" in s:
        raise ValueError("NUL byte in literal")
    return "'" + s.replace("'", "''") + "'"


def dsn() -> str:
    raw = os.environ.get("EXA_DSN", "").strip()
    if not raw:
        raise RuntimeError("EXA_DSN is not set in .env")
    if "/" in raw:
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


class Db:
    """One pyexasol connection, guarded by a re-entrant lock (pyexasol
    connections are not thread-safe; FastAPI runs sync work in a threadpool)."""

    def __init__(self, identity: str = "svc"):
        if identity not in IDENTITIES:
            raise ValueError(identity)
        self.identity = identity
        self._conn = None
        self._lock = threading.RLock()

    def _connect(self):
        import pyexasol

        user_var, pw_var = IDENTITIES[self.identity]
        user, pw = os.environ.get(user_var, ""), os.environ.get(pw_var, "")
        if not user or not pw:
            raise RuntimeError(f"{user_var}/{pw_var} not set in .env")
        last = None
        for attempt in range(3):
            try:
                return pyexasol.connect(dsn=dsn(), user=user, password=pw, autocommit=True,
                                        fetch_dict=False, compression=True,
                                        client_name=f"drydock-{self.identity}")
            except pyexasol.ExaConnectionError as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"cannot connect as {self.identity}: {last}")

    @property
    def conn(self):
        with self._lock:
            if self._conn is None or self._conn.is_closed:
                self._conn = self._connect()
            return self._conn

    def execute(self, sql: str):
        """EVERY statement on the product path passes the dialect firewall here, before
        it can reach Exasol — f-string, generated, agent-authored, all of it. There is no
        flag to turn this off. (Only scripts/verify.py and scripts/probe.py, which exist to
        run UNPROVEN SQL against PROBE_SCRATCH, use their own connection.)"""
        import pyexasol

        from .lintguard import clean

        clean(sql, f"db.{self.identity}")
        with self._lock:
            try:
                return self.conn.execute(sql)
            except pyexasol.ExaQueryError as e:
                raise DbError(sql, str(e.code), e.message) from e

    def rows(self, sql: str) -> list[tuple]:
        with self._lock:
            return [tuple(r) for r in self.execute(sql)]

    def dicts(self, sql: str) -> list[dict]:
        with self._lock:
            st = self.execute(sql)
            cols = st.column_names()
            return [dict(zip(cols, r)) for r in st]

    def scalar(self, sql: str) -> Any:
        r = self.rows(sql)
        return r[0][0] if r else None

    def run(self, sql: str) -> int:
        """DML/DDL; returns rows affected."""
        with self._lock:
            return int(self.execute(sql).rowcount() or 0)

    def timed(self, sql: str) -> tuple[int, float]:
        t0 = time.perf_counter()
        n = self.run(sql)
        return n, (time.perf_counter() - t0) * 1000

    @contextmanager
    def transaction(self) -> Iterator["Db"]:
        """Autocommit off for the block; COMMIT on success, ROLLBACK on error."""
        with self._lock:
            c = self.conn
            c.set_autocommit(False)
            try:
                yield self
                c.commit()
            except BaseException:
                try:
                    c.rollback()
                finally:
                    c.set_autocommit(True)
                raise
            c.set_autocommit(True)

    def import_rows(self, rows, schema: str, table: str, columns: list[str] | None = None) -> None:
        with self._lock:
            self.conn.import_from_iterable(rows, (ident(schema), ident(table)),
                                           import_params={"columns": columns} if columns else None)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


_pool: dict[str, Db] = {}
_pool_lock = threading.Lock()


def get(identity: str = "svc") -> Db:
    with _pool_lock:
        if identity not in _pool:
            _pool[identity] = Db(identity)
        return _pool[identity]
