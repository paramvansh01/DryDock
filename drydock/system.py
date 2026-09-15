"""Live metadata for the UI's Live System view: what Exasol reports, right now.

Read-only. Session id, the database clock, version, per-table row counts, columns,
declared keys and gate tiers all come from the instance at the moment of the call.
"""

from __future__ import annotations

import time

from . import dataset
from .config import SETTINGS
from .db import Db

SCHEMAS = ("SOURCE_A", "SOURCE_B", "UPLOADS", "GOLDEN", "GOLDEN_V", "DRYDOCK", "ER_WORK", "BENCH")
COLUMN_SCHEMAS = ("SOURCE_A", "SOURCE_B", "UPLOADS", "GOLDEN", "GOLDEN_V", "DRYDOCK", "BENCH")
# The verified facts a scripted run depends on (require_verified / require_answered in branch, diff, er, merge).
RUN_CHECKS = ("V3", "V4", "V5", "V6", "V11")


def snapshot(db: Db, trigger: str) -> dict:
    t0 = time.perf_counter()
    session, db_time = db.rows("SELECT CURRENT_SESSION, CURRENT_TIMESTAMP")[0]
    version = db.scalar("SELECT PARAM_VALUE FROM EXA_METADATA WHERE PARAM_NAME = 'databaseProductVersion'")
    tables = [{"schema": s, "table": t, "rows": int(n or 0), "kind": "TABLE"} for s, t, n in db.rows(
        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROW_COUNT FROM EXA_ALL_TABLES "
        "WHERE TABLE_SCHEMA IN ('SOURCE_A', 'SOURCE_B', 'UPLOADS', 'GOLDEN', 'DRYDOCK', 'ER_WORK', 'BENCH') "
        "OR TABLE_SCHEMA LIKE 'BR!_%' ESCAPE '!' ORDER BY TABLE_SCHEMA, TABLE_NAME")]
    tables += [{"schema": s, "table": v, "rows": None, "kind": "VIEW"} for s, v in db.rows(
        "SELECT VIEW_SCHEMA, VIEW_NAME FROM EXA_ALL_VIEWS WHERE VIEW_SCHEMA = 'GOLDEN_V'")]
    columns: dict[str, list] = {}
    for s, t, c, typ in db.rows(
            "SELECT COLUMN_SCHEMA, COLUMN_TABLE, COLUMN_NAME, COLUMN_TYPE FROM EXA_ALL_COLUMNS "
            "WHERE COLUMN_SCHEMA IN ('SOURCE_A', 'SOURCE_B', 'UPLOADS', 'GOLDEN', 'GOLDEN_V', 'DRYDOCK', 'BENCH') "
            "ORDER BY COLUMN_SCHEMA, COLUMN_TABLE, COLUMN_ORDINAL_POSITION"):
        if "__" in t:                      # GOLDEN's __ARCH_/__NEW_ copies repeat CUSTOMERS' columns
            continue
        columns.setdefault(f"{s}.{t}", []).append([c, typ])
    # Keys exactly as the database records them: declared constraints, plus the diff keys Drydock registered
    # and verified unique.
    keys = [{"schema": s, "table": t, "column": c, "kind": k, "ref": f"{rs}.{rt}.{rc}" if rs else None}
            for s, t, k, c, rs, rt, rc in db.rows(
                "SELECT CONSTRAINT_SCHEMA, CONSTRAINT_TABLE, CONSTRAINT_TYPE, COLUMN_NAME, REFERENCED_SCHEMA, "
                "REFERENCED_TABLE, REFERENCED_COLUMN FROM EXA_ALL_CONSTRAINT_COLUMNS "
                "WHERE CONSTRAINT_SCHEMA IN ('SOURCE_A', 'SOURCE_B', 'GOLDEN', 'DRYDOCK', 'BENCH')")]
    keys += [{"schema": s, "table": t, "column": c, "kind": "DIFF KEY" + (" (verified unique)" if u else ""), "ref": None}
             for s, t, c, u in db.rows("SELECT SCHEMA_NAME, TABLE_NAME, KEY_COLUMN, IS_UNIQUE FROM DRYDOCK.TABLE_KEYS")]
    tiers = [{"tier": int(t), "label": lb, "max_changed": int(mc or 0), "max_risk": float(mr or 0),
              "allow_deletes": bool(ad), "max_delete_pct": float(md or 0)} for t, lb, mc, mr, ad, md in db.rows(
        "SELECT TIER, LABEL, MAX_CHANGED, MAX_RISK, ALLOW_DELETES, MAX_DELETE_PCT FROM DRYDOCK.TIER_CONFIG ORDER BY TIER")]
    config = {"tiers": tiers, "planner_model": SETTINGS.planner_model, "adjudicator_model": SETTINGS.adjudicator_model,
              "adjudicator_rung": SETTINGS.adjudicator_rung, "mcp_row_limit": SETTINGS.mcp_row_limit,
              "mcp_schema_pattern": SETTINGS.mcp_schema_pattern,
              "dataset": dataset.info(db), "readiness": readiness()}
    return {"db_time": str(db_time), "session": str(session), "version": str(version), "tables": tables,
            "columns": columns, "keys": keys, "config": config,
            "ms": round((time.perf_counter() - t0) * 1000, 1), "trigger": trigger}


def readiness() -> dict:
    """What a person needs before a run can work, checked the way the run itself checks it. The UI turns
    this into a plain-language checklist, with the fix for each problem."""
    import os

    from .config import verified_status
    checks = {c: verified_status(c) for c in RUN_CHECKS}
    return {"checks": checks, "checks_ok": all(v == "PASS" for v in checks.values()),
            "gemini_key": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
            "adjudicator": SETTINGS.adjudicator_rung}
