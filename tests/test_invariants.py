"""System invariants, run against a live, seeded Exasol instance.

    DRYDOCK_LIVE=1 uv run pytest tests/test_invariants.py -v

Without DRYDOCK_LIVE=1 these are skipped. With it, a missing verification makes them
fail. The assertions are pinned by tests/invariants.lock.json.

Test 18.7 simulates an out-of-band writer by updating GOLDEN directly as the service
user; that is the scenario under test. Product code never writes GOLDEN outside
drydock/merge.py.
"""

from __future__ import annotations

__dialect_lint__ = "exempt"  # deliberately runs forbidden statements as the agent

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.skipif(os.environ.get("DRYDOCK_LIVE") != "1",
                                reason="live invariants: set DRYDOCK_LIVE=1 with a seeded, verified instance")


@pytest.fixture(scope="module")
def db():
    from drydock import er, events
    from drydock.db import Db
    events.clear_sinks()
    er.install_hooks()
    return Db("svc")


@pytest.fixture()
def clean(db):
    from reset import reset
    reset(db)
    return db


def golden_fp(db):
    from drydock.hashing import table_fingerprint
    return table_fingerprint(db, "GOLDEN", "CUSTOMERS").hex


def some_keys(db, n, offset=0):
    return [r[0] for r in db.rows(f"SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS ORDER BY GOLDEN_ID LIMIT {n} OFFSET {offset}")]


def in_list(keys):
    return ", ".join("'" + k + "'" for k in keys)


def open_(db, purpose="test", run_id=None, cls=None):
    from drydock import branch
    return branch.open_branch(db, purpose, run_id, cls, opened_by="human")["branch_id"]


def run(db, bid, sql):
    from drydock import branch
    out = branch.run_in_branch(db, bid, sql, "invariant test")
    assert out["status"] == "OK", out
    return out


# ------------------------------------------------------------------ data load

def test_loaded_counts(db):
    """Exactly the documented row counts and exactly 400 decoy pairs, by SELECT COUNT(*)."""
    assert int(db.scalar("SELECT COUNT(*) FROM SOURCE_A.CUSTOMERS")) == 36_600
    assert int(db.scalar("SELECT COUNT(*) FROM SOURCE_B.CLIENTS")) == 32_400
    assert int(db.scalar("SELECT COUNT(*) FROM BENCH.TRUE_PAIRS WHERE IS_DECOY")) == 400
    assert int(db.scalar("SELECT COUNT(*) FROM BENCH.TRUE_PAIRS WHERE IS_MATCH")) == 28_000
    assert int(db.scalar("SELECT COUNT(*) FROM BENCH.TRUE_DUPS")) == 600
    assert int(db.scalar("SELECT COUNT(*) FROM BENCH.GOLDEN_CLEAN")) == 36_600


def test_reset_under_ten_seconds(db):
    from reset import reset
    assert reset(db)["seconds"] < 10


# ------------------------------------------------------------------ 18.4 privilege floor

AGENT_WRITES = [
    "INSERT INTO GOLDEN.CUSTOMERS (GOLDEN_ID) VALUES ('X')",
    "UPDATE GOLDEN.CUSTOMERS SET CITY = 'x'",
    "DELETE FROM GOLDEN.CUSTOMERS",
    "MERGE INTO GOLDEN.CUSTOMERS g USING (SELECT 'X' AS K) s ON (g.GOLDEN_ID = s.K) WHEN MATCHED THEN UPDATE SET g.CITY = 'x'",
    "TRUNCATE TABLE GOLDEN.CUSTOMERS",
    "DROP TABLE GOLDEN.CUSTOMERS",
    "CREATE TABLE GOLDEN.EVIL (A DECIMAL(9,0))",
    "UPDATE GOLDEN_V.CUSTOMERS SET CITY = 'x'",
    "UPDATE SOURCE_A.CUSTOMERS SET CITY = 'x'",
    "DELETE FROM SOURCE_B.CLIENTS",
    "CREATE SCHEMA AGENT_SCHEMA",
    "DROP SCHEMA DRYDOCK CASCADE",
    "SELECT COUNT(*) FROM BENCH.TRUE_PAIRS",
    "SELECT COUNT(*) FROM DRYDOCK.BRANCHES",
    "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS",
]


@pytest.mark.parametrize("sql", AGENT_WRITES)
def test_18_4_privilege_floor(sql):
    from drydock.db import Db, DbError
    agent = Db("agent")
    with pytest.raises(DbError):
        agent.run(sql)


def test_18_4_agent_can_read_its_surfaces():
    from drydock.db import Db
    agent = Db("agent")
    assert int(agent.scalar("SELECT COUNT(*) FROM GOLDEN_V.CUSTOMERS")) > 0
    assert int(agent.scalar("SELECT COUNT(*) FROM SOURCE_A.CUSTOMERS")) == 36_600


def test_18_4_agent_read_path_survives_a_merge_swap(clean):
    """An object GRANT follows the renamed object (check V14), so after a swap a table-granted reader would
    lose the new table and gain the archive. The agent therefore reads through the GOLDEN_V view and holds
    nothing on GOLDEN. Proven end to end through a real merge: the agent reads the merged value through the
    view, and cannot read the archive the old table became."""
    from drydock import gate
    from drydock.db import Db, DbError
    db = clean
    key = some_keys(db, 1, offset=77)[0]
    bid = open_(db)
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'V14-SWAPPED' WHERE GOLDEN_ID = '{key}'")
    m = gate.request_merge(db, bid, 0.999, "V14 read-path proof")
    assert m["decision"] == "MERGED", m
    agent = Db("agent")
    assert agent.scalar(f"SELECT CITY FROM GOLDEN_V.CUSTOMERS WHERE GOLDEN_ID = '{key}'") == "V14-SWAPPED"
    with pytest.raises(DbError):
        agent.run(f"SELECT COUNT(*) FROM GOLDEN.CUSTOMERS__ARCH_{m['merge_id']}")


# ------------------------------------------------------------------ isolation: 18.1 / 18.2 / 18.14

def test_phase2_exit_update_in_branch_leaves_golden_byte_identical(clean):
    db = clean
    before = golden_fp(db)
    bid = open_(db)
    out = run(db, bid, "UPDATE GOLDEN.CUSTOMERS SET CITY = 'x'")
    assert out["rows_affected"] > 0
    assert golden_fp(db) == before


def test_18_1_isolation_after_many_arbitrary_statements(clean):
    db = clean
    before = golden_fp(db)
    bid = open_(db)
    stmts = [f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'c{i}' WHERE GOLDEN_ID IN (SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS "
             f"ORDER BY GOLDEN_ID LIMIT 5 OFFSET {i * 5})" for i in range(40)]
    stmts += ["DELETE FROM GOLDEN.CUSTOMERS WHERE POSTCODE IS NULL",
              "INSERT INTO GOLDEN.CUSTOMERS (GOLDEN_ID, FULL_NAME) VALUES ('G-NEW1', 'New Person')",
              "CREATE TABLE T1 AS SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS WHERE CITY LIKE 'c%'",
              "UPDATE GOLDEN.CUSTOMERS SET EMAIL = LOWER(EMAIL) WHERE GOLDEN_ID IN (SELECT GOLDEN_ID FROM T1)",
              "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS",
              "MERGE INTO GOLDEN.CUSTOMERS g USING (SELECT CUST_ID, EMAIL FROM SOURCE_A.CUSTOMERS) a "
              "ON (g.SOURCE_A_REF = a.CUST_ID) WHEN MATCHED THEN UPDATE SET g.EMAIL = a.EMAIL",
              "UPDATE GOLDEN.CUSTOMERS SET PHONE = NULL WHERE GOLDEN_ID = 'G-NEW1'",
              "DELETE FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID = 'G-NEW1'",
              "DROP TABLE T1",
              "SELECT MAX(GOLDEN_ID) FROM GOLDEN_V.CUSTOMERS"]
    assert len(stmts) == 50
    for s in stmts:
        run(db, bid, s)
    mat = db.scalar(f"SELECT BASE_FINGERPRINT FROM DRYDOCK.MATERIALISATIONS WHERE BRANCH_ID = '{bid}'")
    assert golden_fp(db) == before == mat


def test_18_2_discard_totality(clean):
    from drydock import branch
    db = clean
    before = golden_fp(db)
    bid = open_(db)
    run(db, bid, "UPDATE GOLDEN.CUSTOMERS SET CITY = 'x'")
    branch.discard_branch(db, bid, "test")
    assert int(db.scalar(f"SELECT COUNT(*) FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = '{bid}'")) == 0
    assert int(db.scalar(f"SELECT COUNT(*) FROM DRYDOCK.BASE_HASHES WHERE BRANCH_ID = '{bid}'")) == 0
    assert golden_fp(db) == before


def test_18_14_reserved_names_blocked(clean):
    from drydock import branch
    db = clean
    bid = open_(db)
    for t in ("CUSTOMERS__ARCH_1", "CUSTOMERS__NEW_1"):
        out = branch.run_in_branch(db, bid, f"SELECT COUNT(*) FROM GOLDEN.{t}", "reserved")
        assert out["status"] == "BLOCKED" and out["code"] == "RESERVED_NAME"


# ------------------------------------------------------------------ diff: 18.3 / 18.10 / 18.13

def test_18_3_diff_exactness_137(clean):
    from drydock import diff
    db = clean
    keys = some_keys(db, 137, offset=1000)
    bid = open_(db)
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = CITY || '*' WHERE GOLDEN_ID IN ({in_list(keys)})")
    d = diff.compute(db, bid)["tables"][0]
    assert (d["changed"], d["added"], d["deleted"]) == (137, 0, 0)
    assert d["columns_touched"] == {"CITY": 137}
    got = {r[0] for r in db.rows(f"SELECT KEY_VALUE FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = '{bid}'")}
    assert got == set(keys)


def test_18_10_arbitrary_sql_runs_and_is_observed(clean):
    from drydock import diff
    db = clean
    bid = open_(db)
    k = some_keys(db, 20, offset=2000)
    # Exasol rejects `WITH ... UPDATE` and any subquery in SET, so the supported equivalents are used: a CTE
    # inside the IN-subquery, and MERGE. Still five different statement shapes, all agent-authored.
    run(db, bid, "UPDATE GOLDEN.CUSTOMERS SET CITY = 'CTE' WHERE GOLDEN_ID IN "
                 "(WITH x AS (SELECT GOLDEN_ID FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID IN (" + in_list(k[:5]) + ")) "
                 "SELECT GOLDEN_ID FROM x)")
    run(db, bid, "MERGE INTO GOLDEN.CUSTOMERS g USING SOURCE_A.CUSTOMERS a ON (a.CUST_ID = g.SOURCE_A_REF "
                 "AND g.GOLDEN_ID IN (" + in_list(k[5:10]) + ")) "
                 "WHEN MATCHED THEN UPDATE SET g.EMAIL = LOWER(a.EMAIL) || '.corr'")
    run(db, bid, "CREATE TABLE RANKED AS SELECT GOLDEN_ID, ROW_NUMBER() OVER (ORDER BY GOLDEN_ID) AS RN "
                 "FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID IN (" + in_list(k[10:15]) + ")")
    run(db, bid, "MERGE INTO GOLDEN.CUSTOMERS g USING RANKED r ON (g.GOLDEN_ID = r.GOLDEN_ID) "
                 "WHEN MATCHED THEN UPDATE SET g.PHONE = 'W' || TO_CHAR(r.RN)")
    run(db, bid, "MERGE INTO GOLDEN.CUSTOMERS g USING (SELECT a.CUST_ID, b.TOWN FROM SOURCE_A.CUSTOMERS a "
                 "JOIN SOURCE_B.CLIENTS b ON LOWER(a.EMAIL) = LOWER(b.EMAIL_ADDR)) s ON (g.SOURCE_A_REF = s.CUST_ID "
                 "AND g.GOLDEN_ID IN (" + in_list(k[15:20]) + ")) WHEN MATCHED THEN UPDATE SET g.POSTCODE = 'MRG'")
    d = diff.compute(db, bid)["tables"][0]
    touched = d["columns_touched"]
    assert touched.get("CITY") == 5 and touched.get("EMAIL") == 5 and touched.get("PHONE") == 5
    assert d["changed"] >= 15


def test_18_13_keyless_degradation(clean):
    from drydock import branch, diff
    db = clean
    db.run("CREATE OR REPLACE TABLE GOLDEN.DUPKEY (K VARCHAR(10), V VARCHAR(10))")
    db.run("INSERT INTO GOLDEN.DUPKEY VALUES ('a', '1'), ('a', '2'), ('b', '3')")
    db.run("INSERT INTO DRYDOCK.TABLE_KEYS (SCHEMA_NAME, TABLE_NAME, KEY_COLUMN) VALUES ('GOLDEN', 'DUPKEY', 'K')")
    try:
        bid = open_(db)
        run(db, bid, "UPDATE GOLDEN.DUPKEY SET V = '9' WHERE V = '3'")
        d = diff.compute(db, bid)["tables"][0]
        assert d["mode"] == "KEYLESS"
        assert d["changed"] == 0 and d["added"] == 1 and d["deleted"] == 1
        assert d["rows"] == []  # no row cards, no row-level approval in keyless mode
        branch.discard_branch(db, bid, "t")
    finally:
        db.run("DELETE FROM DRYDOCK.TABLE_KEYS WHERE TABLE_NAME = 'DUPKEY'")
        db.run("DROP TABLE GOLDEN.DUPKEY")


# ------------------------------------------------------------------ merge: 18.12 / 18.9 / 18.8 / 18.7 / 18.6

def _pending_847(db):
    from drydock import gate
    keys = some_keys(db, 847, offset=3000)
    bid = open_(db)
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = CITY || ' (reviewed)' WHERE GOLDEN_ID IN ({in_list(keys)})")
    r = gate.request_merge(db, bid, 0.0, "invariant: force PENDING via risk")
    assert r["decision"] == "PENDING", r
    return bid, keys, r["merge_id"]


def test_a_branch_awaiting_a_person_never_expires_and_a_discard_closes_its_request(clean):
    """A branch waiting for review is not idle, so the TTL sweeper must never expire it; and a merge
    request cannot outlive its branch."""
    from drydock import branch, merge
    db = clean
    bid, _keys, mid = _pending_847(db)
    db.run(f"UPDATE DRYDOCK.BRANCHES SET TTL_SECONDS = 0 WHERE BRANCH_ID = '{bid}'")
    assert bid not in branch.sweep_expired(db)
    assert int(db.scalar(f"SELECT COUNT(*) FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = '{bid}'")) == 1
    branch.discard_branch(db, bid, "reviewer discarded")
    assert db.scalar(f"SELECT DECISION FROM DRYDOCK.MERGES WHERE MERGE_ID = {mid}") == "BLOCKED"
    with pytest.raises(ValueError):
        merge.apply(db, mid)


def test_phase4_exit_and_18_12_row_level_exclusion_per_row(clean):
    from drydock import diff, merge
    db = clean
    base_fp = golden_fp(db)
    original = dict(db.rows("SELECT GOLDEN_ID, CITY FROM GOLDEN.CUSTOMERS"))
    bid, keys, mid = _pending_847(db)
    branch_city = dict(db.rows(f"SELECT GOLDEN_ID, CITY FROM {bid}.CUSTOMERS WHERE GOLDEN_ID IN ({in_list(keys)})"))
    refused = keys[:3]
    diff.set_approval(db, bid, refused, False)
    out = merge.apply(db, mid, resolved_by="invariant")
    assert out["decision"] == "MERGED"
    assert (out["rows_applied"], out["rows_excluded"]) == (844, 3)
    now = dict(db.rows("SELECT GOLDEN_ID, CITY FROM GOLDEN.CUSTOMERS"))
    assert len(now) == len(original)
    for k in refused:                                   # asserted PER ROW, not by count
        assert now[k] == original[k], k
    for k in keys[3:]:
        assert now[k] == branch_city[k] and now[k] != original[k], k
    for k in set(original) - set(keys):
        assert now[k] == original[k], k
    # unmerge -> byte-identical to the pre-merge image (18.9)
    u = merge.unmerge(db, mid)
    assert u["fingerprint_match"] is True
    assert golden_fp(db) == base_fp
    pre = db.scalar(f"SELECT PRE_MERGE_FINGERPRINT FROM DRYDOCK.MERGES WHERE MERGE_ID = {mid}")
    base = db.scalar(f"SELECT BASE_FINGERPRINT FROM DRYDOCK.MATERIALISATIONS WHERE BRANCH_ID = '{bid}'")
    assert golden_fp(db) == pre == base


def test_18_9_unmerge_is_lifo(clean):
    from drydock import gate, merge
    db = clean
    b1 = open_(db)
    run(db, b1, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'one' WHERE GOLDEN_ID IN ({in_list(some_keys(db, 3))})")
    m1 = gate.request_merge(db, b1, 0.999, "first")
    b2 = open_(db)
    run(db, b2, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'two' WHERE GOLDEN_ID IN ({in_list(some_keys(db, 3, 10))})")
    m2 = gate.request_merge(db, b2, 0.999, "second")
    assert m1["decision"] == m2["decision"] == "MERGED"
    refused = merge.unmerge(db, m1["merge_id"])
    assert refused["ok"] is False and refused["code"] == "UNMERGE_SUPERSEDED"
    assert merge.unmerge(db, m2["merge_id"])["fingerprint_match"] is True
    assert merge.unmerge(db, m1["merge_id"])["fingerprint_match"] is True


def test_18_8_idempotency(clean):
    from drydock import gate
    db = clean
    bid = open_(db)
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'idem' WHERE GOLDEN_ID IN ({in_list(some_keys(db, 5))})")
    a = gate.request_merge(db, bid, 0.999, "once", idem_key="idem-test-1")
    fp_after_first = golden_fp(db)
    b = gate.request_merge(db, bid, 0.999, "twice", idem_key="idem-test-1")
    assert a["replayed"] is False and b["replayed"] is True
    assert b["merge_id"] == a["merge_id"]
    assert golden_fp(db) == fp_after_first
    assert int(db.scalar("SELECT COUNT(*) FROM GOLDEN.CUSTOMERS WHERE CITY = 'idem'")) == 5


def test_18_7_base_drift_on_a_touched_key_blocks(clean):
    from drydock import gate
    db = clean
    k = some_keys(db, 10, offset=5000)
    bid = open_(db)
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'branch' WHERE GOLDEN_ID IN ({in_list(k)})")
    db.run(f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'out-of-band' WHERE GOLDEN_ID = '{k[0]}'")  # simulated external writer
    r = gate.request_merge(db, bid, 1.0, "drift test")
    assert r["decision"] == "BLOCKED" and "BASE_DRIFT" in r["reason"]


def test_18_7_added_drift_on_an_untouched_key_does_not_block(clean):
    """Drift is key-scoped, so independent match-class branches can all merge."""
    from drydock import gate
    db = clean
    k = some_keys(db, 10, offset=6000)
    other = some_keys(db, 1, offset=9000)[0]
    bid = open_(db)
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'branch' WHERE GOLDEN_ID IN ({in_list(k)})")
    db.run(f"UPDATE GOLDEN.CUSTOMERS SET CITY = 'elsewhere' WHERE GOLDEN_ID = '{other}'")
    r = gate.request_merge(db, bid, 0.999, "no overlap")
    assert r["decision"] == "MERGED", r
    assert db.scalar(f"SELECT CITY FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID = '{other}'") == "elsewhere"


def test_18_6_hard_delete_block_unbuyable_at_tier3(clean):
    from drydock import gate
    db = clean
    db.run("INSERT INTO DRYDOCK.RUNS (RUN_ID, TIER, GATE_ENABLED, MIN_CONFIDENCE) VALUES ('inv-t3', 3, TRUE, 0)")
    bid = open_(db, run_id="inv-t3")
    run(db, bid, "DELETE FROM GOLDEN.CUSTOMERS WHERE MOD(CAST(SUBSTR(GOLDEN_ID, 4) AS DECIMAL(18,0)), 5) < 2")
    r = gate.request_merge(db, bid, 1.0, "delete ~40%")
    assert r["decision"] == "BLOCKED" and "DELETE_PCT_EXCEEDED" in r["reason"]


def test_18_5_min_confidence_monotonic_through_the_database(clean):
    from drydock import gate
    db = clean
    db.run("INSERT INTO DRYDOCK.RUNS (RUN_ID, TIER, GATE_ENABLED, MIN_CONFIDENCE) VALUES ('inv-mono', 2, TRUE, 0.6)")
    seen = [0.6]
    for code in ["WRONG_MATCH"] * 5 + ["POLICY"] * 5:
        gate.adapt_policy(db, "inv-mono", code)
        seen.append(float(db.scalar("SELECT MIN_CONFIDENCE FROM DRYDOCK.RUNS WHERE RUN_ID = 'inv-mono'")))
    assert all(b >= a for a, b in zip(seen, seen[1:])), seen


def test_18_17_tier_zero_never_merges(clean):
    from drydock import gate
    db = clean
    db.run("INSERT INTO DRYDOCK.RUNS (RUN_ID, TIER, GATE_ENABLED, MIN_CONFIDENCE) VALUES ('inv-t0', 0, TRUE, 0)")
    bid = open_(db, run_id="inv-t0")
    run(db, bid, f"UPDATE GOLDEN.CUSTOMERS SET CITY = 't0' WHERE GOLDEN_ID IN ({in_list(some_keys(db, 1))})")
    assert gate.request_merge(db, bid, 1.0, "tier 0")["decision"] != "MERGED"


# ------------------------------------------------------------------ 18.11 decoy protection (needs the adjudicator)

@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="18.11 needs the Gemini adjudicator (GEMINI_API_KEY)")
def test_18_11_no_split_decoy_reaches_golden_without_a_human(clean):
    """Gate on, nobody reviews: every decoy in GOLDEN must be one the panel merged UNANIMOUSLY.
    No decoy the matchers disagreed on can arrive without a person. Every decoy is checked individually."""
    from agent import playbook
    db = clean
    playbook.run_scripted(db, "inv-decoys", tier=2, gate_enabled=True)
    decoys = db.rows("SELECT t.A_ID, t.B_ID, c.PANEL_RESULT, g.GOLDEN_ID FROM BENCH.TRUE_PAIRS t "
                     "LEFT JOIN DRYDOCK.CANDIDATES c ON c.RUN_ID = 'inv-decoys' AND c.A_ID = t.A_ID AND c.B_ID = t.B_ID "
                     "LEFT JOIN GOLDEN.CUSTOMERS g ON g.SOURCE_A_REF = t.A_ID AND g.SOURCE_B_REF = t.B_ID "
                     "WHERE t.IS_DECOY")
    assert len(decoys) == 400
    for a_id, b_id, panel, in_golden in decoys:
        if in_golden is not None:
            assert panel == "UNANIMOUS_MERGE", (a_id, b_id, panel)


# ------------------------------------------------------------------ 18.15 (Panel replaces Regatta) / 18.16

def test_18_15_panel_result_is_exactly_the_vote_tally(clean):
    """PANEL_RESULT is UNANIMOUS_* iff all three
    votes agree, SPLIT otherwise — for every scored candidate, checked in the database."""
    from agent import playbook
    from drydock import er
    db = clean
    playbook.start_run_row(db, "inv-panel", tier=2, gate_enabled=True, seed=1, mode="scripted")
    er.declare_mapping(db, "inv-panel", playbook.MAPPING, "invariant")
    for kind, rule, sql in playbook.BLOCKING:
        er.stage_candidates(db, "inv-panel", rule, sql, kind)
    er.run_matching(db, "inv-panel", adjudicate=False)
    bad = int(db.scalar(
        "SELECT COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = 'inv-panel' AND NOT ("
        "(PANEL_RESULT = 'UNANIMOUS_MERGE' AND VOTE_DETERM = 'MERGE' AND VOTE_PROB = 'MERGE' AND VOTE_SKEPTIC = 'MERGE') OR "
        "(PANEL_RESULT = 'UNANIMOUS_REJECT' AND VOTE_DETERM = 'REJECT' AND VOTE_PROB = 'REJECT' AND VOTE_SKEPTIC = 'REJECT') OR "
        "(PANEL_RESULT = 'SPLIT' AND NOT (VOTE_DETERM = VOTE_PROB AND VOTE_PROB = VOTE_SKEPTIC)))"))
    assert bad == 0
    assert int(db.scalar("SELECT COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = 'inv-panel'")) > 0
    flagged_sizes = [int(r[0]) for r in db.rows(
        "SELECT COMPONENT_SIZE FROM DRYDOCK.CANDIDATES WHERE RUN_ID = 'inv-panel' AND VERDICT = 'FLAG'")]
    assert all(s > 2 for s in flagged_sizes)


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="18.16 needs the Gemini adjudicator (GEMINI_API_KEY)")
def test_18_16_precedent_changes_the_recorded_verdict(clean):
    """Same comparison, 0 precedents vs 2 precedents -> PRECEDENTS_USED populated on the second call,
    and the recorded adjudications differ in their precedent input."""
    from drydock import adjudicate, er, precedent
    db = clean
    db.run("DELETE FROM DRYDOCK.PRECEDENTS")
    sig = {"email": 0, "phone": 1000, "name": 1000, "addr": 1000, "dob": 0, "dob_gap_years": 28,
           "suffix_conflict": 0, "business_conflict": 0, "given_initial_conflict": 0, "same_family": 1}
    item = {"pair_id": 900001, "kind": "AB", "summary": er.compare_summary(sig)}
    first = adjudicate.adjudicate(db, "inv-prec", [item])[900001]
    assert first["precedents"] == []
    for _ in range(2):
        precedent.record(db, defect_class="FUZZY_NAME", context=er.compare_summary(sig), verdict="REJECTED",
                         reject_code="WRONG_MATCH", note="father and son - never merge on shared address alone")
    second = adjudicate.adjudicate(db, "inv-prec", [item])[900001]
    assert len(second["precedents"]) == 2
    used = [r[0] for r in db.rows("SELECT PRECEDENTS_USED FROM DRYDOCK.ADJUDICATIONS WHERE PAIR_ID = 900001 ORDER BY ADJ_ID")]
    assert used[0] == "[]" and used[1] != "[]"
