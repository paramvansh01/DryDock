"""SQL retargeting: table-reference substitution and nothing else.

The agent's statement is parsed with sqlglot (read="exasol") only to find table
references. The output is the agent's original text with schema qualifiers spliced in
place. It is never re-rendered through sqlglot, whose exasol writer silently rewrites
SQL (IDENTITY -> AUTO_INCREMENT, SUBSTR -> SUBSTRING). What the agent wrote is what runs,
except for where it points.

Rules:
  GOLDEN.T / GOLDEN_V.T  WRITE -> materialise T, point at BR_x.T
                         READ  -> BR_x.T if materialised in this branch, else GOLDEN.T
  BR_<own>.T             left alone (tables the agent created in its branch)
  unqualified T          own-branch table if the branch created it, a CTE name, else BLOCK
  SOURCE_A / SOURCE_B / ER_WORK  READ only
  DRYDOCK / BENCH / SYS / EXA_* / other BR_* / anything else  BLOCK
  reserved names (__ARCH_, __NEW_, __UNDONE_) anywhere in GOLDEN  BLOCK
  DDL on GOLDEN, DROP SCHEMA, GRANT/REVOKE, users, sessions, transaction control,
  unparseable statements  BLOCK (fail closed)

A pure function of (sql, branch state), with no database access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

GOVERNED = {"GOLDEN"}                     # schemas whose tables are branchable
GOVERNED_ALIASES = {"GOLDEN_V": "GOLDEN"}  # agent-facing read views of GOVERNED
READ_ONLY = {"SOURCE_A", "SOURCE_B", "ER_WORK"}
FORBIDDEN = {"DRYDOCK", "BENCH", "SYS", "PROBE_SCRATCH"}
RESERVED_MARKERS = ("__ARCH_", "__NEW_", "__UNDONE_")


class Blocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass
class BranchState:
    branch_id: str                       # also the schema name, e.g. BR_1a2b3c4d
    materialised: set[str] = field(default_factory=set)   # GOLDEN table names copied in
    created: set[str] = field(default_factory=set)        # tables the agent created in BR


@dataclass
class Retargeted:
    sql: str
    original_sql: str
    tables_read: list[str]
    tables_written: list[str]
    to_materialise: list[str]            # GOLDEN tables to copy BEFORE executing
    creates: list[str]                   # own-branch tables this statement creates
    kind: str


_BLOCKED_TYPES: dict[type, tuple[str, str]] = {
    exp.Grant: ("PRIVILEGE", "GRANT is not allowed in a branch"),  # not-sql
    exp.Command: ("UNPARSED", "statement not understood by the parser; refusing (fail closed)"),
    exp.Use: ("SESSION", "OPEN SCHEMA / USE is not allowed; qualify table names instead"),  # not-sql
    exp.Commit: ("TRANSACTION", "transaction control is managed by Drydock"),
    exp.Rollback: ("TRANSACTION", "transaction control is managed by Drydock"),
    exp.Transaction: ("TRANSACTION", "transaction control is managed by Drydock"),
    exp.Set: ("SESSION", "SET is not allowed in a branch"),
}
for _name in ("Revoke", "AlterSession"):
    if hasattr(exp, _name):
        _BLOCKED_TYPES[getattr(exp, _name)] = ("PRIVILEGE", f"{_name.upper()} is not allowed")


def _up(ident: exp.Expression | None) -> str:
    if ident is None:
        return ""
    name = ident.name if hasattr(ident, "name") else str(ident)
    return name if (isinstance(ident, exp.Identifier) and ident.quoted) else name.upper()


def _span(ident: exp.Expression) -> tuple[int, int] | None:
    m = getattr(ident, "meta", None) or {}
    if "start" in m and "end" in m:
        return int(m["start"]), int(m["end"])
    return None


def _cte_names(root: exp.Expression) -> set[str]:
    return {c.alias_or_name.upper() for c in root.find_all(exp.CTE)}


def _write_targets(stmt: exp.Expression) -> list[exp.Table]:
    """Tables in WRITE position, by AST position."""
    out: list[exp.Table] = []

    def table_of(node):
        if isinstance(node, exp.Table):
            return node
        if isinstance(node, exp.Schema) and isinstance(node.this, exp.Table):
            return node.this
        return None

    if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Alter, exp.Drop)):
        t = table_of(stmt.this)
        if t is not None:
            out.append(t)
    if isinstance(stmt, exp.TruncateTable):
        out.extend(t for t in stmt.expressions if isinstance(t, exp.Table))
    if isinstance(stmt, exp.Drop):  # sqlglot keeps DROP targets under `tables`
        out.extend(t for t in (stmt.args.get("tables") or []) if isinstance(t, exp.Table))
    return out


# Fail closed: only these statement shapes are understood. Anything else blocks.
_ALLOWED_TYPES = (exp.Query, exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create,
                  exp.Drop, exp.TruncateTable, exp.Alter)


def retarget(sql: str, state: BranchState) -> Retargeted:
    """Retarget ONE statement. Raises Blocked. Multi-statement input is refused:
    run_in_branch sends one statement per call so every op is logged separately."""
    try:
        parsed = sqlglot.parse(sql, read="exasol")
    except Exception as e:  # sqlglot raises several error classes
        raise Blocked("PARSE_ERROR", f"{type(e).__name__}: {e}") from e
    stmts = [p for p in parsed if p is not None]
    if len(stmts) != 1:
        raise Blocked("MULTI_STATEMENT", f"send exactly one statement per call (got {len(stmts)})")
    stmt = stmts[0]
    for t, (code, msg) in _BLOCKED_TYPES.items():
        if isinstance(stmt, t):
            raise Blocked(code, msg)
    if not isinstance(stmt, _ALLOWED_TYPES):
        raise Blocked("UNSUPPORTED_STATEMENT", f"{type(stmt).__name__} statements are not allowed in a branch")

    own = state.branch_id.upper()
    kind = type(stmt).__name__.upper()

    if isinstance(stmt, exp.Create):
        ckind = (stmt.args.get("kind") or "").upper()
        if ckind not in ("TABLE", "VIEW"):
            raise Blocked("DDL", f"CREATE {ckind or '?'} is not allowed in a branch")  # not-sql
    if isinstance(stmt, exp.Drop):
        dkind = (stmt.args.get("kind") or "").upper()
        if dkind not in ("TABLE", "VIEW"):
            raise Blocked("DDL", f"DROP {dkind or '?'} is not allowed in a branch")  # not-sql

    ctes = _cte_names(stmt)
    writes = {id(t) for t in _write_targets(stmt)}
    edits: list[tuple[int, int, str]] = []   # (start, end_inclusive, replacement)
    reads_out: list[str] = []
    writes_out: list[str] = []
    to_mat: list[str] = []
    creates: list[str] = []
    resolved: dict[tuple[str, str], str] = {}  # (schema, table) -> new schema, for column refs

    def edit_schema(db_ident: exp.Expression, new_schema: str) -> None:
        sp = _span(db_ident)
        if sp is None:
            raise Blocked("NO_POSITION", "parser gave no source position for a schema reference")
        edits.append((sp[0], sp[1], new_schema))

    def insert_prefix(name_ident: exp.Expression, new_schema: str) -> None:
        sp = _span(name_ident)
        if sp is None:
            raise Blocked("NO_POSITION", "parser gave no source position for a table reference")
        edits.append((sp[0], sp[0] - 1, f"{new_schema}."))

    for t in stmt.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise Blocked("DYNAMIC_TABLE", "table functions / dynamic table references are not allowed")
        name = _up(t.this)
        schema = _up(t.args.get("db"))
        if t.args.get("catalog") is not None:
            raise Blocked("UNKNOWN_SCHEMA", f"three-part name {t.sql(dialect='exasol')} not allowed")
        is_write = id(t) in writes
        if name.startswith("EXA_") or schema.startswith("EXA_"):
            raise Blocked("FORBIDDEN_SCHEMA", f"system object {name} is not reachable from a branch")

        if not schema:
            if name in ctes and not is_write:
                continue
            if name in state.created or (isinstance(stmt, exp.Create) and is_write):
                if isinstance(stmt, exp.Create) and is_write:
                    creates.append(name)
                insert_prefix(t.this, own)
                (writes_out if is_write else reads_out).append(f"{own}.{name}")
                continue
            raise Blocked("UNQUALIFIED_REFERENCE",
                          f"{name} is unqualified; write GOLDEN.{name}, SOURCE_A.{name}, etc.")

        if schema in GOVERNED_ALIASES or schema in GOVERNED:
            base = GOVERNED_ALIASES.get(schema, schema)
            if any(m in name for m in RESERVED_MARKERS):
                raise Blocked("RESERVED_NAME", f"{base}.{name} is a reserved merge/archive table")
            if isinstance(stmt, (exp.Create, exp.Drop, exp.Alter)) and is_write:
                raise Blocked("DDL_ON_PRODUCTION", f"{kind} on {base}.{name} is not allowed")
            if is_write:
                if name not in state.materialised and name not in to_mat:
                    to_mat.append(name)
                edit_schema(t.args["db"], own)
                resolved[(schema, name)] = own
                writes_out.append(f"{base}.{name}")
            else:
                target = own if (name in state.materialised or name in to_mat) else base
                if target != schema:
                    edit_schema(t.args["db"], target)
                resolved[(schema, name)] = target
                reads_out.append(f"{base}.{name}")
            continue

        if schema == own:
            if isinstance(stmt, exp.Create) and is_write:
                creates.append(name)
            (writes_out if is_write else reads_out).append(f"{own}.{name}")
            continue
        if schema in READ_ONLY:
            if is_write:
                raise Blocked("WRITE_TO_READONLY", f"{schema} is read-only for every identity")
            reads_out.append(f"{schema}.{name}")
            continue
        if schema in FORBIDDEN:
            raise Blocked("FORBIDDEN_SCHEMA", f"{schema} is not reachable from a branch")
        if schema.startswith("BR_"):
            raise Blocked("OTHER_BRANCH", f"{schema} is another branch")
        raise Blocked("UNKNOWN_SCHEMA", f"schema {schema} is not reachable from a branch")

    # Fully-qualified column references (GOLDEN.CUSTOMERS.COL) follow their table.
    for c in stmt.find_all(exp.Column):
        db = c.args.get("db")
        if db is None or c.args.get("table") is None:
            continue
        schema, table = _up(db), _up(c.args["table"])
        if (schema, table) in resolved:
            new = resolved[(schema, table)]
            if new != schema:
                edit_schema(db, new)
        elif schema in GOVERNED or schema in GOVERNED_ALIASES:
            raise Blocked("UNRESOLVED_COLUMN", f"column reference {c.sql()} names a table not in FROM")

    out = sql
    for start, end, rep in sorted(set(edits), key=lambda e: e[0], reverse=True):
        out = out[:start] + rep + out[end + 1:]

    return Retargeted(sql=out, original_sql=sql, tables_read=sorted(set(reads_out)),
                      tables_written=sorted(set(writes_out)), to_materialise=to_mat,
                      creates=creates, kind=kind)
