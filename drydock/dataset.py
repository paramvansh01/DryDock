"""Which dataset GOLDEN holds, and switching between them.

Two datasets share one pipeline:
  demo    SOURCE_A.CUSTOMERS + SOURCE_B.CLIENTS, owned by the admin, scored against BENCH.
  upload  a person's own two files, loaded by drydock/uploads.py into UPLOADS.SOURCE_A / SOURCE_B,
          which have the demo sources' exact columns. There is no answer key, so no score.

Everything that reads a source asks active() which tables to read. Switching clears the run state
that pointed at the old GOLDEN (branches, diffs, merges, decisions, events, case law) and recreates
GOLDEN from the new dataset's starting snapshot through merge.reseed(), the single write path.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import merge
from .config import ROOT
from .db import Db, DbError, lit

RUN_TABLES = ("BRANCHES", "MATERIALISATIONS", "BRANCH_OPS", "DIFFS", "DIFF_ROWS", "BASE_HASHES", "MERGES",
              "ADJUDICATIONS", "RUNS", "CANDIDATES", "CANDIDATES_RAW", "MAPPINGS", "EVENTS", "IDEMPOTENCY")


@dataclass(frozen=True)
class Dataset:
    name: str
    a: str          # System A: the list GOLDEN starts from (SOURCE_A.CUSTOMERS columns)
    b: str          # System B: folded into GOLDEN (SOURCE_B.CLIENTS columns)
    seed: str       # GOLDEN's starting snapshot
    scored: bool    # an answer key exists (BENCH describes the demo data only)
    agent: bool     # the agent's read grants and prompts cover these tables

    @property
    def schemas(self) -> tuple[str, str]:
        return self.a.split(".")[0], self.b.split(".")[0]


DEMO = Dataset("demo", "SOURCE_A.CUSTOMERS", "SOURCE_B.CLIENTS", "BENCH.GOLDEN_CLEAN", scored=True, agent=True)
UPLOAD = Dataset("upload", "UPLOADS.SOURCE_A", "UPLOADS.SOURCE_B", "UPLOADS.GOLDEN_SEED", scored=False, agent=False)
BY_NAME = {d.name: d for d in (DEMO, UPLOAD)}


def ensure_schema(db: Db) -> None:
    """Apply sql/03_uploads.sql. Idempotent (IF NOT EXISTS throughout), so it is safe on every startup."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from _common import split_sql
    for stmt in split_sql((ROOT / "sql" / "03_uploads.sql").read_text()):
        db.run(stmt)


def _row(db: Db) -> dict | None:
    try:
        rows = db.dicts("SELECT NAME, LABEL, ACTIVATED_AT, ROWS_A, ROWS_B, DETAIL FROM DRYDOCK.DATASET "
                        "ORDER BY ACTIVATED_AT DESC LIMIT 1")
    except DbError:                     # DRYDOCK.DATASET not created yet: an install that only ever ran the demo
        return None
    return rows[0] if rows else None


def active(db: Db) -> Dataset:
    r = _row(db)
    return BY_NAME.get((r or {}).get("NAME") or "demo", DEMO)


def info(db: Db) -> dict:
    """The active dataset for the UI: name, label, row counts, when it was loaded, and its quality report."""
    r = _row(db)
    ds = BY_NAME.get((r or {}).get("NAME") or "demo", DEMO)
    detail = {}
    if r and r.get("DETAIL"):
        try:
            detail = json.loads(r["DETAIL"])
        except ValueError:
            detail = {}
    if r:
        rows_a, rows_b = int(r["ROWS_A"] or 0), int(r["ROWS_B"] or 0)
    else:
        rows_a, rows_b = _count(db, ds.a), _count(db, ds.b)
    return {"name": ds.name, "label": (r or {}).get("LABEL") or "Demo data (synthetic customers)",
            "rows_a": rows_a, "rows_b": rows_b, "activated_at": str(r["ACTIVATED_AT"]) if r else None,
            "scored": ds.scored, "agent": ds.agent, "tables": {"a": ds.a, "b": ds.b},
            "files": detail.get("files"), "quality": detail.get("quality")}


def _count(db: Db, table: str) -> int:
    try:
        return int(db.scalar(f"SELECT COUNT(*) FROM {table}") or 0)
    except DbError:
        return 0


def clear_run_state(db: Db, wipe_precedents: bool = False) -> int:
    """Everything that points at the current GOLDEN: branch schemas, ER_WORK decision tables and the run
    tables. Never touches a source or BENCH. Returns the number of branch schemas dropped."""
    dropped = 0
    for (s,) in db.rows("SELECT SCHEMA_NAME FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME LIKE 'BR!_%' ESCAPE '!'"):
        db.run(f"DROP SCHEMA {s} CASCADE")
        dropped += 1
    for (t,) in db.rows("SELECT TABLE_NAME FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = 'ER_WORK'"):
        db.run(f"DROP TABLE ER_WORK.{t}")
    for t in RUN_TABLES + (("PRECEDENTS",) if wipe_precedents else ()):
        db.run(f"DELETE FROM DRYDOCK.{t}")
    return dropped


def activate(db: Db, ds: Dataset, *, label: str, rows_a: int, rows_b: int, detail: dict | None = None) -> dict:
    """Make `ds` the dataset GOLDEN holds. Case law is wiped too: a precedent about one dataset's people
    says nothing about another's. The caller emits the resulting events (the orchestrator first clears
    the history it replays to browsers)."""
    t0 = time.perf_counter()
    dropped = clear_run_state(db, wipe_precedents=True)
    fp = merge.reseed(db, ds.seed)
    db.run("DELETE FROM DRYDOCK.DATASET")
    db.run("INSERT INTO DRYDOCK.DATASET (NAME, LABEL, ROWS_A, ROWS_B, DETAIL) VALUES ("
           f"{lit(ds.name)}, {lit(label[:400])}, {int(rows_a)}, {int(rows_b)}, {lit(json.dumps(detail or {}))})")
    return {"name": ds.name, "label": label, "rows_a": int(rows_a), "rows_b": int(rows_b), "golden_rows": fp.n,
            "fingerprint": fp.hex, "branches_dropped": dropped, "seconds": round(time.perf_counter() - t0, 2)}
