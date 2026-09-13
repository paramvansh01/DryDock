"""Metrics: the matcher's numbers and the gate's number. Runs as DRYDOCK_SVC, which can
read BENCH; there is no MCP tool for it, so scores never reach the agent.

False positives are counted from the merged side, so a false merge that is neither a
true match nor a decoy is still counted; A<->A deduplication is scored against
BENCH.TRUE_DUPS.

  matcher (verdicts, before any reviewer):  precision / recall / F1
  gate    (what is actually in GOLDEN now): false merges that reached GOLDEN
In an A/B pair the control and treatment share the same verdicts, so the matcher numbers
are identical by construction and only the gate number can differ.
"""

from __future__ import annotations

import json

from drydock import events
from drydock.db import Db, lit


def _one(db: Db, sql: str) -> int:
    return int(db.scalar(sql) or 0)


def score(db: Db, run_id: str, emit: bool = True) -> dict:
    r = lit(run_id)
    merged_ab = _one(db, f"SELECT COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {r} AND KIND = 'AB' AND VERDICT = 'MERGE'")
    tp = _one(db, "SELECT COUNT(*) FROM DRYDOCK.CANDIDATES c JOIN BENCH.TRUE_PAIRS t ON t.A_ID = c.A_ID "
                  f"AND t.B_ID = c.B_ID AND t.IS_MATCH WHERE c.RUN_ID = {r} AND c.KIND = 'AB' AND c.VERDICT = 'MERGE'")
    decoy_merged = _one(db, "SELECT COUNT(*) FROM DRYDOCK.CANDIDATES c JOIN BENCH.TRUE_PAIRS t ON t.A_ID = c.A_ID "
                            f"AND t.B_ID = c.B_ID AND t.IS_DECOY WHERE c.RUN_ID = {r} AND c.KIND = 'AB' AND c.VERDICT = 'MERGE'")
    n_true = _one(db, "SELECT COUNT(*) FROM BENCH.TRUE_PAIRS WHERE IS_MATCH")
    fp, fn = merged_ab - tp, n_true - tp
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * rc / (p + rc) if p + rc else 0.0

    merged_aa = _one(db, f"SELECT COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {r} AND KIND = 'AA' AND VERDICT = 'MERGE'")
    tp_aa = _one(db, "SELECT COUNT(*) FROM DRYDOCK.CANDIDATES c JOIN BENCH.TRUE_DUPS d ON "
                     "((d.A_ID = c.A_ID AND d.A_DUP_ID = c.B_ID) OR (d.A_ID = c.B_ID AND d.A_DUP_ID = c.A_ID)) "
                     f"WHERE c.RUN_ID = {r} AND c.KIND = 'AA' AND c.VERDICT = 'MERGE'")
    n_dups = _one(db, "SELECT COUNT(*) FROM BENCH.TRUE_DUPS")
    flagged = _one(db, f"SELECT COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {r} AND VERDICT = 'FLAG'")

    # The gate's number: what is in GOLDEN right now.
    # A false merge is a row where an A record and a B record were joined into one identity but are not the
    # same person. A row inserted by NEW_CUSTOMERS (B record, no A counterpart) is not a merge, so it needs
    # both references. Whether such a row should have matched an A record is a recall failure, counted as
    # true_matches_added_as_new_customers below.
    fm_ab = _one(db, "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS g WHERE g.SOURCE_A_REF IS NOT NULL "
                     "AND g.SOURCE_B_REF IS NOT NULL AND NOT EXISTS ("
                     "SELECT 1 FROM BENCH.TRUE_PAIRS t WHERE t.IS_MATCH AND t.A_ID = g.SOURCE_A_REF AND t.B_ID = g.SOURCE_B_REF)")
    fm_decoy = _one(db, "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS g JOIN BENCH.TRUE_PAIRS t ON t.A_ID = g.SOURCE_A_REF "
                        "AND t.B_ID = g.SOURCE_B_REF AND t.IS_DECOY")
    fm_aa = _one(db, "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS g WHERE g.MERGED_A_REFS IS NOT NULL AND NOT EXISTS ("
                     "SELECT 1 FROM BENCH.TRUE_DUPS d WHERE (d.A_ID = g.SOURCE_A_REF AND d.A_DUP_ID = g.MERGED_A_REFS) "
                     "OR (d.A_DUP_ID = g.SOURCE_A_REF AND d.A_ID = g.MERGED_A_REFS))")
    enriched = _one(db, "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS "
                        "WHERE SOURCE_A_REF IS NOT NULL AND SOURCE_B_REF IS NOT NULL")
    added_new = _one(db, "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS "
                         "WHERE SOURCE_A_REF IS NULL AND SOURCE_B_REF IS NOT NULL")
    missed_as_new = _one(db, "SELECT COUNT(*) FROM GOLDEN.CUSTOMERS g JOIN BENCH.TRUE_PAIRS t ON t.B_ID = g.SOURCE_B_REF "
                             "AND t.IS_MATCH WHERE g.SOURCE_A_REF IS NULL")

    out = {
        "precision": round(p, 4), "recall": round(rc, 4), "f1": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn, "merged_pairs": merged_ab, "decoys_merged_by_matcher": decoy_merged,
        "dedup": {"merged": merged_aa, "tp": tp_aa, "fp": merged_aa - tp_aa, "fn": n_dups - tp_aa},
        "flagged_pairs": flagged,
        "false_merges_in_golden": fm_ab + fm_aa, "decoy_false_merges": fm_decoy,
        "golden_rows_enriched": enriched, "golden_rows_added_as_new_customers": added_new,
        "true_matches_added_as_new_customers": missed_as_new,
    }
    db.run(f"UPDATE DRYDOCK.RUNS SET METRICS = {lit(json.dumps(out))} WHERE RUN_ID = {r}")
    if emit:
        events.emit("score.updated", run_id, None, precision=out["precision"], recall=out["recall"], f1=out["f1"],
                    false_merges_in_golden=out["false_merges_in_golden"], decoy_false_merges=fm_decoy,
                    detail={k: v for k, v in out.items() if k not in ("precision", "recall", "f1")})
    return out
