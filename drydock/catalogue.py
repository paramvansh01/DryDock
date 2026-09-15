"""Schema-aware check that every table and column a statement names exists.

The catalogue is derived from the project's own DDL (sql/*.sql, bench/sql/*.sql),
the reset CTAS, the projections of the generated entity-resolution SQL, and
sql/system_catalogue.json for the Exasol system views that are read.

check_sql() fails a statement that names an unknown table, or a column the table
does not have, including SET/INSERT target columns. Names being right is necessary,
not sufficient: scripts/probe_all.py runs every statement against a live instance.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .config import ROOT

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

Catalogue = dict[str, list[str]]           # "SCHEMA.TABLE" -> [COLUMN, ...] (upper case, ordered)

# Schemas whose tables are created at runtime under a naming pattern, resolved to a catalogued table.
BRANCH_SCHEMA = re.compile(r"^BR_[0-9A-Z]+$")
ER_WORK_TABLE = re.compile(r"^(MATCHES|NEW|DEDUP|A_NORM|B_NORM)_[A-Z0-9_]+$")


def _split(text: str) -> list[str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from _common import split_sql
    return split_sql(text)


def _columns_of_create(e: exp.Create) -> list[str]:
    schema = e.this
    if isinstance(schema, exp.Schema):
        return [c.name.upper() for c in schema.expressions if isinstance(c, exp.ColumnDef)]
    return []


def _projection(select: exp.Expression, cat: Catalogue) -> list[str] | None:
    """Output column names of a SELECT, expanding SELECT * from a catalogued table."""
    if not isinstance(select, exp.Query):
        return None
    if isinstance(select, exp.Union):
        return _projection(select.this, cat)
    out: list[str] = []
    for p in select.selects:
        if isinstance(p, exp.Star):
            src = select.find(exp.Table)
            key = _key(src) if src is not None else None
            if key in cat:
                out.extend(cat[key])
            else:
                return None
        else:
            name = p.alias_or_name
            if not name:
                return None
            out.append(name.upper())
    return out


def _key(t: exp.Table) -> str:
    return f"{t.db.upper()}.{t.name.upper()}" if t.db else t.name.upper()


def ddl_statements() -> list[str]:
    files = sorted((ROOT / "sql").glob("*.sql")) + sorted((ROOT / "bench" / "sql").glob("*.sql"))
    out = []
    for f in files:
        out.extend(_split(f.read_text()))
    # Runtime CTAS that defines GOLDEN.CUSTOMERS (drydock/merge.py, reseed); read from source, not retyped.
    merge_src = (ROOT / "drydock" / "merge.py").read_text()
    out.extend(re.findall(r'"(CREATE TABLE GOLDEN\.CUSTOMERS AS SELECT \* FROM BENCH\.GOLDEN_CLEAN)"', merge_src))
    return out


def learn(statements: list[str], cat: Catalogue) -> Catalogue:
    """Add every table/view the statements define. Repeats until fixpoint (views over CTAS)."""
    pending = list(statements)
    for _ in range(4):
        left = []
        for sql in pending:
            try:
                e = sqlglot.parse_one(sql, read="exasol")
            except Exception:
                continue
            if not isinstance(e, exp.Create) or (e.args.get("kind") or "").upper() not in ("TABLE", "VIEW"):
                continue
            target = e.this.this if isinstance(e.this, exp.Schema) else e.this
            if not isinstance(target, exp.Table):
                continue
            cols = _columns_of_create(e)
            if not cols and e.expression is not None:
                cols = _projection(e.expression, cat) or []
                if not cols:
                    left.append(sql)
                    continue
            cat[_key(target)] = cols
        pending = left
        if not pending:
            break
    return cat


def system_catalogue() -> Catalogue:
    raw = json.loads((ROOT / "sql" / "system_catalogue.json").read_text())
    return {name: cols for name, cols in raw["SYS"].items()}


def build(extra_statements: list[str] | None = None) -> Catalogue:
    cat: Catalogue = {}
    learn(ddl_statements(), cat)
    for name, cols in system_catalogue().items():
        cat[name] = cols          # unqualified system views (EXA_ALL_COLUMNS ...)
        cat[f"SYS.{name}"] = cols
    if extra_statements:
        learn(extra_statements, cat)
    return cat


@dataclass
class Problem:
    kind: str
    detail: str


@dataclass
class Result:
    sql: str
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _resolve(key: str, cat: Catalogue) -> str | None:
    if key in cat:
        return key
    schema, _, name = key.partition(".")
    if BRANCH_SCHEMA.match(schema) and f"GOLDEN.{name}" in cat:
        return f"GOLDEN.{name}"
    if schema == "ER_WORK" and ER_WORK_TABLE.match(name):
        stem = name.split("_", 1)[0] if not name.startswith(("A_NORM", "B_NORM")) else name[:6]
        for k in cat:
            if k.startswith(f"ER_WORK.{stem}_"):
                return k
    return None


def check_sql(sql: str, cat: Catalogue) -> Result:
    res = Result(sql)
    try:
        e = sqlglot.parse_one(sql, read="exasol")
    except Exception as err:
        res.problems.append(Problem("parse", str(err)[:200]))
        return res
    if isinstance(e, exp.Command):
        return res                      # opaque (RENAME ...): Gate 3 / DIALECT.md territory
    if isinstance(e, exp.Grant) and re.search(r"\bON\s+SCHEMA\b", sql, re.IGNORECASE):
        known_schemas = {k.split(".", 1)[0] for k in cat if "." in k}
        target = e.find(exp.Table)
        name = (target.name if target is not None else "").upper()
        if name not in known_schemas:
            res.problems.append(Problem("unknown_schema", name))
        return res
    ctes = {c.alias_or_name.upper() for c in e.find_all(exp.CTE)}
    derived = {s.alias_or_name.upper() for s in e.find_all(exp.Subquery) if s.alias_or_name}
    aliases: dict[str, str] = {}
    creating = None
    if isinstance(e, exp.Create):
        t = e.this.this if isinstance(e.this, exp.Schema) else e.this
        creating = _key(t) if isinstance(t, exp.Table) else None
    for t in e.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue
        key = _key(t)
        if not t.db and key in ctes | derived:
            continue
        if key == creating:
            continue
        resolved = _resolve(key, cat)
        if resolved is None:
            if not t.db and BRANCH_SCHEMA.match(key):
                continue
            res.problems.append(Problem("unknown_table", key))
            continue
        aliases[(t.alias_or_name or t.name).upper()] = resolved
        aliases[key] = resolved
        aliases[t.name.upper()] = resolved
    known_tables = set(aliases.values())
    # Only real aliases (expr AS name) introduce names; a bare column in a projection must exist.
    projected = {p.alias.upper() for s in e.find_all(exp.Select) for p in s.selects
                 if isinstance(p, exp.Alias) and p.alias}

    def has(table: str, col: str) -> bool:
        return col in cat.get(table, [])

    for c in e.find_all(exp.Column):
        col = c.name.upper()
        if not col or col == "*":
            continue
        if c.table:
            q = c.table.upper()
            full = f"{c.args['db'].name.upper()}.{q}" if c.args.get("db") else None
            target = _resolve(full, cat) if full else aliases.get(q)
            if target is None:
                if q in ctes or q in derived:
                    continue
                res.problems.append(Problem("unknown_alias", f"{q}.{col}"))
            elif not has(target, col):
                res.problems.append(Problem("unknown_column", f"{target}.{col}"))
        else:
            if col in projected or col in ctes or col in derived:
                continue
            if known_tables and not any(has(t, col) for t in known_tables):
                if ctes or derived:
                    continue            # may come from a CTE / derived table we do not model
                res.problems.append(Problem("unknown_column", f"{col} (in {sorted(known_tables)})"))
    return res
