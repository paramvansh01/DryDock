"""The single write path to GOLDEN. No other module writes GOLDEN.

apply(): stage-then-swap with row-level approval.
  1. STAGE   one CTAS into GOLDEN under a reserved name. Exasol cannot RENAME across
             schemas, so the branch table cannot simply be renamed in. Because a copy
             is made anyway, excluded rows cost only a WHERE:
               approved CHANGED/ADDED rows      <- branch
               rows the reviewer excluded       <- GOLDEN (current)
               rows the branch never touched    <- GOLDEN (current)
             An approved DELETED row is in none of the three.
  2. VERIFY  fingerprint(staged) must equal the expected post-state, computed
             independently: pre - base(approved CHANGED/DELETED) + head(approved CHANGED/ADDED).
  3. SWAP    two same-schema RENAMEs in one transaction (DDL is transactional on Exasol);
             SWAP_STAGE is recorded so recover() can finish an interrupted swap.
  4. RECORD the merge and close the branch.
unmerge(): two renames back, then fingerprint == PRE_MERGE_FINGERPRINT. LIFO per table.

The staging CTAS is generated from the catalogue; every statement passes lintguard.clean().
"""

from __future__ import annotations

import time

from . import events
from .config import GOLDEN, GOLDEN_V, require_answered, require_verified
from .db import Db, ident, lit, qname
from .hashing import ZERO, Fingerprint, columns, fingerprint_of_query, key_text, table_fingerprint
from .lintguard import DialectError, clean
from .locks import golden_write_lock


def _proven(title: str) -> bool:
    from .lintguard import dialect_lint
    return any(e.title.startswith(title) and e.status == "PROVEN" for e in dialect_lint.load_dialect())


def rename_sql(schema: str, old: str, new: str) -> str:
    if _proven("Same-schema RENAME (unqualified target)"):
        return clean(f"RENAME TABLE {qname(schema, old)} TO {ident(new)}", "merge.rename")
    if _proven("Same-schema RENAME (qualified target)"):
        return clean(f"RENAME TABLE {qname(schema, old)} TO {qname(schema, new)}", "merge.rename")
    raise DialectError("no PROVEN same-schema RENAME form in sql/DIALECT.md — run verify.py --only V5")


def refresh_read_views(db: Db) -> None:
    """The agent reads GOLDEN through GOLDEN_V views (sql/02_grants.sql). Re-issue
    after every swap so the view binds to the table now named GOLDEN.CUSTOMERS,
    whatever V14 says about view binding. Schema-level grant survives."""
    cols = ", ".join(c.name for c in columns(db, GOLDEN, "CUSTOMERS"))
    db.run(clean(f"CREATE OR REPLACE VIEW {GOLDEN_V}.CUSTOMERS AS SELECT {cols} FROM {GOLDEN}.CUSTOMERS",
                 "merge.view"))


def _diff_sel(bid: str, t: str, where: str, col: str) -> str:
    return (f"SELECT {col} AS H FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} "
            f"AND TARGET_TABLE = {lit(t)} AND {where}")


def expected_post(db: Db, bid: str, t: str, pre: Fingerprint) -> Fingerprint:
    minus = fingerprint_of_query(db, _diff_sel(bid, t, "APPROVED AND CLASS IN ('CHANGED', 'DELETED')", "BASE_HASH"))
    plus = fingerprint_of_query(db, _diff_sel(bid, t, "APPROVED AND CLASS IN ('CHANGED', 'ADDED')", "HEAD_HASH"))
    return pre - minus + plus


def staging_sql(db: Db, bid: str, t: str, mid: int, mode: str) -> str:
    new = f"{t}__NEW_{mid}"
    if mode != "KEYED":  # keyless: whole-table, no row-level approval
        return clean(f"CREATE TABLE {qname(GOLDEN, new)} AS SELECT * FROM {bid}.{ident(t)}", "merge.stage")
    cols = columns(db, GOLDEN, t)
    names = ", ".join(c.name for c in cols)
    key = db.scalar(f"SELECT KEY_COLUMN FROM DRYDOCK.TABLE_KEYS WHERE SCHEMA_NAME = {lit(GOLDEN)} "
                    f"AND TABLE_NAME = {lit(t)}")
    k = key_text(cols, key)
    dr = f"SELECT KEY_VALUE FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} AND TARGET_TABLE = {lit(t)}"
    return clean(
        f"CREATE TABLE {qname(GOLDEN, new)} AS "
        f"SELECT {names} FROM {bid}.{ident(t)} WHERE {k} IN ({dr} AND APPROVED AND CLASS IN ('CHANGED', 'ADDED')) "
        f"UNION ALL SELECT {names} FROM {qname(GOLDEN, t)} WHERE {k} IN ({dr} AND NOT APPROVED) "
        f"UNION ALL SELECT {names} FROM {qname(GOLDEN, t)} WHERE {k} NOT IN ({dr})", "merge.stage")


def _block(db: Db, mid: int, run_id, bid, reason: str) -> dict:
    db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'BLOCKED', BLOCK_REASON = {lit(reason[:512])} "
           f"WHERE MERGE_ID = {mid}")
    events.emit("merge.gated", run_id, bid, merge_id=mid, decision="BLOCKED", reason=reason, risk=0.0,
                tier=-1, limits={}, rows_needing_review=0)
    return {"decision": "BLOCKED", "block_reason": reason, "rows_applied": 0}


def apply(db: Db, merge_id: int, resolved_by: str = "human") -> dict:
    require_verified("V5")
    v4 = require_answered("V4")["V4"]
    from . import diff as diffmod
    from . import gate as gatemod

    mid = int(merge_id)
    with golden_write_lock():
        m = db.dicts(f"SELECT m.*, b.RUN_ID, b.STATUS AS BSTATUS FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b "
                     f"ON b.BRANCH_ID = m.BRANCH_ID WHERE m.MERGE_ID = {mid}")
        if not m:
            raise LookupError(f"merge {mid}")
        m = m[0]
        bid, run_id = m["BRANCH_ID"], m["RUN_ID"]
        if m["DECISION"] == "MERGED":
            return {"decision": "MERGED", "rows_applied": int(m["ROWS_APPLIED"] or 0), "replayed": True}
        if m["DECISION"] not in ("APPLYING", "PENDING"):
            raise ValueError(f"merge {mid} is {m['DECISION']}")
        if m["DECISION"] == "PENDING" and m["BSTATUS"] != "OPEN":
            return _block(db, mid, run_id, bid, f"BRANCH_{m['BSTATUS']}")

        # Re-observe at apply time: the hard blocks are checked against NOW, not
        # against whatever was true when the request was gated.
        d = diffmod.compute(db, bid, run_id, emit=False)
        blocks = gatemod.hard_blocks(db, bid, d)
        f = gatemod.facts_from(db, bid, d, blocks)
        from .config import SETTINGS
        if blocks or f.delete_pct > SETTINGS.hard_delete_pct:
            return _block(db, mid, run_id, bid, ",".join(blocks) or "DELETE_PCT_EXCEEDED")

        applied = excluded = 0
        stage_ms = swap_ms = 0.0
        pre_hex = post_hex = ""
        tables = []
        for t_info in d["tables"]:
            t = t_info["table"].split(".", 1)[1]
            mode = t_info["mode"]
            tables.append(t)
            pre = table_fingerprint(db, GOLDEN, t)
            if mode == "KEYED":
                exp_fp = expected_post(db, bid, t, pre)
                a, x = db.rows(f"SELECT SUM(CASE WHEN APPROVED THEN 1 ELSE 0 END), "
                               f"SUM(CASE WHEN APPROVED THEN 0 ELSE 1 END) FROM DRYDOCK.DIFF_ROWS "
                               f"WHERE BRANCH_ID = {lit(bid)} AND TARGET_TABLE = {lit(t)}")[0]
                applied, excluded = applied + int(a or 0), excluded + int(x or 0)
            else:
                exp_fp = table_fingerprint(db, bid, t)
                applied += t_info["added"] + t_info["deleted"]
            new = f"{t}__NEW_{mid}"
            db.run(clean(f"DROP TABLE IF EXISTS {qname(GOLDEN, new)}", "merge.cleanup"))
            _, ms = db.timed(staging_sql(db, bid, t, mid, mode))
            stage_ms += ms
            db.run(f"UPDATE DRYDOCK.MERGES SET SWAP_STAGE = 'STAGED' WHERE MERGE_ID = {mid}")
            got = table_fingerprint(db, GOLDEN, new)
            if got != exp_fp:
                db.run(clean(f"DROP TABLE {qname(GOLDEN, new)}", "merge.cleanup"))
                return _block(db, mid, run_id, bid,
                              f"STAGING_FINGERPRINT_MISMATCH (staged n={got.n}, expected n={exp_fp.n})")
            arch = f"{t}__ARCH_{mid}"
            t0 = time.perf_counter()
            if v4 == "PASS":
                with db.transaction():
                    db.run(rename_sql(GOLDEN, t, arch))
                    db.run(rename_sql(GOLDEN, new, t))
                    db.run(f"UPDATE DRYDOCK.MERGES SET SWAP_STAGE = 'SWAPPED' WHERE MERGE_ID = {mid}")
            else:  # V4 FAIL: non-transactional DDL. Disclosed two-statement window.
                db.run(rename_sql(GOLDEN, t, arch))
                db.run(f"UPDATE DRYDOCK.MERGES SET SWAP_STAGE = 'ARCHIVED' WHERE MERGE_ID = {mid}")
                db.run(rename_sql(GOLDEN, new, t))
                db.run(f"UPDATE DRYDOCK.MERGES SET SWAP_STAGE = 'SWAPPED' WHERE MERGE_ID = {mid}")
            swap_ms += (time.perf_counter() - t0) * 1000
            pre_hex, post_hex = pre.hex, got.hex
            if t == "CUSTOMERS":
                refresh_read_views(db)

        db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'MERGED', ARCHIVE_SUFFIX = {lit(str(mid))}, "
               f"ROWS_APPLIED = {applied}, ROWS_EXCLUDED = {excluded}, TABLES_MERGED = {lit(','.join(tables))}, "
               f"PRE_MERGE_FINGERPRINT = {lit(pre_hex)}, POST_MERGE_FINGERPRINT = {lit(post_hex)}, "
               f"RESOLVED_AT = CURRENT_TIMESTAMP, RESOLVED_BY = {lit(resolved_by)} WHERE MERGE_ID = {mid}")
        db.run(clean(f"DROP SCHEMA IF EXISTS {ident(bid)} CASCADE", "merge.close"))
        db.run(f"UPDATE DRYDOCK.BRANCHES SET STATUS = 'MERGED', CLOSED_AT = CURRENT_TIMESTAMP "
               f"WHERE BRANCH_ID = {lit(bid)}")
        db.run(f"DELETE FROM DRYDOCK.BASE_HASHES WHERE BRANCH_ID = {lit(bid)}")

    events.emit("merge.applied", run_id, bid, merge_id=mid, rows_applied=applied, rows_excluded=excluded,
                stage_ms=round(stage_ms, 1), swap_ms=round(swap_ms, 1), pre_fingerprint=pre_hex,
                post_fingerprint=post_hex)
    for t in tables:
        fp = table_fingerprint(db, GOLDEN, t)
        events.emit("golden.fingerprint", run_id, None, table=f"{GOLDEN}.{t}", fingerprint=fp.hex, rows=fp.n)
    return {"decision": "MERGED", "rows_applied": applied, "rows_excluded": excluded,
            "stage_ms": stage_ms, "swap_ms": swap_ms, "pre_fingerprint": pre_hex, "post_fingerprint": post_hex}


def unmerge(db: Db, merge_id: int, by: str = "human") -> dict:
    """Two renames back, verified against PRE_MERGE_FINGERPRINT. LIFO per table."""
    require_verified("V5")
    v4 = require_answered("V4")["V4"]
    mid = int(merge_id)
    with golden_write_lock():
        m = db.dicts(f"SELECT m.*, b.RUN_ID FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b "
                     f"ON b.BRANCH_ID = m.BRANCH_ID WHERE m.MERGE_ID = {mid}")
        if not m or m[0]["DECISION"] != "MERGED" or m[0]["UNMERGED_AT"] is not None:
            raise ValueError(f"merge {mid} is not an active merge")
        m = m[0]
        tables = [t for t in (m["TABLES_MERGED"] or "").split(",") if t]
        for t in tables:
            later = db.scalar(f"SELECT MIN(MERGE_ID) FROM DRYDOCK.MERGES WHERE MERGE_ID > {mid} "
                              f"AND DECISION = 'MERGED' AND UNMERGED_AT IS NULL "
                              f"AND (',' || TABLES_MERGED || ',') LIKE {lit('%,' + t + ',%')}")
            if later is not None:
                return {"ok": False, "code": "UNMERGE_SUPERSEDED", "superseded_by": int(later),
                        "message": f"merge {later} touched {GOLDEN}.{t} after this one; unmerge that first (LIFO)"}
        match = True
        restored = ""
        fps: list[tuple[str, object]] = []
        for t in tables:
            arch, undone = f"{t}__ARCH_{mid}", f"{t}__UNDONE_{mid}"
            if v4 == "PASS":
                with db.transaction():
                    db.run(rename_sql(GOLDEN, t, undone))
                    db.run(rename_sql(GOLDEN, arch, t))
            else:
                db.run(rename_sql(GOLDEN, t, undone))
                db.run(rename_sql(GOLDEN, arch, t))
            if t == "CUSTOMERS":
                refresh_read_views(db)
            fp = table_fingerprint(db, GOLDEN, t)
            fps.append((t, fp))
            restored = fp.hex
            match = match and fp.hex == m["PRE_MERGE_FINGERPRINT"]
        db.run(f"UPDATE DRYDOCK.MERGES SET UNMERGED_AT = CURRENT_TIMESTAMP, UNMERGE_FP_MATCH = {lit(match)} "
               f"WHERE MERGE_ID = {mid}")
    events.emit("unmerge.applied", m["RUN_ID"], m["BRANCH_ID"], merge_id=mid, fingerprint_match=match,
                restored_fingerprint=restored, expected_fingerprint=m["PRE_MERGE_FINGERPRINT"] or "")
    # Like apply(), publish GOLDEN's new state so every view shows the restored row count and fingerprint.
    for t, fp in fps:
        events.emit("golden.fingerprint", m["RUN_ID"], None, table=f"{GOLDEN}.{t}", fingerprint=fp.hex, rows=fp.n)
    return {"ok": True, "fingerprint_match": match, "restored": restored,
            "expected": m["PRE_MERGE_FINGERPRINT"], "by": by}


def recover(db: Db) -> list[str]:
    """Crash recovery for the non-transactional swap window (V4 FAIL). Promote
    whichever of T / T__NEW_ / T__ARCH_ is consistent; never guess silently."""
    notes = []
    for m in db.dicts("SELECT MERGE_ID, BRANCH_ID, SWAP_STAGE FROM DRYDOCK.MERGES WHERE DECISION = 'APPLYING'"):
        mid = int(m["MERGE_ID"])
        present = {r[0] for r in db.rows(f"SELECT TABLE_NAME FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = {lit(GOLDEN)}")}
        t = "CUSTOMERS"
        new, arch = f"{t}__NEW_{mid}", f"{t}__ARCH_{mid}"
        if t not in present and new in present and arch in present:
            db.run(rename_sql(GOLDEN, new, t))
            db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'MERGED', SWAP_STAGE = 'SWAPPED', "
                   f"BLOCK_REASON = 'CRASH_RECOVERED_FORWARD' WHERE MERGE_ID = {mid}")
            notes.append(f"merge {mid}: finished interrupted swap")
        elif new in present:
            db.run(clean(f"DROP TABLE {qname(GOLDEN, new)}", "merge.recover"))
            db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'BLOCKED', BLOCK_REASON = 'CRASH_RECOVERED_ABORT' "
                   f"WHERE MERGE_ID = {mid}")
            notes.append(f"merge {mid}: dropped orphan staging table")
        elif m["SWAP_STAGE"] in (None, "STAGED"):
            db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'BLOCKED', BLOCK_REASON = 'CRASH_RECOVERED_ABORT' "
                   f"WHERE MERGE_ID = {mid}")
            notes.append(f"merge {mid}: aborted before swap")
    return notes


__all__ = ["apply", "unmerge", "recover", "staging_sql", "expected_post", "ZERO"]
