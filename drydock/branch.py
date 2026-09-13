"""Branch lifecycle: open, copy-on-write materialisation, run_in_branch, discard, TTL.

Every statement the agent submits is retargeted (retarget.py) and then runs as
DRYDOCK_SVC against the branch schema. Nothing here writes GOLDEN.
"""

from __future__ import annotations

import datetime as dt
import secrets
import time

from . import events, idem
from .config import GOLDEN, SETTINGS, require_verified
from .db import Db, DbError, ident, lit, qname
from .hashing import columns, fingerprint_of_query, key_text, row_hash_expr, signature
from .retarget import Blocked, BranchState, retarget


class BranchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


def _branch_row(db: Db, branch_id: str) -> dict:
    rows = db.dicts(f"SELECT * FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(ident(branch_id))}")
    if not rows:
        raise BranchError("NO_SUCH_BRANCH", branch_id)
    return rows[0]


def open_branch(db: Db, purpose: str, run_id: str | None = None, defect_class: str | None = None,
                opened_by: str = "agent", gate_enabled: bool = True) -> dict:
    require_verified("V11")
    n_open = int(db.scalar("SELECT COUNT(*) FROM DRYDOCK.BRANCHES WHERE STATUS = 'OPEN'"
                           + (f" AND RUN_ID = {lit(run_id)}" if run_id else "")))
    if n_open >= SETTINGS.max_open_branches:
        raise BranchError("TOO_MANY_BRANCHES", f"{n_open} open (max {SETTINGS.max_open_branches})")
    bid = "BR_" + secrets.token_hex(4).upper()
    db.run(f"CREATE SCHEMA {bid}")
    db.run("INSERT INTO DRYDOCK.BRANCHES (BRANCH_ID, RUN_ID, OPENED_BY, STATUS, PURPOSE, DEFECT_CLASS, "
           f"GATE_ENABLED, TTL_SECONDS) VALUES ({lit(bid)}, {lit(run_id)}, {lit(opened_by)}, 'OPEN', "
           f"{lit(purpose[:2000])}, {lit(defect_class)}, {lit(gate_enabled)}, {SETTINGS.branch_ttl_seconds})")
    events.emit("branch.opened", run_id, bid, defect_class=defect_class or "", purpose=purpose, schema=bid)
    return {"branch_id": bid, "schema": bid}


def state(db: Db, branch_id: str) -> BranchState:
    bid = ident(branch_id)
    mat = {r[0] for r in db.rows(f"SELECT SOURCE_TABLE FROM DRYDOCK.MATERIALISATIONS WHERE BRANCH_ID = {lit(bid)}")}
    tables = {r[0] for r in db.rows(f"SELECT TABLE_NAME FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = {lit(bid)}")}
    return BranchState(bid, mat, tables - mat)


def materialise(db: Db, branch_id: str, table: str, triggered_by: str = "", run_id: str | None = None) -> dict:
    """Copy GOLDEN.<table> into the branch on first write. Records the
    per-row base hashes of the copy (the snapshot the diff classifies against)
    and the base fingerprint."""
    require_verified("V3", "V6")
    bid, t = ident(branch_id), ident(table)
    if db.scalar(f"SELECT COUNT(*) FROM DRYDOCK.MATERIALISATIONS WHERE BRANCH_ID = {lit(bid)} "
                 f"AND SOURCE_TABLE = {lit(t)}"):
        return {"already": True}
    keys = db.dicts(f"SELECT KEY_COLUMN, IS_UNIQUE, VERIFIED_AT FROM DRYDOCK.TABLE_KEYS "
                    f"WHERE SCHEMA_NAME = {lit(GOLDEN)} AND TABLE_NAME = {lit(t)}")
    if not keys:
        raise BranchError("NO_DIFF_KEY_REGISTERED", f"{GOLDEN}.{t} has no row in DRYDOCK.TABLE_KEYS")
    key = keys[0]["KEY_COLUMN"]
    base_rows = int(db.scalar(f"SELECT COUNT(*) FROM {qname(GOLDEN, t)}"))
    if base_rows > SETTINGS.materialisation_ceiling:
        raise BranchError("TABLE_TOO_LARGE_TO_BRANCH", f"{base_rows} rows > {SETTINGS.materialisation_ceiling}")
    key_mode = "KEYLESS"
    if key:
        uniq = db.scalar(f"SELECT CASE WHEN COUNT(*) = COUNT(DISTINCT {ident(key)}) AND COUNT({ident(key)}) = COUNT(*) "
                         f"THEN TRUE ELSE FALSE END FROM {qname(GOLDEN, t)}")
        db.run(f"UPDATE DRYDOCK.TABLE_KEYS SET IS_UNIQUE = {lit(bool(uniq))}, VERIFIED_AT = CURRENT_TIMESTAMP "
               f"WHERE SCHEMA_NAME = {lit(GOLDEN)} AND TABLE_NAME = {lit(t)}")
        key_mode = "KEYED" if uniq else "KEYLESS"
        if not uniq:
            events.emit("agent.note", run_id, bid, text=f"{GOLDEN}.{t}.{key} is not unique: diff degrades to KEYLESS "
                        "(added/deleted only, no row-level approval)", phase="materialise")

    _, copy_ms = db.timed(f"CREATE TABLE {bid}.{t} AS SELECT * FROM {qname(GOLDEN, t)}")
    cols = columns(db, bid, t)
    key_expr = key_text(cols, key) if key_mode == "KEYED" else "CAST(NULL AS VARCHAR(200))"
    db.run(f"INSERT INTO DRYDOCK.BASE_HASHES (BRANCH_ID, TARGET_TABLE, KEY_VALUE, H) "
           f"SELECT {lit(bid)}, {lit(t)}, {key_expr}, {row_hash_expr(cols)} FROM {bid}.{t}")
    fp = fingerprint_of_query(db, f"SELECT H FROM DRYDOCK.BASE_HASHES WHERE BRANCH_ID = {lit(bid)} "
                                  f"AND TARGET_TABLE = {lit(t)}")
    db.run("INSERT INTO DRYDOCK.MATERIALISATIONS (BRANCH_ID, SOURCE_SCHEMA, SOURCE_TABLE, BASE_ROWCOUNT, "
           "BASE_FINGERPRINT, COLUMN_SIGNATURE, KEY_MODE, COPY_MS, TRIGGERED_BY) VALUES ("
           f"{lit(bid)}, {lit(GOLDEN)}, {lit(t)}, {base_rows}, {lit(fp.hex)}, {lit(signature(cols))}, "
           f"{lit(key_mode)}, {int(copy_ms)}, {lit(triggered_by[:100000])})")
    events.emit("branch.materialised", run_id, bid, table=f"{GOLDEN}.{t}", rows=base_rows,
                copy_ms=round(copy_ms, 1), base_fingerprint=fp.hex, key_mode=key_mode)
    return {"table": t, "rows": base_rows, "copy_ms": copy_ms, "fingerprint": fp.hex, "key_mode": key_mode}


def run_in_branch(db: Db, branch_id: str, sql: str, rationale: str = "", idem_key: str | None = None,
                  run_id: str | None = None) -> dict:
    """Any SQL: retargeted, checked against the hard blocks, executed as DRYDOCK_SVC."""
    bid = ident(branch_id)
    key = idem_key or idem.key_for(run_id, "run_in_branch", {"branch_id": bid, "sql": sql})
    return idem.once(db, key, "run_in_branch", lambda: _run(db, bid, sql, rationale, key, run_id))


def _log_op(db: Db, bid: str, key: str, sql: str, retargeted: str | None, reads, writes, rows, status,
            error, rationale, ms) -> int:
    seq = int(db.scalar(f"SELECT COALESCE(MAX(SEQ), 0) + 1 FROM DRYDOCK.BRANCH_OPS WHERE BRANCH_ID = {lit(bid)}"))
    db.run("INSERT INTO DRYDOCK.BRANCH_OPS (BRANCH_ID, SEQ, IDEM_KEY, ORIGINAL_SQL, RETARGETED_SQL, TABLES_READ, "
           "TABLES_WRITTEN, ROWS_AFFECTED, STATUS, ERROR_TEXT, RATIONALE, EXEC_MS) VALUES ("
           f"{lit(bid)}, {seq}, {lit(key)}, {lit(sql[:100000])}, {lit((retargeted or '')[:100000])}, "
           f"{lit(','.join(reads))}, {lit(','.join(writes))}, {lit(rows)}, {lit(status)}, "
           f"{lit((error or '')[:4000] or None)}, {lit(rationale[:4000])}, {int(ms)})")
    return int(db.scalar(f"SELECT MAX(OP_ID) FROM DRYDOCK.BRANCH_OPS WHERE BRANCH_ID = {lit(bid)} AND SEQ = {seq}"))


def _run(db: Db, bid: str, sql: str, rationale: str, key: str, run_id: str | None) -> dict:
    br = _branch_row(db, bid)
    run_id = run_id or br["RUN_ID"]
    if br["STATUS"] != "OPEN":
        raise BranchError("BRANCH_NOT_OPEN", f"{bid} is {br['STATUS']}")
    try:
        r = retarget(sql, state(db, bid))
    except Blocked as b:
        op_id = _log_op(db, bid, key, sql, None, [], [], None, "BLOCKED", f"{b.code}: {b.message}", rationale, 0)
        events.emit("branch.blocked", run_id, bid, code=b.code, message=b.message, sql=sql)
        return {"op_id": op_id, "status": "BLOCKED", "code": b.code, "error": b.message,
                "rows_affected": 0, "tables_written": [], "materialised": []}

    mats = []
    for t in r.to_materialise:
        mats.append(materialise(db, bid, t, triggered_by=sql, run_id=run_id))
    t0 = time.perf_counter()
    try:
        rows = db.run(r.sql)
        status, err = "OK", None
    except DbError as e:
        rows, status, err = 0, "ERROR", f"[{e.code}] {e.message}"
    ms = (time.perf_counter() - t0) * 1000
    op_id = _log_op(db, bid, key, sql, r.sql, r.tables_read, r.tables_written, rows, status, err, rationale, ms)
    events.emit("branch.op", run_id, bid, op_id=op_id, status=status, rows_affected=rows,
                tables_written=r.tables_written, rationale=rationale, sql=sql, error=err)
    return {"op_id": op_id, "status": status, "rows_affected": rows, "tables_written": r.tables_written,
            "materialised": [m.get("table") for m in mats if not m.get("already")],
            "retargeted_sql": r.sql, "error": err, "exec_ms": round(ms, 1)}


def acknowledge_errors(db: Db, branch_id: str) -> int:
    return db.run(f"UPDATE DRYDOCK.BRANCH_OPS SET ACKNOWLEDGED = TRUE WHERE BRANCH_ID = {lit(ident(branch_id))} "
                  "AND STATUS = 'ERROR'")


def discard_branch(db: Db, branch_id: str, reason: str, *, expired: bool = False) -> dict:
    """DROP SCHEMA ... CASCADE: the entire undo for unmerged work."""
    require_verified("V11")
    bid = ident(branch_id)
    br = _branch_row(db, bid)
    if br["STATUS"] not in ("OPEN", "BLOCKED"):
        return {"ok": True, "already": br["STATUS"]}
    _, drop_ms = db.timed(f"DROP SCHEMA IF EXISTS {bid} CASCADE")
    status = "EXPIRED" if expired else "DISCARDED"
    db.run(f"UPDATE DRYDOCK.BRANCHES SET STATUS = {lit(status)}, CLOSED_AT = CURRENT_TIMESTAMP, "
           f"NOTES = {lit(reason[:2000])} WHERE BRANCH_ID = {lit(bid)}")
    # A merge request cannot outlive its branch: close it, so the gate never offers to apply a branch that is gone.
    db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'BLOCKED', BLOCK_REASON = {lit(('BRANCH_' + status)[:512])} "
           f"WHERE BRANCH_ID = {lit(bid)} AND DECISION = 'PENDING'")
    _forget(db, bid)
    if expired:
        age = (dt.datetime.now() - br["OPENED_AT"]).total_seconds() if br.get("OPENED_AT") else 0.0
        events.emit("branch.expired", br["RUN_ID"], bid, age_seconds=round(age, 1))
    else:
        events.emit("branch.discarded", br["RUN_ID"], bid, reason=reason, drop_ms=round(drop_ms, 1))
    return {"ok": True, "status": status, "drop_ms": drop_ms}


def _forget(db: Db, bid: str) -> None:
    for t in ("BASE_HASHES", "DIFF_ROWS"):
        db.run(f"DELETE FROM DRYDOCK.{t} WHERE BRANCH_ID = {lit(bid)}")


def sweep_expired(db: Db) -> list[str]:
    """Expire idle branches. Called by the orchestrator every 30 s.
    A branch whose merge is waiting for a reviewer is not idle, so it never expires."""
    rows = db.rows("SELECT b.BRANCH_ID FROM DRYDOCK.BRANCHES b WHERE b.STATUS = 'OPEN' "
                   "AND SECONDS_BETWEEN(CURRENT_TIMESTAMP, b.OPENED_AT) > b.TTL_SECONDS "
                   "AND NOT EXISTS (SELECT 1 FROM DRYDOCK.MERGES m WHERE m.BRANCH_ID = b.BRANCH_ID "
                   "AND m.DECISION = 'PENDING')")
    done = []
    for (bid,) in rows:
        discard_branch(db, bid, "TTL expired", expired=True)
        done.append(bid)
    return done


def branch_log(db: Db, branch_id: str | None = None, run_id: str | None = None, limit: int = 50) -> dict:
    bid = lit(ident(branch_id)) if branch_id else "NULL"
    ops = db.dicts("SELECT o.OP_ID, o.BRANCH_ID, o.SEQ, o.STATUS, o.ROWS_AFFECTED, o.TABLES_WRITTEN, "
                   "o.ERROR_TEXT, o.RATIONALE, o.EXEC_MS FROM DRYDOCK.BRANCH_OPS o JOIN DRYDOCK.BRANCHES b "
                   f"ON b.BRANCH_ID = o.BRANCH_ID WHERE ({bid} IS NULL OR o.BRANCH_ID = {bid}) "
                   f"AND ({lit(run_id)} IS NULL OR b.RUN_ID = {lit(run_id)}) ORDER BY o.OP_ID DESC LIMIT {int(limit)}")
    return {"ops": ops}
