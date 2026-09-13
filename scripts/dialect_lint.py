"""Gates 1 and 2 of the dialect firewall.

    uv run python scripts/dialect_lint.py                 # whole repo
    uv run python scripts/dialect_lint.py path/a.sql drydock/diff.py
    uv run python scripts/dialect_lint.py --sql "SELECT ..."

Gate 1  sqlglot.parse_one(sql, read="exasol") must succeed.
        - parses as postgres but not exasol  -> FAIL, both trees printed
        - sqlglot falls back to an opaque exp.Command (RENAME, CREATE SCRIPT,
          ...) -> FAIL unless sql/DIALECT.md has a PROVEN entry with the same
          leading-keyword signature. sqlglot cannot vouch for what it did not
          parse, so the live instance has to.
        - sqlglot's exasol->exasol round-trip introduces a banned pattern
          (e.g. IDENTITY -> AUTO_INCREMENT) -> WARN: never render this through
          sqlglot.
Gate 2  scripts/banned.txt patterns over the literal-stripped SQL -> FAIL unless
        a PROVEN DIALECT.md entry says "ALLOWS: <ID>".

Note: sqlglot's exasol parser accepts RETURNING, ON CONFLICT, ILIKE and '::'
without complaint (sqlglot 30.18), so Gate 1 alone catches little. Gate 2 and
live probing (scripts/probe.py) carry the weight.

Runtime use: drydock modules that GENERATE SQL call assert_clean(sql) on the
final rendered string before executing it.

Files whose SQL is deliberately experimental (verify.py probes, lint tests)
declare  __dialect_lint__ = "exempt"  at module level and are skipped.
"""

from __future__ import annotations

import argparse
import ast
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from _common import DIALECT_MD, ROOT, split_sql, strip_literals_and_comments  # noqa: E402

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

BANNED_TXT = ROOT / "scripts" / "banned.txt"
SCAN_DIRS = ("sql", "drydock", "agent", "bench", "scripts")
# Case-SENSITIVE on purpose: repo convention is uppercase SQL keywords, which is
# how SQL in a Python literal is told apart from prose ("with --flag ...").
SQL_START = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|RENAME|GRANT|REVOKE|"
    r"WITH|TRUNCATE|OPEN\s+SCHEMA|COMMIT|ROLLBACK|IMPORT|EXPORT)\s")
OPAQUE_KEYWORDS = {
    "CREATE", "OR", "REPLACE", "PYTHON", "PYTHON3", "JAVA", "R", "LUA", "SCALAR",
    "SET", "SCRIPT", "RENAME", "TABLE", "SCHEMA", "VIEW", "USER", "ROLE",
    "CONNECTION", "IMPORT", "EXPORT", "FLUSH", "STATISTICS", "KILL", "SESSION",
    "ALTER", "EXECUTE", "OPEN", "CLOSE", "DESCRIBE", "PRELOAD", "RECOMPRESS",
    "REORGANIZE", "CONSUMER", "GROUP", "ADAPTER", "UDF", "IF", "EXISTS", "NOT",
    "FORCE", "VIRTUAL", "PRIORITY",
}


class DialectError(RuntimeError):
    pass


@dataclass
class Finding:
    level: str      # FAIL | WARN | INFO
    gate: str       # G1 | G2
    message: str
    origin: str = ""
    sql: str = ""


@dataclass
class Banned:
    id: str
    rx: re.Pattern[str]
    why: str


@dataclass
class DialectEntry:
    title: str
    status: str     # PROVEN | DISPROVEN | ?
    sql: str
    allows: set[str]


# ---------------------------------------------------------------- loaders

def load_banned(path: Path = BANNED_TXT) -> list[Banned]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) < 2:
            continue
        out.append(Banned(parts[0], re.compile(parts[1], re.I), parts[2] if len(parts) > 2 else ""))
    return out


def load_dialect(path: Path = DIALECT_MD) -> list[DialectEntry]:
    if not path.exists():
        return []
    entries = []
    text = re.sub(r"(?ms)^```.*?^```", "", path.read_text())  # format examples are not entries
    for chunk in re.split(r"(?m)^### ", text)[1:]:
        title, _, body = chunk.partition("\n")
        status_m = re.search(r"(?m)^(PROVEN|DISPROVEN)\b", body)
        sql_m = re.search(r"(?ms)^SQL:\s*(.*?)(?=^\s*RESULT:|^\s*NOTE:|^\s*ALLOWS:|\Z)", body)
        allows_m = re.findall(r"(?m)^ALLOWS:\s*(.*)$", body)
        allows = {a.strip() for line in allows_m for a in line.split(",") if a.strip()}
        entries.append(DialectEntry(title.strip(), status_m.group(1) if status_m else "?",
                                    sql_m.group(1).strip() if sql_m else "", allows))
    return entries


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).upper()


def opaque_signature(sql: str) -> str:
    words = []
    for w in _norm(strip_literals_and_comments(sql)).split(" "):
        if w in OPAQUE_KEYWORDS:
            if w not in ("OR", "REPLACE", "IF", "NOT", "EXISTS", "FORCE"):
                words.append(w)
        else:
            break
    return " ".join(words)


# ---------------------------------------------------------------- core

def _try_parse(sql: str, dialect: str):
    try:
        return sqlglot.parse_one(sql, read=dialect), None
    except Exception as e:  # sqlglot raises several error types
        return None, f"{type(e).__name__}: {e}"


def lint_sql(sql: str, origin: str = "", *, template: bool = False,
             banned: list[Banned] | None = None,
             dialect: list[DialectEntry] | None = None) -> list[Finding]:
    banned = load_banned() if banned is None else banned
    dialect = load_dialect() if dialect is None else dialect
    proven = [d for d in dialect if d.status == "PROVEN"]
    allowed_ids = {a for d in proven for a in d.allows}
    proven_exact = {_norm(d.sql) for d in proven if d.sql}
    findings: list[Finding] = []

    def add(level, gate, msg):
        findings.append(Finding(level, gate, msg, origin, sql))

    # ---- Gate 1
    exa, exa_err = _try_parse(sql, "exasol")
    if exa is None:
        pg, _ = _try_parse(sql, "postgres")
        if _norm(sql) in proven_exact:
            add("INFO", "G1", "sqlglot cannot parse, but exact statement is PROVEN in DIALECT.md")
        elif pg is not None:
            add("FAIL", "G1", "PARSES AS POSTGRES BUT NOT EXASOL — you wrote Postgres.\n"
                f"  exasol: {exa_err}\n  postgres tree: {pg!r}")
        elif template == "fragment":
            add("INFO", "G1", "declared # sql-fragment: composed at runtime; linted in full at Db.execute")
        else:
            # An f-string that does not parse as a complete statement (with its placeholders standing in
            # for values/identifiers) is a FAIL, not a warning.
            add("FAIL", "G1", f"exasol parse failed{' (f-string template)' if template else ''}: {exa_err}. "
                "Rewrite so values sit in literal/identifier positions, or mark the line '# sql-fragment' "
                "if it is deliberately composed (the runtime firewall in Db.execute then lints the final SQL).")
    elif isinstance(exa, exp.Command):
        sig = opaque_signature(sql)
        covered = bool(sig) and any(opaque_signature(d.sql) == sig for d in proven if d.sql)
        if covered:
            add("INFO", "G1", f"opaque to sqlglot ({sig!r}); covered by a PROVEN DIALECT.md entry")
        else:
            add("FAIL", "G1", f"opaque to sqlglot (exp.Command, signature {sig!r}) and no PROVEN "
                "DIALECT.md entry has that signature. Probe it (scripts/probe.py) and record it.")
    else:
        try:
            rendered = exa.sql(dialect="exasol")
            src_stripped = strip_literals_and_comments(sql)
            out_stripped = strip_literals_and_comments(rendered)
            for b in banned:
                if b.rx.search(out_stripped) and not b.rx.search(src_stripped):
                    add("WARN", "G1", f"sqlglot exasol round-trip INTRODUCES {b.id} "
                        f"({b.why}); never render this statement through sqlglot")
        except Exception as e:
            add("WARN", "G1", f"sqlglot could not re-render: {type(e).__name__}: {e}")

    # ---- Gate 2
    stripped = strip_literals_and_comments(sql)
    for b in banned:
        if b.rx.search(stripped):
            if b.id in allowed_ids:
                add("INFO", "G2", f"{b.id} present but ALLOWED by a PROVEN DIALECT.md entry")
            else:
                add("FAIL", "G2", f"banned pattern {b.id}: {b.why}")
    return findings


def assert_clean(sql: str, origin: str = "runtime") -> str:
    """Runtime guard for generated SQL. Returns sql unchanged or raises."""
    fails = [f for f in lint_sql(sql, origin) if f.level == "FAIL"]
    if fails:
        msg = "\n".join(f"{f.gate}: {f.message}" for f in fails)
        raise DialectError(f"generated SQL failed the dialect firewall ({origin}):\n{msg}\n---\n{sql}")
    return sql


# ---------------------------------------------------------------- extraction

def _is_exempt(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__dialect_lint__" for t in node.targets):
            return isinstance(node.value, ast.Constant) and node.value.value == "exempt"
    return False


def sql_from_python(path: Path) -> tuple[list[tuple[str, str, bool]], bool]:
    """Returns ([(origin, sql, is_template)], exempt)."""
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    if _is_exempt(tree):
        return [], True
    lines = source.splitlines()

    def not_sql(node) -> bool:
        """A string on a line marked '# not-sql' is prose that happens to start with a keyword."""
        return "# not-sql" in lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else False
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                    getattr(node.body[0], "value", None), ast.Constant):
                docstrings.add(id(node.body[0].value))
    fstring_parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings and id(node) not in fstring_parts and not not_sql(node):
            if SQL_START.match(node.value) and " " in node.value.strip():
                for s in split_sql(node.value):
                    out.append((f"{path.relative_to(ROOT)}:{node.lineno}", s, False))
        elif isinstance(node, ast.JoinedStr) and not not_sql(node):
            parts = []
            for v in node.values:
                parts.append(v.value if isinstance(v, ast.Constant) else "P_ARG")
            text = "".join(parts)
            if SQL_START.match(text) and " " in text.strip():
                frag = "# sql-fragment" in lines[node.lineno - 1]
                out.append((f"{path.relative_to(ROOT)}:{node.lineno} (f-string)", text, "fragment" if frag else True))
    return out, False


def collect(paths: list[Path]) -> tuple[list[tuple[str, str, bool]], list[str]]:
    items, exempt = [], []
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted(p.rglob("*.sql")) + sorted(p.rglob("*.py"))
        elif p.exists():
            files.append(p)
    for f in files:
        if ".venv" in f.parts or "node_modules" in f.parts:
            continue
        if f.suffix == ".sql":
            for i, s in enumerate(split_sql(f.read_text()), 1):
                items.append((f"{f.relative_to(ROOT)}#{i}", s, False))
        elif f.suffix == ".py":
            got, ex = sql_from_python(f)
            if ex:
                exempt.append(str(f.relative_to(ROOT)))
            items += got
    return items, exempt


def lint_paths(paths: list[Path]) -> tuple[list[Finding], list[str], int]:
    banned, dialect = load_banned(), load_dialect()
    items, exempt = collect(paths)
    findings = []
    for origin, sql, templ in items:
        findings += lint_sql(sql, origin, template=templ, banned=banned, dialect=dialect)
    return findings, exempt, len(items)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--sql")
    ap.add_argument("--quiet", action="store_true", help="only print FAILs")
    a = ap.parse_args()

    if a.sql:
        findings = [f for s in split_sql(a.sql) for f in lint_sql(s, "cli")]
        exempt, n = [], len(split_sql(a.sql))
    else:
        paths = [Path(p).resolve() for p in a.paths] or [ROOT / d for d in SCAN_DIRS]
        findings, exempt, n = lint_paths(paths)

    fails = [f for f in findings if f.level == "FAIL"]
    for f in findings:
        if a.quiet and f.level != "FAIL":
            continue
        one_line = re.sub(r"\s+", " ", f.sql)[:160]
        print(f"{f.level:4} {f.gate} {f.origin}\n     {f.message}\n     sql: {one_line}")
    for e in exempt:
        print(f"SKIP exempt file: {e}")
    print(f"\n{n} statements, {len(fails)} FAIL, "
          f"{sum(f.level == 'WARN' for f in findings)} WARN")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
