"""Precedents: reviewer decisions stored in DRYDOCK.PRECEDENTS and retrieved in the database.

Before a split pair is adjudicated, the k most similar past decisions (same match class,
ranked by EDIT_DISTANCE between comparison summaries, in SQL) are passed to the adjudicator.
This is retrieval, not training: deleting a precedent removes its influence immediately.
"""

from __future__ import annotations

import json

from . import events
from .db import Db, lit


def record(db: Db, *, defect_class: str, context: str, verdict: str, before: str = "", after: str = "",
           reject_code: str | None = None, note: str | None = None, by: str = "human",
           merge_id: int | None = None, run_id: str | None = None) -> int:
    if verdict not in ("APPROVED", "REJECTED"):
        raise ValueError(verdict)
    db.run("INSERT INTO DRYDOCK.PRECEDENTS (DEFECT_CLASS, COLUMN_NAME, BEFORE_VALUE, AFTER_VALUE, CONTEXT, "
           "HUMAN_VERDICT, REJECT_CODE, HUMAN_NOTE, DECIDED_BY, SOURCE_MERGE) VALUES ("
           f"{lit(defect_class)}, 'PAIR', {lit(before[:500])}, {lit(after[:500])}, {lit(context[:1000])}, "
           f"{lit(verdict)}, {lit(reject_code)}, {lit((note or '')[:1000] or None)}, {lit(by)}, {lit(merge_id)})")
    pid = int(db.scalar("SELECT MAX(PRECEDENT_ID) FROM DRYDOCK.PRECEDENTS"))
    events.emit("precedent.recorded", run_id, None, precedent_id=pid, verdict=verdict,
                defect_class=defect_class, note=note)
    return pid


def retrieve(db: Db, context: str, defect_class: str = "PAIR", k: int = 3) -> list[dict]:
    """Top-k precedents by comparison-text similarity, computed in Exasol."""
    rows = db.dicts(
        "SELECT PRECEDENT_ID, HUMAN_VERDICT, HUMAN_NOTE, CONTEXT, DECIDED_BY, "
        f"EDIT_DISTANCE(CONTEXT, {lit(context[:1000])}) AS D FROM DRYDOCK.PRECEDENTS "
        f"WHERE DEFECT_CLASS = {lit(defect_class)} OR {lit(defect_class)} = 'PAIR' "
        f"ORDER BY D ASC, PRECEDENT_ID DESC LIMIT {int(k)}")
    if rows:
        ids = ", ".join(str(int(r["PRECEDENT_ID"])) for r in rows)
        db.run(f"UPDATE DRYDOCK.PRECEDENTS SET TIMES_CITED = TIMES_CITED + 1 WHERE PRECEDENT_ID IN ({ids})")
    return [{"id": int(r["PRECEDENT_ID"]), "verdict": r["HUMAN_VERDICT"], "note": r["HUMAN_NOTE"],
             "context": r["CONTEXT"], "by": r["DECIDED_BY"], "distance": int(r["D"])} for r in rows]


def record_for_merge(db: Db, merge_id: int, by: str = "human") -> list[int]:
    """On approval: every pair the reviewer explicitly touched becomes a precedent —
    checked -> APPROVED, unchecked -> REJECTED."""
    from .er import _records, compare_summary, run_tag  # noqa: F401

    m = db.dicts(f"SELECT m.BRANCH_ID, b.RUN_ID, b.DEFECT_CLASS FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b "
                 f"ON b.BRANCH_ID = m.BRANCH_ID WHERE m.MERGE_ID = {int(merge_id)}")
    if not m or not m[0]["RUN_ID"]:
        return []
    bid, run_id, cls = m[0]["BRANCH_ID"], m[0]["RUN_ID"], m[0]["DEFECT_CLASS"]
    touched = db.rows(f"SELECT KEY_VALUE, APPROVED FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} "
                      "AND DEFAULT_REASON = 'HUMAN'")
    out = []
    for key, approved in touched[:200]:
        a_id = str(key)[2:] if str(key).startswith("G-") else str(key)
        p = db.dicts(f"SELECT PAIR_ID, A_ID, B_ID, SIGNALS FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} "
                     f"AND (A_ID = {lit(a_id)} OR (KIND = 'AA' AND B_ID = {lit(a_id)})) AND VERDICT = 'MERGE' LIMIT 1")
        if not p:
            continue
        sig = json.loads(p[0]["SIGNALS"] or "{}")
        out.append(record(db, defect_class=cls, context=compare_summary(sig),
                          verdict="APPROVED" if approved else "REJECTED",
                          before=f"A {p[0]['A_ID']}", after=f"B {p[0]['B_ID']}",
                          reject_code=None if approved else "WRONG_MATCH", by=by, merge_id=merge_id, run_id=run_id))
    return out


def add_note(db: Db, precedent_id: int, note: str) -> None:
    db.run(f"UPDATE DRYDOCK.PRECEDENTS SET HUMAN_NOTE = {lit(note[:1000])} WHERE PRECEDENT_ID = {int(precedent_id)}")


def delete(db: Db, precedent_id: int) -> None:
    """The demo beat: delete a precedent row and watch the verdict flip back."""
    db.run(f"DELETE FROM DRYDOCK.PRECEDENTS WHERE PRECEDENT_ID = {int(precedent_id)}")
