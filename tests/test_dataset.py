"""Datasets: the pipeline reads the active dataset's tables, and uploads stay read-only. Offline."""

from __future__ import annotations

import pytest

from agent import playbook
from drydock import catalogue, dataset, er, merge
from drydock.lintguard import clean
from drydock.retarget import Blocked, BranchState, retarget


def test_the_demo_is_scored_and_uploads_are_not():
    assert dataset.DEMO.scored and dataset.DEMO.agent
    assert not dataset.UPLOAD.scored and not dataset.UPLOAD.agent
    assert dataset.BY_NAME == {"demo": dataset.DEMO, "upload": dataset.UPLOAD}


def test_blocking_follows_the_dataset_and_leaves_the_demo_rules_alone():
    assert playbook.blocking(dataset.DEMO) == playbook.BLOCKING
    up = playbook.blocking(dataset.UPLOAD)
    assert len(up) == len(playbook.BLOCKING)
    for _kind, _rule, sql in up:
        assert "UPLOADS.SOURCE_" in sql and "SOURCE_A.CUSTOMERS" not in sql and "SOURCE_B.CLIENTS" not in sql
        er.check_blocking_sql(sql)                  # read-only schemas only: UPLOADS is READ_ONLY


def test_published_decisions_read_the_uploaded_tables():
    m = dict(playbook.MAPPING)
    for sql in er.publish_sql("r", "T", m, dataset.UPLOAD):
        assert "SOURCE_A.CUSTOMERS" not in sql and "SOURCE_B.CLIENTS" not in sql
        clean(sql, "test")
    assert "UPLOADS.SOURCE_B.CLIENT_REF = c.B_ID" in er.publish_sql("r", "T", m, dataset.UPLOAD)[0]


def test_branch_sql_can_read_uploads_but_never_write_them():
    ok = retarget("SELECT COUNT(*) FROM UPLOADS.SOURCE_A", BranchState("BR_TEST"))
    assert ok.tables_written == []
    for sql in ("DELETE FROM UPLOADS.SOURCE_A", "UPDATE UPLOADS.SOURCE_B SET TOWN = 'x'",
                "INSERT INTO UPLOADS.GOLDEN_SEED (GOLDEN_ID) VALUES ('x')"):
        with pytest.raises(Blocked):
            retarget(sql, BranchState("BR_TEST"))


def test_names_keep_letters_from_every_script():
    sql = er._norm_select(dataset.UPLOAD.a, {f: f for f in er.GOLDEN_FIELDS}, "CUST_ID")
    assert r"'[^\p{L} '']'" in sql and "A-Za-z" not in sql


def test_upload_tables_are_in_the_catalogue_with_the_demo_columns():
    cat = catalogue.build()
    assert cat["UPLOADS.SOURCE_A"] == cat["SOURCE_A.CUSTOMERS"]
    assert cat["UPLOADS.SOURCE_B"] == cat["SOURCE_B.CLIENTS"]
    assert cat["UPLOADS.GOLDEN_SEED"] == cat["BENCH.GOLDEN_CLEAN"]
    assert "DRYDOCK.DATASET" in cat


def test_unmerge_order_is_application_order_not_request_order():
    sql = merge.later_merge_sql(87, "CUSTOMERS")
    clean(sql, "test")
    assert "l.RESOLVED_AT > m.RESOLVED_AT" in sql and "ORDER BY l.RESOLVED_AT DESC" in sql
    assert "MERGE_ID > 87" not in sql               # the old rule: ids are handed out at request time
