"""Read-only exports and lineage, for the people who sign changes off.

    golden()        the reconciled list as it stands now
    table()         any browsable table (the schemas drydock/browse.py allows)
    audit()         every merge request: who asked, what the gate decided and why, who decided, when,
                    the fingerprints before and after, and any undo
    changes()       one branch's exact change set: every changed row, whether it is included, which
                    columns change, and the values now and in the branch (when the branch still exists)
    history()       one golden record: where each field came from, the source records behind it, the
                    match evidence that linked them, and every change that touched it

SELECT only. Every statement passes the dialect firewall in Db.execute.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Iterable, Iterator
from decimal import Decimal

from . import browse, dataset
from .db import Db, DbError, ident, lit, qname

GOLDEN_T = "GOLDEN.CUSTOMERS"
CHANGE = {"ADDED": "Added", "CHANGED": "Changed", "DELETED": "Deleted"}


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, Decimal):
        return str(int(v)) if v == v.to_integral_value() else str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v)


def to_csv(header: list[str], rows: Iterable[Iterable]) -> Iterator[str]:
    """CSV text in chunks. A UTF-8 byte-order mark first, so Excel shows accented names correctly."""
    buf = io.StringIO()
    w = csv.writer(buf)
    buf.write("\ufeff")
    w.writerow(header)
    for i, r in enumerate(rows, 1):
        w.writerow([_cell(v) for v in r])
        if i % 2000 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


def _cols(db: Db, schema: str, table: str) -> list[str]:
    return [c for (c,) in db.rows(
        f"SELECT COLUMN_NAME FROM EXA_ALL_COLUMNS WHERE COLUMN_SCHEMA = {lit(schema)} AND COLUMN_TABLE = {lit(table)} "
        "ORDER BY COLUMN_ORDINAL_POSITION")]


def golden(db: Db) -> tuple[list[str], list[tuple]]:
    cols = _cols(db, "GOLDEN", "CUSTOMERS")
    return cols, db.rows(f"SELECT {', '.join(cols)} FROM {GOLDEN_T} ORDER BY GOLDEN_ID")


def table(db: Db, schema: str, name: str) -> tuple[list[str], list[tuple]]:
    s = browse._schema(schema)                  # the browser's allow-list: no arbitrary schema
    t = ident(name)
    cols = _cols(db, s, t)
    if not cols:
        raise ValueError(f"no such table: {s}.{t}")
    return cols, db.rows(f"SELECT {', '.join(cols)} FROM {qname(s, t)}")


AUDIT_COLUMNS = [
    ("MERGE_ID", "Change #"), ("RUN_ID", "Run"), ("DEFECT_CLASS", "Change type"), ("BRANCH_ID", "Branch"),
    ("REQUESTED_AT", "Requested at"), ("REQUESTED_BY", "Requested by"), ("TOTAL_CHANGED", "Rows changed"),
    ("CONFIDENCE", "Confidence"), ("RISK_SCORE", "Risk"), ("TIER_AT_TIME", "Tier"), ("DECISION", "Decision"),
    ("BLOCK_REASON", "Gate reason"), ("RATIONALE", "Rationale"), ("RESOLVED_AT", "Decided at"),
    ("RESOLVED_BY", "Decided by"), ("ROWS_APPLIED", "Rows applied"), ("ROWS_EXCLUDED", "Rows left out"),
    ("PRE_MERGE_FINGERPRINT", "Fingerprint before"), ("POST_MERGE_FINGERPRINT", "Fingerprint after"),
    ("REJECT_CODE", "Reject reason"), ("REJECT_NOTE", "Reject note"), ("UNMERGED_AT", "Undone at"),
    ("UNMERGE_FP_MATCH", "Undo verified by fingerprint"),
]


def audit(db: Db) -> tuple[list[str], list[tuple]]:
    sel = ", ".join(("b." if c in ("RUN_ID", "DEFECT_CLASS") else "m.") + c for c, _ in AUDIT_COLUMNS)
    rows = db.rows(f"SELECT {sel} FROM DRYDOCK.MERGES m LEFT JOIN DRYDOCK.BRANCHES b ON b.BRANCH_ID = m.BRANCH_ID "
                   "ORDER BY m.MERGE_ID")
    return [label for _, label in AUDIT_COLUMNS], rows


def _column_order(db: Db, branch_id: str) -> list[str]:
    raw = db.scalar(f"SELECT COLUMN_ORDER FROM DRYDOCK.DIFFS WHERE BRANCH_ID = {lit(branch_id)} "
                    "AND TARGET_TABLE = 'CUSTOMERS' ORDER BY COMPUTED_AT DESC LIMIT 1")
    return json.loads(raw) if raw else []


def _mask_columns(mask, order: list[str]) -> list[str]:
    m = int(mask or 0)
    return [c for i, c in enumerate(order) if m >> i & 1]


def changes(db: Db, branch_id: str) -> tuple[list[str], list[tuple]]:
    bid = ident(branch_id)
    order = _column_order(db, bid)
    if not order:
        raise ValueError(f"branch {bid} has no diff yet")
    cols = _cols(db, "GOLDEN", "CUSTOMERS")
    live = bool(db.scalar(f"SELECT COUNT(*) FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = {lit(bid)} "
                          "AND TABLE_NAME = 'CUSTOMERS'"))
    # Aliased: a result set may not repeat a column name (the database driver refuses duplicates).
    now = ", ".join(f"g.{c} AS N_{i}" for i, c in enumerate(cols))
    head = ", ".join((f"h.{c}" if live else "NULL") + f" AS H_{i}" for i, c in enumerate(cols))
    join = f"LEFT JOIN {bid}.CUSTOMERS h ON h.GOLDEN_ID = d.KEY_VALUE " if live else ""
    sel = f"d.KEY_VALUE, d.CLASS, d.APPROVED, d.DEFAULT_REASON, d.COLS_MASK, {now}, {head}"
    rows = db.rows(f"SELECT {sel} FROM DRYDOCK.DIFF_ROWS d "  # sql-fragment: composed above, linted at Db.execute
                   f"LEFT JOIN {GOLDEN_T} g ON g.GOLDEN_ID = d.KEY_VALUE {join}"
                   f"WHERE d.BRANCH_ID = {lit(bid)} AND d.TARGET_TABLE = 'CUSTOMERS' ORDER BY d.KEY_VALUE")
    header = ["Record", "Change", "Included", "Why it starts where it does", "Columns that change"]
    header += [f"{c} (now)" for c in cols] + [f"{c} (in this change)" for c in cols]
    n = len(cols)
    out = [(k, CHANGE.get(cls, cls), ok, reason, ", ".join(_mask_columns(mask, order)), *r[5:5 + n],
            *(r[5 + n:] if live else [""] * n)) for r in rows for (k, cls, ok, reason, mask) in [r[:5]]]
    return header, out


def history(db: Db, golden_id: str) -> dict:
    gid = str(golden_id)[:64]
    cols = _cols(db, "GOLDEN", "CUSTOMERS")
    now = db.dicts(f"SELECT {', '.join(cols)} FROM {GOLDEN_T} WHERE GOLDEN_ID = {lit(gid)}")
    rec = {k: _cell(v) or None for k, v in now[0].items()} if now else None
    src = dataset.active(db)
    a_ids = set()
    if rec and rec.get("SOURCE_A_REF"):
        a_ids.add(rec["SOURCE_A_REF"])
    elif gid.startswith("G-"):
        a_ids.add(gid[2:])                              # every golden id is 'G-' || the source record's id
    if rec and rec.get("MERGED_A_REFS"):
        a_ids.update(x.strip() for x in rec["MERGED_A_REFS"].split(",") if x.strip())
    b_ids = {rec["SOURCE_B_REF"]} if rec and rec.get("SOURCE_B_REF") else set()
    sources = []
    for label, t, key, ids in (("System A", src.a, "CUST_ID", a_ids), ("System B", src.b, "CLIENT_REF", b_ids)):
        if not ids:
            continue
        for r in db.dicts(f"SELECT * FROM {t} WHERE {key} IN ({', '.join(lit(i) for i in sorted(ids))})"):
            sources.append({"system": label, "id": r[key], "record": {k: _cell(v) or None for k, v in r.items()}})
    evidence = []
    if a_ids:
        ids = ", ".join(lit(i) for i in sorted(a_ids | b_ids))
        for r in db.dicts("SELECT PAIR_ID, RUN_ID, KIND, A_ID, B_ID, SCORE_TOTAL, VOTE_DETERM, VOTE_PROB, VOTE_SKEPTIC, "
                          "PANEL_RESULT, AI_VERDICT, ADJUDICATOR, VERDICT, MATCH_CLASS, RATIONALE FROM DRYDOCK.CANDIDATES "
                          f"WHERE A_ID IN ({ids}) AND B_ID IN ({ids}) ORDER BY PAIR_ID"):
            evidence.append({k.lower(): _cell(v) or None for k, v in r.items()})
    changes_ = []
    for r in db.dicts("SELECT d.BRANCH_ID, d.CLASS, d.APPROVED, d.DEFAULT_REASON, d.COLS_MASK, b.DEFECT_CLASS, b.RUN_ID, "
                      "m.MERGE_ID, m.DECISION, m.RESOLVED_AT, m.RESOLVED_BY, m.UNMERGED_AT, m.REJECT_CODE "
                      "FROM DRYDOCK.DIFF_ROWS d JOIN DRYDOCK.BRANCHES b ON b.BRANCH_ID = d.BRANCH_ID "
                      "LEFT JOIN DRYDOCK.MERGES m ON m.BRANCH_ID = d.BRANCH_ID "
                      f"WHERE d.TARGET_TABLE = 'CUSTOMERS' AND d.KEY_VALUE = {lit(gid)} ORDER BY m.RESOLVED_AT, d.BRANCH_ID"):
        changes_.append({
            "branch": r["BRANCH_ID"], "change": CHANGE.get(r["CLASS"], r["CLASS"]), "kind": r["DEFECT_CLASS"],
            "run": r["RUN_ID"], "included": bool(r["APPROVED"]), "default_reason": r["DEFAULT_REASON"],
            "columns": _mask_columns(r["COLS_MASK"], _column_order(db, r["BRANCH_ID"])),
            "merge_id": int(r["MERGE_ID"]) if r["MERGE_ID"] is not None else None, "decision": r["DECISION"],
            "decided_at": _cell(r["RESOLVED_AT"]) or None, "decided_by": r["RESOLVED_BY"],
            "undone_at": _cell(r["UNMERGED_AT"]) or None, "reject_code": r["REJECT_CODE"]})
    survivorship = None
    if rec and rec.get("SURVIVORSHIP"):
        try:
            survivorship = json.loads(rec["SURVIVORSHIP"])
        except ValueError:
            survivorship = None
    return {"golden_id": gid, "record": rec, "exists": rec is not None, "sources": sources,
            "evidence": evidence, "changes": changes_, "survivorship": survivorship, "dataset": src.name}


def safe(fn, *args):
    """Turn a missing table into a clear message rather than a 500."""
    try:
        return fn(*args)
    except DbError as e:
        raise ValueError(f"Exasol said: {e.message}") from e
