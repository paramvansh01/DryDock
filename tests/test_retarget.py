"""retarget.py is the highest-risk module, so it has the most tests: the core cases,
every hard block, and text-preservation guarantees.
"""

from __future__ import annotations

__dialect_lint__ = "exempt"  # deliberately feeds forbidden SQL to the retargeter

import pytest

from drydock.retarget import Blocked, BranchState, retarget

BR = "BR_1A2B3C4D"


def st(mat=(), created=()):
    return BranchState(BR, set(mat), set(created))


def blocked(sql, code, state=None):
    with pytest.raises(Blocked) as e:
        retarget(sql, state or st())
    assert e.value.code == code, (e.value.code, e.value.message)
    return e.value


# ---------------------------------------------------------------- core cases

def test_simple_update_materialises_and_rewrites():
    r = retarget("UPDATE GOLDEN.CUSTOMERS SET CITY = 'x'", st())
    assert r.sql == f"UPDATE {BR}.CUSTOMERS SET CITY = 'x'"
    assert r.to_materialise == ["CUSTOMERS"]
    assert r.tables_written == ["GOLDEN.CUSTOMERS"]


def test_update_with_subquery_in_where_reads_own_write_target():
    r = retarget("UPDATE GOLDEN.CUSTOMERS SET CITY = 'x' WHERE GOLDEN_ID IN "
                 "(SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS WHERE COUNTRY = 'GB')", st())
    assert r.sql.count(f"{BR}.CUSTOMERS") == 2
    assert "GOLDEN." not in r.sql


def test_cte_feeding_insert_into_golden():
    sql = ("INSERT INTO GOLDEN.CUSTOMERS (GOLDEN_ID, FULL_NAME) "
           "WITH n AS (SELECT CLIENT_REF, FIRST_NAME || ' ' || LAST_NAME AS NM FROM SOURCE_B.CLIENTS) "
           "SELECT 'G-' || CLIENT_REF, NM FROM n")
    r = retarget(sql, st())
    assert f"INSERT INTO {BR}.CUSTOMERS" in r.sql
    assert "FROM SOURCE_B.CLIENTS" in r.sql and "FROM n" in r.sql
    assert r.tables_read == ["SOURCE_B.CLIENTS"]


def test_correlated_subquery():
    sql = ("UPDATE GOLDEN.CUSTOMERS g SET EMAIL = (SELECT MAX(b.EMAIL_ADDR) FROM SOURCE_B.CLIENTS b "
           "WHERE b.CLIENT_REF = g.SOURCE_B_REF) WHERE g.EMAIL IS NULL")
    r = retarget(sql, st())
    assert r.sql.startswith(f"UPDATE {BR}.CUSTOMERS g SET")
    assert "FROM SOURCE_B.CLIENTS b" in r.sql


def test_multi_table_merge():
    sql = ("MERGE INTO GOLDEN.CUSTOMERS g USING (SELECT m.GOLDEN_ID, b.EMAIL_ADDR FROM ER_WORK.MATCHES_R1 m "
           "JOIN SOURCE_B.CLIENTS b ON b.CLIENT_REF = m.B_ID) s ON (g.GOLDEN_ID = s.GOLDEN_ID) "
           "WHEN MATCHED THEN UPDATE SET g.EMAIL = s.EMAIL_ADDR")
    r = retarget(sql, st())
    assert f"MERGE INTO {BR}.CUSTOMERS g" in r.sql
    assert set(r.tables_read) == {"ER_WORK.MATCHES_R1", "SOURCE_B.CLIENTS"}


def test_create_table_in_branch_then_read_it():
    r1 = retarget("CREATE TABLE PAIRS AS SELECT CUST_ID FROM SOURCE_A.CUSTOMERS", st())
    assert r1.sql == f"CREATE TABLE {BR}.PAIRS AS SELECT CUST_ID FROM SOURCE_A.CUSTOMERS"
    assert r1.creates == ["PAIRS"] and r1.to_materialise == []
    r2 = retarget("SELECT COUNT(*) FROM PAIRS", st(created={"PAIRS"}))
    assert r2.sql == f"SELECT COUNT(*) FROM {BR}.PAIRS"


def test_create_table_qualified_with_own_branch():
    r = retarget(f"CREATE TABLE {BR}.T2 (A DECIMAL(9,0))", st())
    assert r.sql == f"CREATE TABLE {BR}.T2 (A DECIMAL(9,0))" and r.creates == ["T2"]


def test_delete_with_join_subquery():
    sql = ("DELETE FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID IN (SELECT d.DUP_GOLDEN_ID FROM ER_WORK.DEDUP_R1 d "
           "JOIN SOURCE_A.CUSTOMERS a ON a.CUST_ID = d.A_ID)")
    r = retarget(sql, st())
    assert r.sql.startswith(f"DELETE FROM {BR}.CUSTOMERS WHERE")
    assert r.to_materialise == ["CUSTOMERS"]


def test_bench_reference_blocks():
    blocked("SELECT * FROM BENCH.TRUE_PAIRS", "FORBIDDEN_SCHEMA")
    blocked("UPDATE GOLDEN.CUSTOMERS SET CITY = (SELECT MAX(A_ID) FROM BENCH.TRUE_PAIRS)", "FORBIDDEN_SCHEMA")


def test_other_branch_blocks():
    blocked("SELECT * FROM BR_DEADBEEF.CUSTOMERS", "OTHER_BRANCH")


def test_reserved_archive_name_blocks():
    blocked("SELECT * FROM GOLDEN.CUSTOMERS__ARCH_1", "RESERVED_NAME")
    blocked("SELECT * FROM GOLDEN.CUSTOMERS__NEW_1", "RESERVED_NAME")
    blocked("SELECT * FROM GOLDEN_V.CUSTOMERS__UNDONE_3", "RESERVED_NAME")


def test_garbage_blocks_with_parser_message():
    e = blocked("SELEC FROM WHERE", "PARSE_ERROR")
    assert "Line" in e.message or "Invalid" in e.message


# ---------------------------------------------------------------- read routing

def test_unmaterialised_read_passes_through_to_golden():
    r = retarget("SELECT COUNT(*) FROM GOLDEN.CUSTOMERS", st())
    assert r.sql == "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS" and r.to_materialise == []


def test_materialised_read_sees_branch_copy():
    r = retarget("SELECT COUNT(*) FROM GOLDEN.CUSTOMERS", st(mat={"CUSTOMERS"}))
    assert r.sql == f"SELECT COUNT(*) FROM {BR}.CUSTOMERS"


def test_golden_v_alias_reads_golden_or_branch():
    assert retarget("SELECT * FROM GOLDEN_V.CUSTOMERS", st()).sql == "SELECT * FROM GOLDEN.CUSTOMERS"
    assert retarget("SELECT * FROM GOLDEN_V.CUSTOMERS", st(mat={"CUSTOMERS"})).sql == \
        f"SELECT * FROM {BR}.CUSTOMERS"


def test_write_via_golden_v_still_lands_in_branch():
    r = retarget("UPDATE GOLDEN_V.CUSTOMERS SET CITY = 'x'", st())
    assert r.sql == f"UPDATE {BR}.CUSTOMERS SET CITY = 'x'"
    assert r.tables_written == ["GOLDEN.CUSTOMERS"]


def test_fully_qualified_column_follows_its_table():
    r = retarget("UPDATE GOLDEN.CUSTOMERS SET CITY = UPPER(GOLDEN.CUSTOMERS.CITY)", st())
    assert r.sql == f"UPDATE {BR}.CUSTOMERS SET CITY = UPPER({BR}.CUSTOMERS.CITY)"


def test_lowercase_schema_is_normalised():
    r = retarget("update golden.customers set city = 'x'", st())
    assert r.sql == f"update {BR}.customers set city = 'x'"


def test_sources_readable():
    r = retarget("SELECT COUNT(*) FROM SOURCE_A.CUSTOMERS a JOIN SOURCE_B.CLIENTS b ON a.EMAIL = b.EMAIL_ADDR",
                 st())
    assert r.sql.startswith("SELECT COUNT(*) FROM SOURCE_A.CUSTOMERS a JOIN SOURCE_B.CLIENTS b")


# ---------------------------------------------------------------- hard blocks

@pytest.mark.parametrize("sql,code", [
    ("UPDATE SOURCE_A.CUSTOMERS SET CITY = 'x'", "WRITE_TO_READONLY"),
    ("DELETE FROM SOURCE_B.CLIENTS", "WRITE_TO_READONLY"),
    ("INSERT INTO ER_WORK.MATCHES_R1 SELECT * FROM ER_WORK.MATCHES_R1", "WRITE_TO_READONLY"),
    ("SELECT * FROM DRYDOCK.BRANCHES", "FORBIDDEN_SCHEMA"),
    ("SELECT * FROM SYS.EXA_ALL_TABLES", "FORBIDDEN_SCHEMA"),
    ("SELECT * FROM EXA_ALL_TABLES", "FORBIDDEN_SCHEMA"),
    ("SELECT * FROM PROBE_SCRATCH.T", "FORBIDDEN_SCHEMA"),
    ("SELECT * FROM SOMEWHERE.T", "UNKNOWN_SCHEMA"),
    ("DROP TABLE GOLDEN.CUSTOMERS", "DDL_ON_PRODUCTION"),
    ("ALTER TABLE GOLDEN.CUSTOMERS ADD COLUMN X VARCHAR(10)", "DDL_ON_PRODUCTION"),
    ("CREATE TABLE GOLDEN.EVIL AS SELECT 1 AS A", "DDL_ON_PRODUCTION"),
    ("DROP SCHEMA GOLDEN CASCADE", "DDL"),
    (f"DROP SCHEMA {BR} CASCADE", "DDL"),
    ("GRANT SELECT ON GOLDEN.CUSTOMERS TO PUBLIC", "PRIVILEGE"),
    ("COMMIT", "TRANSACTION"),
    ("ROLLBACK", "TRANSACTION"),
    ("OPEN SCHEMA GOLDEN", "SESSION"),
    ("SELECT * FROM CUSTOMERS", "UNQUALIFIED_REFERENCE"),
    ("UPDATE CUSTOMERS SET CITY = 'x'", "UNQUALIFIED_REFERENCE"),
    ("SELECT 1; SELECT 2", "MULTI_STATEMENT"),
    ("RENAME TABLE GOLDEN.CUSTOMERS TO X", "UNPARSED"),
])
def test_hard_blocks(sql, code):
    blocked(sql, code)


def test_create_user_blocked():
    with pytest.raises(Blocked):
        retarget("CREATE USER X IDENTIFIED BY \"pw\"", st())


# ---------------------------------------------------------------- text preservation

def test_original_text_preserved_except_references():
    sql = ("UPDATE GOLDEN.CUSTOMERS SET POSTCODE = UPPER(SUBSTR(POSTCODE, 1, 4))  -- keep SUBSTR\n"
           "WHERE REGEXP_REPLACE(PHONE, '[^0-9]', '') = '5550102233'")
    r = retarget(sql, st())
    assert r.sql == sql.replace("GOLDEN.CUSTOMERS", f"{BR}.CUSTOMERS", 1)
    assert "SUBSTR(" in r.sql and "SUBSTRING" not in r.sql


def test_string_literal_mentioning_golden_is_untouched():
    r = retarget("UPDATE GOLDEN.CUSTOMERS SET SURVIVORSHIP = 'from GOLDEN.CUSTOMERS'", st())
    assert r.sql == f"UPDATE {BR}.CUSTOMERS SET SURVIVORSHIP = 'from GOLDEN.CUSTOMERS'"


def test_create_with_identity_is_not_rewritten_to_auto_increment():
    r = retarget("CREATE TABLE T (ID DECIMAL(18,0) IDENTITY, A VARCHAR(10))", st())
    assert "IDENTITY" in r.sql and "AUTO_INCREMENT" not in r.sql
    assert r.sql == f"CREATE TABLE {BR}.T (ID DECIMAL(18,0) IDENTITY, A VARCHAR(10))"


def test_cte_name_unqualified_is_fine():
    r = retarget("WITH x AS (SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS) SELECT COUNT(*) FROM x", st())
    assert r.sql == "WITH x AS (SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS) SELECT COUNT(*) FROM x"


def test_window_function_and_join_on_branch():
    sql = ("CREATE TABLE RANKED AS SELECT GOLDEN_ID, ROW_NUMBER() OVER (PARTITION BY EMAIL ORDER BY GOLDEN_ID) RN "
           "FROM GOLDEN.CUSTOMERS")
    r = retarget(sql, st(mat={"CUSTOMERS"}))
    assert r.sql.startswith(f"CREATE TABLE {BR}.RANKED AS SELECT")
    assert r.sql.endswith(f"FROM {BR}.CUSTOMERS")


def test_truncate_golden_is_a_branch_write():
    r = retarget("TRUNCATE TABLE GOLDEN.CUSTOMERS", st())
    assert r.sql == f"TRUNCATE TABLE {BR}.CUSTOMERS" and r.to_materialise == ["CUSTOMERS"]


def test_insert_into_own_created_table():
    r = retarget("INSERT INTO PAIRS SELECT CUST_ID FROM SOURCE_A.CUSTOMERS", st(created={"PAIRS"}))
    assert r.sql == f"INSERT INTO {BR}.PAIRS SELECT CUST_ID FROM SOURCE_A.CUSTOMERS"


def test_drop_own_branch_table_allowed():
    r = retarget(f"DROP TABLE {BR}.PAIRS", st(created={"PAIRS"}))
    assert r.sql == f"DROP TABLE {BR}.PAIRS"


def test_quoted_identifiers():
    r = retarget('UPDATE "GOLDEN"."CUSTOMERS" SET CITY = \'x\'', st())
    assert "GOLDEN" not in r.sql.replace(BR, "")
    assert r.to_materialise == ["CUSTOMERS"]


def test_drop_golden_is_blocked_not_treated_as_read():
    # regression: sqlglot stores DROP targets under `tables`; this once passed as a READ
    blocked("DROP TABLE GOLDEN.CUSTOMERS", "DDL_ON_PRODUCTION")
    blocked("DROP VIEW GOLDEN_V.CUSTOMERS", "DDL_ON_PRODUCTION")


@pytest.mark.parametrize("sql", ["EXPLAIN SELECT 1", "DESCRIBE GOLDEN.CUSTOMERS",
                                 "ANALYZE DATABASE ESTIMATE STATISTICS", "KILL SESSION 5"])
def test_unknown_statement_kinds_fail_closed(sql):
    with pytest.raises(Blocked):
        retarget(sql, st())


def test_merge_using_a_plain_table_retargets_only_the_governed_side():
    """INTERNAL_DEDUP step 1 is a MERGE (Exasol rejects a subquery in UPDATE ... SET).
    The governed target moves to the branch; the ER_WORK source table is left alone."""
    from agent import playbook
    sql = playbook.class_sql("INTERNAL_DEDUP", "R1")[0][0]
    assert sql.startswith("MERGE INTO GOLDEN.CUSTOMERS")
    r = retarget(sql, st())
    assert f"MERGE INTO {BR}.CUSTOMERS g" in r.sql
    assert "USING ER_WORK.DEDUP_R1 d" in r.sql
    assert "GOLDEN.CUSTOMERS" not in r.sql
