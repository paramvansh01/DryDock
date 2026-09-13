"""Run every SQL statement the product can issue against the live instance, and record
each outcome.

    uv run python scripts/probe_all.py            # after scripts/setup.py seeded the instance

What it runs, as DRYDOCK_SVC through drydock.db (so the dialect firewall applies):
  1. every complete static SQL literal in drydock/, agent/, bench/, scripts/
  2. the generated SQL: entity-resolution normalisation, panel and publish; playbook
     blocking rules and per-class merge SQL (rendered for a probe run tag)
Statements run inside transactions that are always rolled back, so nothing persists.
  - the entity-resolution pipeline is one group in one transaction, because each
    statement reads tables the previous ones create;
  - reset.py's CTAS gets the DROP that precedes it as a rolled-back preamble;
  - bench/sql/10_sources.sql is owned by the admin; it is proven by setup.py having
    run it, which is recorded in runs/setup.json (missing record -> ERROR).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import dialect_lint as dl  # noqa: E402
from _common import append_verified, now_iso  # noqa: E402

TAG = "R_PROBE_ALL"


class _Rollback(Exception):
    pass


ADMIN_FILE = "bench/sql/10_sources.sql"      # owned by the admin; evidence comes from runs/setup.json


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def setup_evidence() -> dict[str, dict]:
    f = ROOT / "runs" / "setup.json"
    if not f.exists():
        return {}
    return {_norm(r["sql"]): r for r in json.loads(f.read_text())}


def groups() -> list[dict]:
    """A group is a list of (origin, sql) run IN ORDER inside one rolled-back transaction.
    Independent statements are groups of one; `preamble` statements run first and are not reported."""
    from agent import playbook
    from drydock import er

    items, _exempt = dl.collect([ROOT / d for d in ("drydock", "agent", "bench", "scripts")])
    out: list[dict] = []
    for o, sql, templ in items:
        if templ:
            continue
        if o.startswith(ADMIN_FILE):
            out.append({"items": [(o, sql)], "by_setup": True})
        elif "CREATE TABLE GOLDEN.CUSTOMERS AS" in _norm(sql):   # not-sql: a substring matcher, not a statement
            # reset() drops GOLDEN.CUSTOMERS (a template statement) before this CTAS; replay that order.
            out.append({"items": [(o, sql)], "preamble": ["DROP TABLE GOLDEN.CUSTOMERS"]})
        else:
            out.append({"items": [(o, sql)]})

    a = {f: f for f in er.GOLDEN_FIELDS}
    pipeline: list[tuple[str, str]] = [
        ("er.norm_a", f"CREATE OR REPLACE TABLE ER_WORK.A_NORM_{TAG} AS " + er._norm_select("SOURCE_A.CUSTOMERS", a, "CUST_ID")),
        ("er.norm_b", f"CREATE OR REPLACE TABLE ER_WORK.B_NORM_{TAG} AS " + er._norm_select("SOURCE_B.CLIENTS", playbook.MAPPING, "CLIENT_REF")),
    ]
    pipeline += [(f"er.publish.{i}", s) for i, s in enumerate(er.publish_sql("probe", TAG, playbook.MAPPING))]
    pipeline.append(("er.signals", er.signals_select(f"ER_WORK.A_NORM_{TAG}", f"ER_WORK.B_NORM_{TAG}")))
    pipeline += [(f"playbook.block.{rule}", sql) for _k, rule, sql in playbook.BLOCKING]
    for cls in ("EXACT_EMAIL", "PHONE_ADDRESS", "FUZZY_NAME", "NEW_CUSTOMERS", "INTERNAL_DEDUP"):
        pipeline += [(f"playbook.{cls}.{i}", sql) for i, (sql, _w) in enumerate(playbook.class_sql(cls, TAG))]
    out.append({"items": pipeline})
    return out


def main() -> int:
    from drydock.config import verified_status
    from drydock.db import Db, DbError
    from drydock.lintguard import DialectError

    ddl_transactional = verified_status("V4") == "PASS"
    db = Db("svc")
    evidence = setup_evidence()
    results = []
    for grp in groups():
        if grp.get("by_setup"):
            for origin, sql in grp["items"]:
                ev = evidence.get(_norm(sql))
                if ev:
                    results.append({"origin": origin, "outcome": "OK-BY-SETUP",
                                    "detail": f"ran as {ev['identity']} at {ev['ts']} (runs/setup.json)"})
                else:
                    results.append({"origin": origin, "outcome": "ERROR",
                                    "detail": "admin-owned statement with no record in runs/setup.json — "
                                              "run scripts/setup.py on this instance first"})
            continue
        if not ddl_transactional and any(
                sql.lstrip().upper().startswith(("CREATE", "DROP", "ALTER", "RENAME", "GRANT", "REVOKE"))
                for _o, sql in grp["items"]):
            for origin, _sql in grp["items"]:
                results.append({"origin": origin, "outcome": "SKIPPED-DDL", "detail": "V4 not PASS: DDL would persist"})
            continue
        done: list[dict] = []
        try:
            with db.transaction():
                for sql in grp.get("preamble", ()):
                    db.execute(sql)
                for origin, sql in grp["items"]:
                    t0 = time.perf_counter()
                    try:
                        db.execute(sql)
                        done.append({"origin": origin, "outcome": "OK", "ms": round((time.perf_counter() - t0) * 1000, 1)})
                    except DialectError as e:
                        done.append({"origin": origin, "outcome": "ERROR", "detail": f"dialect firewall: {e}"[:1500]})
                    except DbError as e:
                        done.append({"origin": origin, "outcome": "ERROR", "detail": f"[{e.code}] {e.message}"[:1500]})
                raise _Rollback()
        except _Rollback:
            pass
        except (DialectError, DbError) as e:            # a preamble statement failed: the group is unproven
            detail = f"group preamble failed: {e}"[:1500]
            done = [{"origin": o, "outcome": "ERROR", "detail": detail} for o, _s in grp["items"]]
        results += done
    errors = [r for r in results if r["outcome"] == "ERROR"]
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "probe_all.json").write_text(json.dumps(results, indent=2))
    lines = [f"### {now_iso()} · probe_all.py · {len(results)} statements · {len(errors)} ERROR · "
             f"{sum(r['outcome'] == 'SKIPPED-DDL' for r in results)} SKIPPED-DDL · "
             f"{sum(r['outcome'] == 'OK-BY-SETUP' for r in results)} OK-BY-SETUP", ""]
    lines += [f"- {r['outcome']:11} {r['origin']}" + (f"\n  {r['detail']}" if r.get("detail") else "") for r in results]
    append_verified("Probes", "\n".join(lines))
    for r in results:
        print(f"{r['outcome']:12} {r['origin']}" + (f"\n             {r['detail'][:400]}" if r["outcome"] == "ERROR" else ""))
    print(f"\n{len(results)} statements, {len(errors)} ERROR — recorded in VERIFIED.md and runs/probe_all.json")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
