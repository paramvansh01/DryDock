"""Layer-2 pure logic: mapping validation, blocking-SQL checks, closure, and the
guarantee that the adjudicator's input carries no personal data."""

from __future__ import annotations

__dialect_lint__ = "exempt"  # feeds hostile SQL to the validators

import re

import pytest

from drydock import er
from drydock.adjudicate import render_item

GOOD_MAPPING = {
    "FULL_NAME": "FIRST_NAME || ' ' || LAST_NAME", "EMAIL": "EMAIL_ADDR", "PHONE": "MOBILE",
    "ADDR_LINE": "STREET", "CITY": "TOWN", "POSTCODE": "ZIP",
    "COUNTRY": "CASE NATION WHEN 'GB' THEN 'United Kingdom' ELSE NATION END",
    "DATE_OF_BIRTH": "TO_DATE(DOB, 'DD/MM/YYYY')",
}


def test_good_mapping_validates():
    assert er.validate_mapping(GOOD_MAPPING) == GOOD_MAPPING


@pytest.mark.parametrize("bad", [
    "(SELECT MAX(A_ID) FROM BENCH.TRUE_PAIRS)", "MOBILE FROM X", "MOBILE WHERE 1=1", "MOBILE, ZIP",
    "a.EMAIL", "EMAIL", "DRYDOCK.CANON(MOBILE)", "SYS_CONTEXT(1)", "(SELECT 1)",
])
def test_hostile_mapping_expressions_rejected(bad):
    with pytest.raises(er.ErError):
        er.validate_b_expr(bad)


def test_mapping_must_cover_every_field():
    m = dict(GOOD_MAPPING)
    del m["EMAIL"]
    with pytest.raises(er.ErError):
        er.validate_mapping(m)


def test_blocking_sql_accepts_a_proper_rule():
    sql = ("SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID FROM SOURCE_A.CUSTOMERS a "
           "JOIN SOURCE_B.CLIENTS b ON LOWER(TRIM(a.EMAIL)) = LOWER(TRIM(b.EMAIL_ADDR))")
    assert er.check_blocking_sql(sql) == sql


@pytest.mark.parametrize("bad", [
    "SELECT a.CUST_ID AS A_ID, t.B_ID AS B_ID FROM SOURCE_A.CUSTOMERS a JOIN BENCH.TRUE_PAIRS t ON t.A_ID = a.CUST_ID",
    "SELECT GOLDEN_ID AS A_ID, SOURCE_B_REF AS B_ID FROM GOLDEN.CUSTOMERS",
    "SELECT a.CUST_ID, b.CLIENT_REF FROM SOURCE_A.CUSTOMERS a, SOURCE_B.CLIENTS b",
    "DELETE FROM SOURCE_A.CUSTOMERS",
    "SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID, 1 AS X FROM SOURCE_A.CUSTOMERS a, SOURCE_B.CLIENTS b",
])
def test_blocking_sql_refusals(bad):
    with pytest.raises(er.ErError):
        er.check_blocking_sql(bad)


def test_closure_detects_components_larger_than_two():
    pairs = [(1, "AB", "A1", "B1"), (2, "AB", "A2", "B1"), (3, "AB", "A3", "B3"), (4, "AA", "A4", "A5"),
             (5, "AA", "A5", "A6")]
    c = er.components(pairs)
    assert c[1][1] == 3 and c[2][1] == 3 and c[1][0] == c[2][0]
    assert c[3][1] == 2
    assert c[4][1] == 3 and c[5][1] == 3


def test_closure_namespaces_a_and_b_ids():
    # the same string in A and B is two different records
    c = er.components([(1, "AB", "X1", "X1"), (2, "AB", "X2", "X2")])
    assert c[1][1] == 2 and c[2][1] == 2


def test_closure_is_order_independent():
    pairs = [(1, "AB", "A1", "B1"), (2, "AB", "A2", "B1"), (3, "AA", "A1", "A9")]
    a = er.components(pairs)
    b = er.components(list(reversed(pairs)))
    assert {k: v[1] for k, v in a.items()} == {k: v[1] for k, v in b.items()}


SIG = {"email": 0, "phone": 1000, "name": 1000, "addr": 1000, "dob": 0, "dob_gap_years": 28,
       "suffix_conflict": 1, "business_conflict": 0, "given_initial_conflict": 0, "same_family": 1}


def test_compare_summary_describes_the_smith_pair():
    s = er.compare_summary(SIG)
    assert "28 years apart" in s and "suffix conflict" in s and "same surname" in s


def test_adjudicator_input_contains_no_personal_data():
    text = render_item({"pair_id": 7, "kind": "AB", "summary": er.compare_summary(SIG)},
                       [{"id": 14, "verdict": "REJECTED", "context": er.compare_summary(SIG),
                         "note": "father and son - never merge on shared address alone"}])
    assert not re.search(r"@|\d{6,}|\b\d{4}-\d{2}-\d{2}\b|\d{2}/\d{2}/\d{4}", text)
    assert "#14 REJECTED" in text


def test_run_tag_is_a_safe_identifier():
    assert er.run_tag("demo-1") == "DEMO_1"
    assert er.run_tag("2026 ab") == "R_2026_AB"


def test_panel_sql_is_exasol_clean():
    from drydock.lintguard import dialect_lint
    for sql in (er.panel_sql("r1", "AB", "ER_WORK.A_NORM_R1", "ER_WORK.B_NORM_R1", 0.75),
                "CREATE OR REPLACE TABLE ER_WORK.A_NORM_R1 AS "
                + er._norm_select("SOURCE_A.CUSTOMERS", {f: f for f in er.GOLDEN_FIELDS}, "CUST_ID")):
        assert not [f for f in dialect_lint.lint_sql(sql, "t") if f.level == "FAIL"]
