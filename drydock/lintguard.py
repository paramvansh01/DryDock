"""Runtime dialect check for generated SQL.

Every generated statement passes clean() before it runs. A Postgres-ism, or an opaque
statement with no recorded evidence in sql/DIALECT.md, raises instead of reaching the
database.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from .config import ROOT

_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import dialect_lint  # noqa: E402

DialectError = dialect_lint.DialectError


@lru_cache(maxsize=4)
def _rules(banned_mtime: float, dialect_mtime: float):
    return dialect_lint.load_banned(), dialect_lint.load_dialect()


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def clean(sql: str, origin: str = "runtime") -> str:
    banned, dialect = _rules(_mtime(dialect_lint.BANNED_TXT), _mtime(dialect_lint.DIALECT_MD))
    fails = [f for f in dialect_lint.lint_sql(sql, origin, banned=banned, dialect=dialect) if f.level == "FAIL"]
    if fails:
        msg = "\n".join(f"{f.gate}: {f.message}" for f in fails)
        raise DialectError(f"generated SQL failed the dialect firewall ({origin}):\n{msg}\n---\n{sql[:3000]}")
    return sql
