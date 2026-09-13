"""Every table and column the code names must exist in the schema derived from the
project's own DDL. Offline; scripts/probe_all.py is the live counterpart.
"""

from __future__ import annotations

__dialect_lint__ = "exempt"

import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dialect_lint as dl  # noqa: E402

from drydock import catalogue, er  # noqa: E402

TAG = "R_SEMANTIC_TEST"
MAPPING = None


@lru_cache(maxsize=1)
def generated() -> list[tuple[str, str]]:
    from agent import playbook
    m = playbook.MAPPING
    a = {f: f for f in er.GOLDEN_FIELDS}
    out = [("er.norm_a", f"CREATE OR REPLACE TABLE ER_WORK.A_NORM_{TAG} AS " + er._norm_select("SOURCE_A.CUSTOMERS", a, "CUST_ID")),
           ("er.norm_b", f"CREATE OR REPLACE TABLE ER_WORK.B_NORM_{TAG} AS " + er._norm_select("SOURCE_B.CLIENTS", m, "CLIENT_REF"))]
    out += [(f"er.publish.{i}", s) for i, s in enumerate(er.publish_sql("r", TAG, m))]
    return out


@lru_cache(maxsize=1)
def cat():
    return catalogue.build([s for _, s in generated()])


def test_catalogue_is_derived_not_empty():
    c = cat()
    assert c["GOLDEN.CUSTOMERS"] == c["BENCH.GOLDEN_CLEAN"]           # via the reset CTAS
    assert c["GOLDEN_V.CUSTOMERS"] == c["GOLDEN.CUSTOMERS"]           # via the view in 02_grants.sql
    assert f"ER_WORK.MATCHES_{TAG}" in c and "B_EMAIL" in c[f"ER_WORK.MATCHES_{TAG}"]
    assert "DATE_OF_BIRTH" in c["SOURCE_A.CUSTOMERS"] and "DOB" in c["SOURCE_B.CLIENTS"]


def test_published_columns_match_what_publish_actually_creates():
    """er.published_columns() is what the agent is told; it must equal what publish_sql creates."""
    c = cat()
    pub = er.published_columns()
    assert c[f"ER_WORK.MATCHES_{TAG}"] == pub["matches"]
    assert c[f"ER_WORK.NEW_{TAG}"] == pub["new_customers"]
    assert c[f"ER_WORK.DEDUP_{TAG}"] == pub["dedup"]


@pytest.mark.parametrize("sql,kind", [
    ("SELECT CITTY FROM GOLDEN.CUSTOMERS", "unknown_column"),
    ("SELECT * FROM GOLDEN.USERS", "unknown_table"),
    ("UPDATE GOLDEN.CUSTOMERS SET CUSTOMER_ID = 'x'", "unknown_column"),
    ("SELECT a.EMAILX FROM SOURCE_A.CUSTOMERS a", "unknown_column"),
    ("SELECT COUNT(*) FROM DRYDOCK.BRANCH", "unknown_table"),
])
def test_checker_catches_hallucinated_names(sql, kind):
    r = catalogue.check_sql(sql, cat())
    assert any(p.kind == kind for p in r.problems), r.problems


def _static_statements():
    items, _exempt = dl.collect([ROOT / d for d in dl.SCAN_DIRS])
    return [(o, s) for o, s, templ in items if not templ]


@pytest.mark.parametrize("origin,sql", _static_statements(), ids=lambda x: x if isinstance(x, str) and len(x) < 60 else None)
def test_every_static_statement_names_real_tables_and_columns(origin, sql):
    r = catalogue.check_sql(sql, cat())
    assert r.ok, f"{origin}: {[(p.kind, p.detail) for p in r.problems]}\n{sql[:300]}"


@pytest.mark.parametrize("origin,sql", generated(), ids=lambda x: x if isinstance(x, str) and len(x) < 60 else None)
def test_generated_er_sql_names_real_tables_and_columns(origin, sql):
    r = catalogue.check_sql(sql, cat())
    assert r.ok, f"{origin}: {[(p.kind, p.detail) for p in r.problems]}"


def test_panel_and_classification_sql_are_semantically_valid():
    a, b = f"ER_WORK.A_NORM_{TAG}", f"ER_WORK.B_NORM_{TAG}"
    for sql in (er.panel_sql("r", "AB", a, b, 0.75), er.signals_select(a, b)):
        r = catalogue.check_sql(sql, cat())
        assert r.ok, [(p.kind, p.detail) for p in r.problems]


def test_playbook_sql_is_semantically_valid():
    from agent import playbook
    for kind, rule, sql in playbook.BLOCKING:
        r = catalogue.check_sql(sql, cat())
        assert r.ok, (rule, [(p.kind, p.detail) for p in r.problems])
    for cls in ("EXACT_EMAIL", "PHONE_ADDRESS", "FUZZY_NAME", "NEW_CUSTOMERS", "INTERNAL_DEDUP"):
        for sql, _why in playbook.class_sql(cls, TAG):
            r = catalogue.check_sql(sql, cat())
            assert r.ok, (cls, [(p.kind, p.detail) for p in r.problems], sql[:300])


def test_no_ddl_identifier_is_a_reserved_word():
    """A reserved word used as an identifier fails at DDL time ([42000] syntax error), yet sqlglot parses it
    happily. The authority is the instance's own keyword list, snapshotted from EXA_SQL_KEYWORDS in
    sql/reserved_keywords.json. Every table and column Drydock creates must avoid it."""
    import json
    snap = json.loads((ROOT / "sql" / "reserved_keywords.json").read_text())
    reserved = set(snap["reserved"])
    assert len(reserved) > 100, "keyword snapshot looks truncated"
    c = catalogue.build()
    checked, clashes = 0, []
    for table, cols in c.items():
        for name in [table.split(".")[-1], *cols]:
            checked += 1
            if name.upper() in reserved:
                clashes.append(f"{table}.{name}")
    assert checked > 100, f"only {checked} identifiers seen; the catalogue did not load"
    assert not clashes, f"reserved words used as identifiers (quote-free SQL will not parse): {clashes}"
