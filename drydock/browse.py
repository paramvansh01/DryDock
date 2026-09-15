"""Read-only table browser for the UI's Database view.

SELECT only. Nothing here writes, and nothing here takes SQL from the browser: the caller
names a schema, a table, a page and (optionally) a sort column and a text filter, and this
module composes the statement itself from validated identifiers. Every statement it composes
still passes the dialect firewall in Db.execute like all other SQL.

The composed statement is returned with the rows so the view can show exactly what was run.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from decimal import Decimal
from typing import Any

from .db import Db, ident, lit, qname

# Browsable schemas. BENCH is the answer key: readable by a person here (as the scorer already
# is), never by the agent, which holds no grant on it.
SCHEMAS = ("SOURCE_A", "SOURCE_B", "UPLOADS", "GOLDEN", "GOLDEN_V", "DRYDOCK", "ER_WORK", "BENCH")
BRANCH_SCHEMA = re.compile(r"^BR_[0-9A-Z]+$")

MAX_LIMIT = 500
DEFAULT_LIMIT = 50
CHAR_TYPE = re.compile(r"^(VARCHAR|CHAR)\b")
LIKE_ESCAPE = "!"                       # the ESCAPE character sql/DIALECT.md proves on this instance


def _schema(name: str) -> str:
    s = ident(name)
    if s not in SCHEMAS and not BRANCH_SCHEMA.match(s):
        raise ValueError(f"schema not browsable: {s}")
    return s


def columns(db: Db, schema: str, table: str) -> list[list[str]]:
    """Columns as Exasol reports them, in ordinal order."""
    s, t = _schema(schema), ident(table)
    return [[c, typ] for c, typ in db.rows(
        f"SELECT COLUMN_NAME, COLUMN_TYPE FROM EXA_ALL_COLUMNS WHERE COLUMN_SCHEMA = {lit(s)} "
        f"AND COLUMN_TABLE = {lit(t)} ORDER BY COLUMN_ORDINAL_POSITION")]


def _like(needle: str) -> str:
    """A LIKE pattern that matches the needle literally: % and _ typed into the filter box are
    the characters they look like, not wildcards."""
    out = needle.upper()
    for c in (LIKE_ESCAPE, "%", "_"):
        out = out.replace(c, LIKE_ESCAPE + c)
    return f"%{out}%"


def _cell(v: Any) -> Any:
    """A JSON-safe value that still says what the database holds."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float):
        # NaN/Infinity are not JSON: json.dumps would emit them and the browser could not parse
        # the page at all. Say what the value is instead of failing the whole grid.
        return v if math.isfinite(v) else str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def rows(db: Db, schema: str, table: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0,
         order: str | None = None, direction: str = "ASC", q: str | None = None) -> dict:
    """One page of a table, with the exact statement that produced it.

    Paging without an ORDER BY is not stable, so when the caller names no sort column the
    first column is used and the response says so (order_implicit).
    """
    t0 = time.perf_counter()
    s, t = _schema(schema), ident(table)
    cols = columns(db, s, t)
    if not cols:
        raise ValueError(f"no such table: {s}.{t}")
    names = [c for c, _ in cols]

    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    implicit = order is None
    sort = names[0] if implicit else ident(order)
    if sort not in names:
        raise ValueError(f"no such column: {s}.{t}.{sort}")
    dirn = "DESC" if str(direction).upper() == "DESC" else "ASC"

    # The filter is a plain LIKE over the character columns — no cast, so a number column is
    # left out rather than guessed at. The response names the columns it searched.
    needle = (q or "").strip()
    searched = [c for c, typ in cols if CHAR_TYPE.match(typ)] if needle else []
    where = ""
    if searched:
        pat = f"{lit(_like(needle))} ESCAPE {lit(LIKE_ESCAPE)}"
        where = " WHERE (" + " OR ".join(f"UPPER({ident(c)}) LIKE {pat}" for c in searched) + ")"

    select = ", ".join(ident(c) for c in names)
    fq = qname(s, t)
    sql = (f"SELECT {select} FROM {fq}{where} "  # sql-fragment: composed above, linted in full at Db.execute
           f"ORDER BY {ident(sort)} {dirn} LIMIT {limit} OFFSET {offset}")
    page = [[_cell(v) for v in r] for r in db.rows(sql)]
    total = int(db.scalar(f"SELECT COUNT(*) FROM {fq}{where}") or 0)  # sql-fragment: same WHERE as the page
    db_time = str(db.scalar("SELECT CURRENT_TIMESTAMP"))

    return {"schema": s, "table": t, "columns": cols, "rows": page, "total": total,
            "limit": limit, "offset": offset, "order": sort, "order_implicit": implicit,
            "dir": dirn, "q": needle or None, "searched": searched, "sql": sql,
            "db_time": db_time, "ms": round((time.perf_counter() - t0) * 1000, 1)}
