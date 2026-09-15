"""Unmerge against a live instance: last APPLIED is undone first, whatever order requests were made in.

    DRYDOCK_LIVE=1 uv run pytest tests/test_live_unmerge.py -v

Reproduces the reviewer path that used to lose data: two held requests, approved in the reverse of the
order they were requested. Unmerging the first-applied one used to rename an archive that predated the
second merge back into place, silently discarding it while the fingerprint still "matched".
"""

from __future__ import annotations

__dialect_lint__ = "exempt"  # test 2 writes GOLDEN out of band on purpose, as test 18.7 does

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

pytestmark = pytest.mark.skipif(os.environ.get("DRYDOCK_LIVE") != "1",
                                reason="live: set DRYDOCK_LIVE=1 with a seeded, verified instance")


@pytest.fixture()
def db():
    from drydock import er, events
    from drydock.db import Db
    from reset import reset
    events.clear_sinks()
    er.install_hooks()
    d = Db("svc")
    reset(d)
    return d


def keys(db, n, offset=0):
    return [r[0] for r in db.rows(f"SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS ORDER BY GOLDEN_ID LIMIT {n} OFFSET {offset}")]


def in_list(ks):
    return ", ".join("'" + k + "'" for k in ks)


def fp(db):
    from drydock.hashing import table_fingerprint
    return table_fingerprint(db, "GOLDEN", "CUSTOMERS").hex


def branch_with(db, sql):
    from drydock import branch
    bid = branch.open_branch(db, "unmerge order", None, None, opened_by="human")["branch_id"]
    assert branch.run_in_branch(db, bid, sql, "unmerge order test")["status"] == "OK"
    return bid


def test_unmerge_follows_application_order_not_request_order(db):
    from drydock import gate, merge
    n = int(db.scalar("SELECT COUNT(*) FROM GOLDEN.CUSTOMERS")) * 12 // 1000      # 1.2%: over tier 2's 1%, under 5%
    b1 = branch_with(db, f"DELETE FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID IN ({in_list(keys(db, n))})")
    m1 = gate.request_merge(db, b1, 0.99, "requested first")
    b2 = branch_with(db, f"DELETE FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID IN ({in_list(keys(db, n, 5000))})")
    m2 = gate.request_merge(db, b2, 0.99, "requested second")
    assert m1["decision"] == m2["decision"] == "PENDING"
    fp0 = fp(db)
    assert merge.apply(db, m2["merge_id"], "reviewer")["decision"] == "MERGED"     # approved first
    fp_between = fp(db)
    assert merge.apply(db, m1["merge_id"], "reviewer")["decision"] == "MERGED"     # approved last: on top
    refused = merge.unmerge(db, m2["merge_id"])
    assert refused["ok"] is False and refused["code"] == "UNMERGE_SUPERSEDED"
    assert refused["superseded_by"] == m1["merge_id"]
    assert merge.unmerge(db, m1["merge_id"])["fingerprint_match"] is True
    assert fp(db) == fp_between
    assert merge.unmerge(db, m2["merge_id"])["fingerprint_match"] is True
    assert fp(db) == fp0


def test_unmerge_refuses_when_golden_changed_outside_the_ledger(db):
    from drydock import gate, merge
    k = keys(db, 4)
    b = branch_with(db, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'merged' WHERE GOLDEN_ID IN ({in_list(k[:3])})")
    m = gate.request_merge(db, b, 0.999, "small change")
    assert m["decision"] == "MERGED"
    db.run(f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'out of band' WHERE GOLDEN_ID = '{k[3]}'")
    before = fp(db)
    r = merge.unmerge(db, m["merge_id"])
    assert r["ok"] is False and r["code"] == "GOLDEN_CHANGED_SINCE_MERGE"
    assert fp(db) == before                                                       # nothing was undone
