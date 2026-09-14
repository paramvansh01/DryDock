"""The Database view's read path: SELECT only, composed from validated identifiers.

No live instance needed — a stub Db records the statements browse.py composes, and
tests/test_dialect.py lints them for real against the dialect rules.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from drydock import browse

COLS = [("CUSTOMER_ID", "DECIMAL(18,0)"), ("FULL_NAME", "VARCHAR(200) UTF8"),
        ("EMAIL", "VARCHAR(200) UTF8"), ("UPDATED_AT", "TIMESTAMP")]


class StubDb:
    """Answers the two catalogue reads browse.py makes and records everything it was asked."""

    def __init__(self, cols=COLS, page=None):
        self.cols, self.page, self.sql = cols, page or [], []

    def rows(self, sql):
        self.sql.append(sql)
        return self.cols if "EXA_ALL_COLUMNS" in sql else self.page

    def scalar(self, sql):
        self.sql.append(sql)
        return 7 if "COUNT(*)" in sql else dt.datetime(2026, 9, 15, 12, 0, 0)

    @property
    def page_sql(self):
        return next(s for s in self.sql if "EXA_ALL_COLUMNS" not in s and "COUNT(*)" not in s
                    and not s.startswith("SELECT CURRENT_TIMESTAMP"))


def test_page_is_a_select_with_an_order_and_a_bounded_limit():
    db = StubDb()
    r = browse.rows(db, "golden", "customers", limit=10_000, offset=20)
    assert r["limit"] == browse.MAX_LIMIT                      # capped, whatever was asked for
    assert db.page_sql == ("SELECT CUSTOMER_ID, FULL_NAME, EMAIL, UPDATED_AT FROM GOLDEN.CUSTOMERS "
                           f"ORDER BY CUSTOMER_ID ASC LIMIT {browse.MAX_LIMIT} OFFSET 20")
    assert r["order"] == "CUSTOMER_ID" and r["order_implicit"] is True   # paging is never unordered
    assert all(s.startswith("SELECT ") for s in db.sql)          # nothing but reads, ever


def test_sort_column_must_be_a_column_of_the_table():
    db = StubDb()
    browse.rows(db, "GOLDEN", "CUSTOMERS", order="email", direction="desc")
    assert "ORDER BY EMAIL DESC" in db.page_sql
    with pytest.raises(ValueError, match="no such column"):
        browse.rows(StubDb(), "GOLDEN", "CUSTOMERS", order="NOPE")


@pytest.mark.parametrize("schema", ["PROBE_SCRATCH", "SYS", "EXA_STATISTICS", "BR_X; DROP SCHEMA GOLDEN"])
def test_only_drydocks_own_schemas_are_browsable(schema):
    with pytest.raises(ValueError):
        browse.rows(StubDb(), schema, "CUSTOMERS")


def test_a_branch_schema_is_browsable():
    db = StubDb()
    browse.rows(db, "BR_7F2A", "CUSTOMERS")
    assert "FROM BR_7F2A.CUSTOMERS" in db.page_sql


@pytest.mark.parametrize("bad", ["CUSTOMERS; DROP SCHEMA GOLDEN CASCADE", "CUSTOMERS WHERE 1=1", "a b"])
def test_an_unsafe_table_name_is_refused(bad):
    with pytest.raises(ValueError):
        browse.rows(StubDb(), "GOLDEN", bad)


def test_filter_searches_character_columns_only_and_escapes_the_needle():
    db = StubDb()
    r = browse.rows(db, "GOLDEN", "CUSTOMERS", q="o'brien")
    assert r["searched"] == ["FULL_NAME", "EMAIL"]             # not the DECIMAL or the TIMESTAMP
    assert ("WHERE (UPPER(FULL_NAME) LIKE '%O''BRIEN%' ESCAPE '!' "
            "OR UPPER(EMAIL) LIKE '%O''BRIEN%' ESCAPE '!')") in db.page_sql
    assert db.page_sql.count("WHERE") == 1
    count = next(s for s in db.sql if "COUNT(*)" in s)
    assert "WHERE (UPPER(FULL_NAME)" in count                  # the total counts the same filtered set


@pytest.mark.parametrize("needle,pattern", [
    ("50%", "'%50!%%'"),                                   # a typed % is the character, not a wildcard
    ("SOURCE_A", "'%SOURCE!_A%'"),                         # and neither is _
    ("a!b", "'%A!!B%'"),                                   # the escape character escapes itself
])
def test_a_wildcard_typed_into_the_filter_is_matched_literally(needle, pattern):
    db = StubDb()
    browse.rows(db, "GOLDEN", "CUSTOMERS", q=needle)
    assert f"UPPER(FULL_NAME) LIKE {pattern} ESCAPE '!'" in db.page_sql


def test_filter_with_no_character_column_searches_nothing():
    db = StubDb(cols=[("N", "DECIMAL(18,0)")])
    r = browse.rows(db, "GOLDEN", "CUSTOMERS", q="abc")
    assert r["searched"] == [] and "WHERE" not in db.page_sql


def test_values_come_back_json_safe_without_losing_what_they_are():
    db = StubDb(page=[[Decimal("42"), Decimal("1.5"), dt.date(2026, 9, 15), None, b"\xde\xad", True]])
    r = browse.rows(db, "GOLDEN", "CUSTOMERS")
    assert r["rows"][0] == [42, 1.5, "2026-09-15", None, "dead", True]


def test_a_value_json_cannot_carry_is_described_rather_than_breaking_the_page():
    db = StubDb(page=[[float("nan"), float("inf"), 1.5]])
    assert browse.rows(db, "GOLDEN", "CUSTOMERS")["rows"][0] == ["nan", "inf", 1.5]


def test_a_missing_table_is_named_not_guessed_at():
    with pytest.raises(ValueError, match="no such table"):
        browse.rows(StubDb(cols=[]), "GOLDEN", "GHOST")
