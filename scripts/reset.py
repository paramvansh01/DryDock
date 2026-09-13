"""One command, under 10 seconds: restore GOLDEN from the seed snapshot and clear run state.

    ./scripts/reset.sh                   # keep PRECEDENTS (case law persists across takes)
    ./scripts/reset.sh --wipe-precedents

Drops every BR_* schema, every GOLDEN table (including __ARCH_/__NEW_/__UNDONE_),
every ER_WORK table; recreates GOLDEN.CUSTOMERS from BENCH.GOLDEN_CLEAN and
re-applies sql/02_grants.sql. Never touches SOURCE_A, SOURCE_B or BENCH.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _common import split_sql  # noqa: E402

from drydock.db import Db, lit  # noqa: E402

RUN_TABLES = ("BRANCHES", "MATERIALISATIONS", "BRANCH_OPS", "DIFFS", "DIFF_ROWS", "BASE_HASHES", "MERGES",
              "ADJUDICATIONS", "RUNS", "CANDIDATES", "CANDIDATES_RAW", "MAPPINGS", "EVENTS", "IDEMPOTENCY")


def reset(db: Db, wipe_precedents: bool = False) -> dict:
    t0 = time.perf_counter()
    dropped = []
    for (s,) in db.rows("SELECT SCHEMA_NAME FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME LIKE 'BR!_%' ESCAPE '!'"):
        db.run(f"DROP SCHEMA {s} CASCADE")
        dropped.append(s)
    for schema in ("GOLDEN", "ER_WORK"):
        for (t,) in db.rows(f"SELECT TABLE_NAME FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = {lit(schema)}"):
            db.run(f"DROP TABLE {schema}.{t}")
    db.run("CREATE TABLE GOLDEN.CUSTOMERS AS SELECT * FROM BENCH.GOLDEN_CLEAN")
    for stmt in split_sql((ROOT / "sql" / "02_grants.sql").read_text()):
        db.run(stmt)
    for t in RUN_TABLES + (("PRECEDENTS",) if wipe_precedents else ()):
        db.run(f"DELETE FROM DRYDOCK.{t}")
    rows = int(db.scalar("SELECT COUNT(*) FROM GOLDEN.CUSTOMERS"))
    return {"golden_rows": rows, "branches_dropped": len(dropped), "seconds": round(time.perf_counter() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe-precedents", action="store_true")
    a = ap.parse_args()
    out = reset(Db("svc"), a.wipe_precedents)
    print(out)
    if out["seconds"] > 10:
        print("WARNING: reset took longer than the 10s target", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
