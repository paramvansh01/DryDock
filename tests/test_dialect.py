"""Gate 1+2 over the whole repo, plus tests of the linter itself.
Runs on every phase transition. A FAIL anywhere in the repo fails the build."""

from __future__ import annotations

__dialect_lint__ = "exempt"  # contains deliberately bad SQL samples

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dialect_lint as dl  # noqa: E402
from _common import split_sql, strip_literals_and_comments  # noqa: E402


def test_repo_has_no_dialect_failures():
    findings, _exempt, _n = dl.lint_paths([ROOT / d for d in dl.SCAN_DIRS])
    fails = [f for f in findings if f.level == "FAIL"]
    assert not fails, "\n".join(f"{f.origin}: {f.gate} {f.message}\n  {f.sql[:200]}" for f in fails)


def _levels(sql, **kw):
    return [(f.level, f.gate, f.message.split(":")[0]) for f in dl.lint_sql(sql, "t", **kw)]


@pytest.mark.parametrize("sql,banned_id", [
    ("INSERT INTO t (a) VALUES (1) RETURNING a", "RETURNING"),
    ("INSERT INTO t (a) VALUES (1) ON CONFLICT DO NOTHING", "ON_CONFLICT"),
    ("SELECT a::int FROM t", "PG_CAST"),
    ("SELECT * FROM t WHERE a ILIKE 'x'", "ILIKE"),
    ("SELECT string_agg(a, ',') FROM t", "STRING_AGG"),
    ("SELECT now()", "NOW_FN"),
    ("CREATE TABLE t (a TEXT)", "TEXT_TYPE"),
    ("SELECT `a` FROM t", "BACKTICK"),
])
def test_postgres_isms_fail_gate2(sql, banned_id):
    fails = [f for f in dl.lint_sql(sql, "t", dialect=[]) if f.level == "FAIL" and f.gate == "G2"]
    assert any(banned_id in f.message for f in fails), fails


def test_literals_and_suffixed_names_do_not_trip_gate2():
    sql = "SELECT HASH_SHA256(a || '::' || b), ERROR_TEXT, CONTEXT FROM t WHERE x = 'RETURNING now()'"
    assert not [f for f in dl.lint_sql(sql, "t", dialect=[]) if f.level == "FAIL"]


def test_opaque_statement_needs_proven_entry():
    sql = "RENAME TABLE PROBE_SCRATCH.T1 TO T2"
    assert any(f.level == "FAIL" and f.gate == "G1" for f in dl.lint_sql(sql, "t", dialect=[]))
    proven = [dl.DialectEntry("Same-schema RENAME", "PROVEN",
                              "RENAME TABLE PROBE_SCRATCH.T1 TO T2;", set())]
    assert not [f for f in dl.lint_sql("RENAME TABLE GOLDEN.CUSTOMERS TO CUSTOMERS__ARCH_7",
                                       "t", dialect=proven) if f.level == "FAIL"]


def test_disproven_entry_does_not_whitelist():
    disproven = [dl.DialectEntry("Cross-schema RENAME", "DISPROVEN", "RENAME TABLE A.T TO B.T", set())]
    assert any(f.level == "FAIL" for f in dl.lint_sql("RENAME TABLE A.T TO T2", "t", dialect=disproven))


def test_allows_line_whitelists_a_banned_id_only_when_proven():
    sql = 'SELECT 1 AS "x"'
    assert any(f.level == "FAIL" for f in dl.lint_sql(sql, "t", dialect=[]))
    ok = [dl.DialectEntry("dq ident", "PROVEN", 'SELECT 1 AS "x"', {"DQUOTE_IDENT"})]
    assert not [f for f in dl.lint_sql(sql, "t", dialect=ok) if f.level == "FAIL"]
    no = [dl.DialectEntry("dq ident", "DISPROVEN", 'SELECT 1 AS "x"', {"DQUOTE_IDENT"})]
    assert any(f.level == "FAIL" for f in dl.lint_sql(sql, "t", dialect=no))


def test_unparseable_fails():
    assert any(f.level == "FAIL" for f in dl.lint_sql("SELEC FROM WHERE", "t", dialect=[]))


def test_sqlglot_identity_rewrite_is_flagged():
    # sqlglot's exasol writer turns IDENTITY into AUTO_INCREMENT. Never round-trip DDL.
    fs = dl.lint_sql("CREATE TABLE t (id DECIMAL(18,0) IDENTITY, v VARCHAR(10))", "t", dialect=[])
    assert any(f.level == "WARN" and "AUTO_INCREMENT" in f.message for f in fs)


def test_assert_clean_raises_on_generated_postgres():
    with pytest.raises(dl.DialectError):
        dl.assert_clean("UPDATE t SET a = b::text RETURNING a")


def test_dialect_md_format_example_is_not_an_entry():
    titles = [e.title for e in dl.load_dialect()]
    assert "<construct>" not in titles
    assert all(e.status != "PROVEN" or e.sql for e in dl.load_dialect())


def test_split_sql_handles_script_blocks_and_quotes():
    text = ("CREATE SCHEMA A;\nINSERT INTO A.T VALUES ('x;y');\n--/\n"
            "CREATE OR REPLACE PYTHON3 SCALAR SCRIPT A.S(x VARCHAR(9)) RETURNS VARCHAR(9) AS\n"
            "def run(ctx):\n    return ctx.x + ';'\n/\nSELECT 1;")
    parts = split_sql(text)
    assert len(parts) == 4
    assert parts[1] == "INSERT INTO A.T VALUES ('x;y')"
    assert parts[2].endswith("return ctx.x + ';'")


def test_strip_literals():
    assert strip_literals_and_comments("SELECT 'a::b' -- ::\n, x") == "SELECT '' \n, x"


def test_db_execute_is_firewalled_before_any_connection():
    """The firewall is enforced at the single execution
    choke point, so dynamic f-string SQL cannot slip past a static check."""
    from drydock.db import Db
    from drydock.lintguard import DialectError

    db = Db("svc")  # never connects: the firewall must refuse first
    with pytest.raises(DialectError):
        db.execute("UPDATE GOLDEN.CUSTOMERS SET CITY = 'x' RETURNING CITY")
    with pytest.raises(DialectError):
        db.execute("SELECT a::int FROM GOLDEN.CUSTOMERS")
    assert db._conn is None


def test_no_product_module_bypasses_db_execute():
    """Only drydock/db.py may call pyexasol's execute; everything else goes through Db.execute."""
    import re
    offenders = []
    for p in (ROOT / "drydock").glob("*.py"):
        if p.name == "db.py":
            continue
        src = p.read_text()
        if re.search(r"\bpyexasol\b", src) or re.search(r"\.conn\.execute\(", src):
            offenders.append(p.name)
    for p in list((ROOT / "agent").glob("*.py")) + list((ROOT / "bench").glob("*.py")):
        if re.search(r"\bpyexasol\b|\.conn\.execute\(", p.read_text()):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, offenders
