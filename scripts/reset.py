"""One command, under 10 seconds: restore GOLDEN from the active dataset's snapshot and clear run state.

    ./scripts/reset.sh                   # keep PRECEDENTS (case law persists across takes)
    ./scripts/reset.sh --wipe-precedents
    ./scripts/reset.sh --demo            # switch to the demo data first (the live tests need it)

Drops every BR_* schema, every GOLDEN table (including __ARCH_/__NEW_/__UNDONE_),
every ER_WORK table; recreates GOLDEN.CUSTOMERS from the active dataset's starting
snapshot (BENCH.GOLDEN_CLEAN for the demo, UPLOADS.GOLDEN_SEED for uploaded files)
and re-applies sql/02_grants.sql. Never touches SOURCE_A, SOURCE_B, UPLOADS or BENCH.
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

from drydock import dataset, merge  # noqa: E402
from drydock.db import Db  # noqa: E402

RUN_TABLES = dataset.RUN_TABLES


def reset(db: Db, wipe_precedents: bool = False) -> dict:
    t0 = time.perf_counter()
    ds = dataset.active(db)
    dropped = dataset.clear_run_state(db, wipe_precedents)
    merge.reseed(db, ds.seed)
    for stmt in split_sql((ROOT / "sql" / "02_grants.sql").read_text()):
        db.run(stmt)
    rows = int(db.scalar("SELECT COUNT(*) FROM GOLDEN.CUSTOMERS"))
    return {"dataset": ds.name, "golden_rows": rows, "branches_dropped": dropped,
            "seconds": round(time.perf_counter() - t0, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe-precedents", action="store_true")
    ap.add_argument("--demo", action="store_true", help="make the demo data the active dataset, then reset")
    a = ap.parse_args()
    db = Db("svc")
    if a.demo:
        db.run("DELETE FROM DRYDOCK.DATASET")        # no row = the demo (drydock/dataset.py)
    out = reset(db, a.wipe_precedents or a.demo)
    print(out)
    if out["seconds"] > 10:
        print("WARNING: reset took longer than the 10s target", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
