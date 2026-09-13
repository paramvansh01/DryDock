"""First-time setup, after scripts/create_users.py and scripts/verify.py.

    uv run python scripts/setup.py --seed 20260913

  1. svc    sql/00_schemas.sql, sql/01_drydock_ddl.sql
  2. admin  bench/sql/10_sources.sql + synthetic data (bench/generate.py)
  3. svc    reset: GOLDEN.CUSTOMERS from BENCH.GOLDEN_CLEAN, sql/02_grants.sql
Every statement is printed with its outcome; the first failure stops with the full
Exasol error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _common import now_iso, split_sql  # noqa: E402

from drydock.db import Db, DbError  # noqa: E402


RECORD: list[dict] = []      # what actually ran here, for scripts/probe_all.py (Gate 3 evidence)


def run_file(db: Db, path: Path) -> None:
    for stmt in split_sql(path.read_text()):
        first = stmt.splitlines()[0][:100]
        try:
            n = db.run(stmt)
            print(f"  ok   ({n:>6}) {first}")
            RECORD.append({"file": path.name, "identity": db.identity, "ts": now_iso(), "sql": stmt, "rows": n})
        except DbError as e:
            print(f"  FAIL {first}\n       [{e.code}] {e.message}")
            raise SystemExit(1) from e


def write_record() -> None:
    """probe_all.py treats a statement recorded here as PROVEN on this instance: it ran for real, as the
    identity that owns it. Statements in bench/sql/10_sources.sql are admin-owned, so probing them as
    DRYDOCK_SVC would only prove the privilege floor, and re-running them as admin would drop loaded data."""
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "setup.json").write_text(json.dumps(RECORD, indent=1))
    print(f"  recorded {len(RECORD)} executed statements -> runs/setup.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--skip-data", action="store_true", help="schemas only")
    a = ap.parse_args()

    svc = Db("svc")
    print("[1] DRYDOCK_SVC: schemas + DRYDOCK tables")
    run_file(svc, ROOT / "sql" / "00_schemas.sql")
    run_file(svc, ROOT / "sql" / "01_drydock_ddl.sql")
    if not a.skip_data:
        print("[2] admin: sources + BENCH + synthetic data")
        from bench.generate import generate, load
        ds = generate(a.seed)
        got = load(ds, RECORD)
        print("  ", got)
    print("[3] DRYDOCK_SVC: reset GOLDEN + grants")
    from reset import reset
    print("  ", reset(svc))
    write_record()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
