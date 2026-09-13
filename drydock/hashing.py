"""Row hashing and table fingerprints, generated from the catalogue.

Canonical form per column, unambiguous without separator escaping:
    COALESCE(TO_CHAR(LENGTH(v)) || ':' || v, '~')
Length-prefixing makes 'a|b' + 'c' distinct from 'a' + 'b|c'. On Exasol, NULL is
ignored by '||' and the empty string is NULL, so both render ':', which cannot
collide with a non-NULL value (those always start with a digit).

Fingerprint = (N, S1, S2): row count and two exact sums of 60-bit slices of each row
hash. Order-independent and algebraic: the expected post-merge fingerprint can be
computed from the pre-merge fingerprint and the diff's hashes, independently of the
staged table.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import SETTINGS, verified_status
from .db import Db, ident, lit

HEX15 = "XXXXXXXXXXXXXXX"


@dataclass(frozen=True)
class Column:
    name: str
    type: str       # EXA_ALL_COLUMNS.COLUMN_TYPE, e.g. VARCHAR(200) UTF8, DECIMAL(18,0), DATE


@dataclass(frozen=True)
class Fingerprint:
    n: int
    s1: int
    s2: int

    @property
    def hex(self) -> str:
        return hashlib.sha256(f"{self.n}:{self.s1}:{self.s2}".encode()).hexdigest()

    def __add__(self, o: "Fingerprint") -> "Fingerprint":
        return Fingerprint(self.n + o.n, self.s1 + o.s1, self.s2 + o.s2)

    def __sub__(self, o: "Fingerprint") -> "Fingerprint":
        return Fingerprint(self.n - o.n, self.s1 - o.s1, self.s2 - o.s2)

    @staticmethod
    def of_hashes(hashes) -> "Fingerprint":
        n = s1 = s2 = 0
        for h in hashes:
            n += 1
            s1 += int(h[0:15], 16)
            s2 += int(h[15:30], 16)
        return Fingerprint(n, s1, s2)


ZERO = Fingerprint(0, 0, 0)


def columns(db: Db, schema: str, table: str) -> list[Column]:
    rows = db.rows(
        "SELECT COLUMN_NAME, COLUMN_TYPE FROM EXA_ALL_COLUMNS "
        f"WHERE COLUMN_SCHEMA = {lit(ident(schema))} AND COLUMN_TABLE = {lit(ident(table))} "
        "ORDER BY COLUMN_ORDINAL_POSITION")
    if not rows:
        raise LookupError(f"no columns for {schema}.{table} (missing, or not visible to {db.identity})")
    return [Column(r[0], r[1]) for r in rows]


def signature(cols: list[Column]) -> str:
    return ",".join(f"{c.name}:{c.type}" for c in cols)


def as_text(col: str, coltype: str, alias: str = "") -> str:
    """Deterministic text rendering of one column, by catalogue type."""
    ref = f"{alias}.{ident(col)}" if alias else ident(col)
    t = coltype.upper()
    if t.startswith(("VARCHAR", "CHAR")):
        return ref
    if t.startswith("DATE"):
        return f"TO_CHAR({ref}, 'YYYY-MM-DD')"
    if t.startswith("TIMESTAMP"):
        return f"TO_CHAR({ref}, 'YYYY-MM-DD HH24:MI:SS.FF3')"
    if t.startswith("BOOLEAN"):
        return f"CASE WHEN {ref} THEN 'T' WHEN NOT {ref} THEN 'F' END"
    if t.startswith(("DECIMAL", "DOUBLE")):
        return f"TO_CHAR({ref})"
    return f"CAST({ref} AS VARCHAR(2000000))"


def key_text(cols: list[Column], key: str, alias: str = "") -> str:
    """The diff key rendered as text (DIFF_ROWS.KEY_VALUE is VARCHAR)."""
    for c in cols:
        if c.name == ident(key):
            return as_text(c.name, c.type, alias)
    raise LookupError(f"key column {key} not in table")


def canon(col: str, coltype: str, alias: str = "") -> str:
    v = as_text(col, coltype, alias)
    return f"COALESCE(TO_CHAR(LENGTH({v})) || ':' || {v}, '~')"


def row_hash_expr(cols: list[Column], alias: str = "") -> str:
    parts = " || '|' || ".join(canon(c.name, c.type, alias) for c in cols)
    return f"HASH_SHA256({parts})"


def fingerprint_mode() -> str:
    m = SETTINGS.fingerprint_mode
    if m in ("sql", "client"):
        return m
    return "sql" if verified_status("L3") == "PASS" else "client"


def fingerprint_of_query(db: Db, hash_query: str, mode: str | None = None) -> Fingerprint:
    """hash_query must select exactly one column named H (64 hex chars)."""
    mode = mode or fingerprint_mode()
    if mode == "sql":
        n, s1, s2 = db.rows(
            f"SELECT COUNT(*), "
            f"COALESCE(SUM(TO_NUMBER(UPPER(SUBSTR(H, 1, 15)), '{HEX15}')), 0), "
            f"COALESCE(SUM(TO_NUMBER(UPPER(SUBSTR(H, 16, 15)), '{HEX15}')), 0) "
            f"FROM ({hash_query})")[0]
        return Fingerprint(int(n), int(s1), int(s2))
    with db._lock:
        st = db.execute(hash_query)
        return Fingerprint.of_hashes(r[0] for r in st)


def table_fingerprint(db: Db, schema: str, table: str, cols: list[Column] | None = None) -> Fingerprint:
    cols = cols or columns(db, schema, table)
    return fingerprint_of_query(db, f"SELECT {row_hash_expr(cols)} AS H FROM {ident(schema)}.{ident(table)}")
