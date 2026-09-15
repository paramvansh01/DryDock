"""Scripted reconciliation and A/B plan replay.

run_scripted(): the same Drydock calls the agent makes, in a fixed order with fixed SQL.
Deterministic, and labelled "scripted" in RUNS.MODE and in the UI.

replay_plan(): the A/B treatment. It replays the control run's recorded tool calls
(branch ids and ER_WORK run tags substituted) and reuses the control's matcher verdicts
instead of re-adjudicating, so the only difference between control and treatment is
GATE_ENABLED.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from drydock import branch, dataset, diff, er, events, gate
from drydock.config import ROOT
from drydock.db import Db, lit

MAPPING = {
    "FULL_NAME": "FIRST_NAME || ' ' || LAST_NAME",
    "EMAIL": "EMAIL_ADDR",
    "PHONE": "MOBILE",
    "ADDR_LINE": "STREET",
    "CITY": "TOWN",
    "POSTCODE": "ZIP",
    "COUNTRY": ("CASE NATION WHEN 'GB' THEN 'United Kingdom' WHEN 'US' THEN 'United States' WHEN 'ES' THEN 'Spain' "
                "WHEN 'DE' THEN 'Germany' WHEN 'NL' THEN 'Netherlands' ELSE NATION END"),
    "DATE_OF_BIRTH": "TO_DATE(DOB, 'DD/MM/YYYY')",
    "LAST_SEEN": "LAST_SEEN",
}

A_LAST = "REGEXP_SUBSTR(UPPER(TRIM(a.FULL_NAME)), '[^ ]+$')"
BLOCKING = [
    ("AB", "EXACT_EMAIL", "SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID FROM SOURCE_A.CUSTOMERS a "
     "JOIN SOURCE_B.CLIENTS b ON LOWER(TRIM(a.EMAIL)) = LOWER(TRIM(b.EMAIL_ADDR))"),
    ("AB", "PHONE_LAST7", "SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID FROM SOURCE_A.CUSTOMERS a "
     "JOIN SOURCE_B.CLIENTS b ON RIGHT(REGEXP_REPLACE(a.PHONE, '[^0-9]', ''), 7) = RIGHT(REGEXP_REPLACE(b.MOBILE, '[^0-9]', ''), 7)"),
    ("AB", "POSTCODE_SURNAME3", "SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID FROM SOURCE_A.CUSTOMERS a "
     "JOIN SOURCE_B.CLIENTS b ON UPPER(REPLACE(a.POSTCODE, ' ', '')) = UPPER(REPLACE(b.ZIP, ' ', '')) "
     f"AND SUBSTR({A_LAST}, 1, 3) = SUBSTR(UPPER(TRIM(b.LAST_NAME)), 1, 3)"),
    ("AB", "SURNAME_SOUNDEX_CITY", "SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID FROM SOURCE_A.CUSTOMERS a "
     f"JOIN SOURCE_B.CLIENTS b ON SOUNDEX({A_LAST}) = SOUNDEX(UPPER(TRIM(b.LAST_NAME))) AND UPPER(TRIM(a.CITY)) = UPPER(TRIM(b.TOWN))"),
    ("AB", "DOB_SURNAME", "SELECT a.CUST_ID AS A_ID, b.CLIENT_REF AS B_ID FROM SOURCE_A.CUSTOMERS a "
     "JOIN SOURCE_B.CLIENTS b ON a.DATE_OF_BIRTH = TO_DATE(b.DOB, 'DD/MM/YYYY') "
     f"AND SOUNDEX({A_LAST}) = SOUNDEX(UPPER(TRIM(b.LAST_NAME)))"),
    ("AA", "ADDRESS_POSTCODE_DOB", "SELECT a1.CUST_ID AS A_ID, a2.CUST_ID AS B_ID FROM SOURCE_A.CUSTOMERS a1 "
     "JOIN SOURCE_A.CUSTOMERS a2 ON a1.CUST_ID < a2.CUST_ID AND UPPER(TRIM(a1.ADDR_LINE)) = UPPER(TRIM(a2.ADDR_LINE)) "
     "AND a1.POSTCODE = a2.POSTCODE AND a1.DATE_OF_BIRTH = a2.DATE_OF_BIRTH"),
]


def blocking(src: dataset.Dataset) -> list[tuple[str, str, str]]:
    """BLOCKING pointed at a dataset's two tables. Uploaded files have the demo sources' exact columns,
    so only the table names change."""
    return [(kind, rule, sql.replace(dataset.DEMO.a, src.a).replace(dataset.DEMO.b, src.b))
            for kind, rule, sql in BLOCKING]


CONFIDENCE = {"EXACT_EMAIL": 0.99, "PHONE_ADDRESS": 0.97, "FUZZY_NAME": 0.80, "NEW_CUSTOMERS": 0.98,
              "INTERNAL_DEDUP": 0.90}
NEWER_B = "m.B_LAST_SEEN > m.A_CREATED_AT"


def class_sql(cls: str, tag: str) -> list[tuple[str, str]]:
    if cls in ("EXACT_EMAIL", "PHONE_ADDRESS", "FUZZY_NAME"):
        surv = ("'{\"FULL_NAME\":\"A\",\"EMAIL\":\"' || CASE WHEN g.EMAIL IS NULL THEN 'B' ELSE 'A' END || "
                "'\",\"PHONE\":\"' || CASE WHEN g.PHONE IS NULL THEN 'B' ELSE 'A' END || "
                f"'\",\"ADDRESS\":\"' || CASE WHEN {NEWER_B} THEN 'B_newer' ELSE 'A' END || "
                "'\",\"DATE_OF_BIRTH\":\"' || CASE WHEN g.DATE_OF_BIRTH IS NULL THEN 'B' ELSE 'A' END || '\"}'")
        return [(
            f"MERGE INTO GOLDEN.CUSTOMERS g USING (SELECT * FROM ER_WORK.MATCHES_{tag} WHERE MATCH_CLASS = '{cls}') m "
            "ON (g.GOLDEN_ID = m.GOLDEN_ID) WHEN MATCHED THEN UPDATE SET "
            "g.SOURCE_B_REF = m.B_ID, g.EMAIL = COALESCE(g.EMAIL, m.B_EMAIL), g.PHONE = COALESCE(g.PHONE, m.B_PHONE), "
            f"g.ADDR_LINE = CASE WHEN {NEWER_B} THEN m.B_ADDR_LINE ELSE g.ADDR_LINE END, "
            f"g.CITY = CASE WHEN {NEWER_B} THEN m.B_CITY ELSE g.CITY END, "
            f"g.POSTCODE = CASE WHEN {NEWER_B} THEN m.B_POSTCODE ELSE g.POSTCODE END, "
            "g.DATE_OF_BIRTH = COALESCE(g.DATE_OF_BIRTH, m.B_DATE_OF_BIRTH), g.MERGED_AT = CURRENT_TIMESTAMP, "
            f"g.SURVIVORSHIP = {surv}",
            f"enrich golden rows with their {cls} match from SOURCE_B, per the survivorship policy")]
    if cls == "NEW_CUSTOMERS":
        return [(
            "INSERT INTO GOLDEN.CUSTOMERS (GOLDEN_ID, FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, POSTCODE, COUNTRY, "
            "DATE_OF_BIRTH, SOURCE_A_REF, SOURCE_B_REF, MERGED_A_REFS, MERGED_AT, SURVIVORSHIP) "
            f"SELECT NEW_GOLDEN_ID, B_FULL_NAME, B_EMAIL, B_PHONE, B_ADDR_LINE, B_CITY, B_POSTCODE, B_COUNTRY, "
            f"B_DATE_OF_BIRTH, NULL, B_ID, NULL, CURRENT_TIMESTAMP, '{{\"ALL\":\"B\"}}' FROM ER_WORK.NEW_{tag}",
            "add SOURCE_B customers with no match as new golden rows")]
    if cls == "INTERNAL_DEDUP":
        return [
            # MERGE, not `UPDATE ... SET c = (SELECT ...)`: Exasol rejects a subquery in SET
            # ([42000] "tables are not allowed in this context").
            (f"MERGE INTO GOLDEN.CUSTOMERS g USING ER_WORK.DEDUP_{tag} d ON (g.GOLDEN_ID = d.KEEP_GOLDEN_ID) "
             "WHEN MATCHED THEN UPDATE SET g.MERGED_A_REFS = d.A_DUP_ID, g.MERGED_AT = CURRENT_TIMESTAMP",
             "record the absorbed duplicate on the surviving golden row"),
            (f"DELETE FROM GOLDEN.CUSTOMERS WHERE GOLDEN_ID IN (SELECT DROP_GOLDEN_ID FROM ER_WORK.DEDUP_{tag})",
             "remove the duplicate golden row"),
        ]
    raise ValueError(cls)


def expected_rows(db: Db, cls: str, tag: str) -> int:
    return int(db.scalar(f"SELECT COUNT(*) FROM ({er.expected_keys_sql(tag, cls)})") or 0)


# ------------------------------------------------------------------ plan recording

def plan_path(run_id: str) -> Path:
    p = ROOT / "runs" / run_id / "plan.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def record_step(run_id: str, tool: str, args: dict, result: dict) -> None:
    with plan_path(run_id).open("a") as f:
        f.write(json.dumps({"tool": tool, "args": args, "result": _brief(result)}, default=str) + "\n")


def _brief(r: dict) -> dict:
    return {k: r[k] for k in ("branch_id", "decision", "op_id", "status", "merge_id", "rows_affected") if k in r}


def start_run_row(db: Db, run_id: str, *, tier: int, gate_enabled: bool, seed: int, mode: str,
                  plan_from_run: str | None = None, min_confidence: float = 0.0) -> None:
    db.run(f"DELETE FROM DRYDOCK.RUNS WHERE RUN_ID = {lit(run_id)}")
    db.run("INSERT INTO DRYDOCK.RUNS (RUN_ID, TIER, GATE_ENABLED, MODE, MIN_CONFIDENCE, REJECTIONS_SEEN, SEED, "
           f"PLAN_FROM_RUN) VALUES ({lit(run_id)}, {int(tier)}, {lit(gate_enabled)}, {lit(mode)}, {min_confidence}, 0, "
           f"{int(seed)}, {lit(plan_from_run)})")
    events.emit("run.started", run_id, None, tier=tier, gate_enabled=gate_enabled, mode=mode, seed=seed,
                min_confidence=min_confidence, plan_from_run=plan_from_run)


def end_run(db: Db, run_id: str) -> dict:
    from bench.score import score
    db.run(f"UPDATE DRYDOCK.RUNS SET ENDED_AT = CURRENT_TIMESTAMP WHERE RUN_ID = {lit(run_id)}")
    pending = gate.list_pending(db, include_resolved=True, run_id=run_id)
    summary: dict[str, object] = {"merges": {str(m["MERGE_ID"]): m["DECISION"] for m in pending}}
    if not dataset.active(db).scored:
        summary["metrics_error"] = "no answer key: scores exist for the demo data only (BENCH describes it)"
    else:
        try:
            summary["metrics"] = score(db, run_id)
        except Exception as e:  # scoring needs BENCH; report, never hide
            summary["metrics_error"] = f"{type(e).__name__}: {e}"
    events.emit("run.ended", run_id, None, status="OK", summary=summary)
    return summary


# ------------------------------------------------------------------ scripted run

def run_scripted(db: Db, run_id: str, *, tier: int = 2, gate_enabled: bool = True, seed: int = 20260913) -> dict:
    er.install_hooks()
    plan_path(run_id).unlink(missing_ok=True)
    start_run_row(db, run_id, tier=tier, gate_enabled=gate_enabled, seed=seed, mode="scripted")
    events.emit("agent.note", run_id, None, text="Scripted run: fixed SQL, no autonomous planning.",
                phase="start")

    def step(tool: str, fn, **args):
        out = fn(**args)
        record_step(run_id, tool, {k: v for k, v in args.items() if k != "db"}, out if isinstance(out, dict) else {})
        return out

    step("declare_mapping", lambda **k: {"mapping": er.declare_mapping(db, run_id, MAPPING, k["rationale"])},
         rationale="scripted mapping")
    for kind, rule, sql in blocking(dataset.active(db)):
        step("stage_candidates", lambda **k: er.stage_candidates(db, run_id, k["rule"], k["select_sql"], k["kind"]),
             rule=rule, select_sql=sql, kind=kind)
    step("run_matching", lambda **k: er.run_matching(db, run_id))
    tag = er.run_tag(run_id)
    for cls in ("EXACT_EMAIL", "PHONE_ADDRESS", "NEW_CUSTOMERS", "INTERNAL_DEDUP", "FUZZY_NAME"):
        exp_rows = expected_rows(db, cls, tag)
        events.emit("hypothesis.stated", run_id, None, match_class=cls, expected_count=exp_rows,
                    rationale="rows published for this class by run_matching")
        if exp_rows == 0:
            continue
        b = step("open_branch", lambda **k: branch.open_branch(db, k["purpose"], run_id, k["defect_class"],
                                                               opened_by="script", gate_enabled=gate_enabled),
                 purpose=f"apply {cls} decisions", defect_class=cls)
        bid = b["branch_id"]
        for sql, why in class_sql(cls, tag):
            step("run_in_branch", lambda **k: branch.run_in_branch(db, k["branch_id"], k["sql"], k["rationale"],
                                                                   run_id=run_id),
                 branch_id=bid, sql=sql, rationale=why)
        d = diff.compute(db, bid, run_id)
        observed = d["total_changed"]
        conf = CONFIDENCE[cls]
        if observed != exp_rows:
            new_conf = round(max(0.3, conf - 0.2), 2)
            events.emit("selfcheck", run_id, bid, expected=exp_rows, observed=observed,
                        discrepancy=observed - exp_rows, confidence_before=conf, confidence_after=new_conf,
                        note=f"expected {exp_rows} rows, the diff shows {observed}")
            conf = new_conf
        step("request_merge", lambda **k: gate.request_merge(db, k["branch_id"], k["confidence"], k["rationale"],
                                                             requested_by="script"),
             branch_id=bid, confidence=conf, rationale=f"{cls}: {observed} rows observed, {exp_rows} expected")
    return end_run(db, run_id)


# ------------------------------------------------------------------ A/B treatment

def clone_matching(db: Db, from_run: str, to_run: str) -> None:
    """Copy the control's matcher verdicts so the treatment differs ONLY in the gate."""
    cols = ("KIND, A_ID, B_ID, BLOCK_RULES, SIGNALS, SCORE_TOTAL, VOTE_DETERM, VOTE_PROB, VOTE_SKEPTIC, PANEL_RESULT, "
            "AI_VERDICT, AI_SCORE, ADJUDICATOR, PRECEDENTS_USED, VERDICT, MATCH_CLASS, RATIONALE, COMPONENT_ID, COMPONENT_SIZE")
    db.run(f"DELETE FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(to_run)}")
    db.run(f"INSERT INTO DRYDOCK.CANDIDATES (RUN_ID, {cols}) SELECT {lit(to_run)}, {cols} FROM DRYDOCK.CANDIDATES "
           f"WHERE RUN_ID = {lit(from_run)}")
    db.run(f"DELETE FROM DRYDOCK.MAPPINGS WHERE RUN_ID = {lit(to_run)}")
    db.run(f"INSERT INTO DRYDOCK.MAPPINGS (RUN_ID, MAPPING) SELECT {lit(to_run)}, MAPPING FROM DRYDOCK.MAPPINGS "
           f"WHERE RUN_ID = {lit(from_run)}")
    er.publish(db, to_run, er.run_tag(to_run))


def replay_plan(db: Db, from_run: str, to_run: str, *, tier: int = 2, gate_enabled: bool = True,
                seed: int = 20260913) -> dict:
    er.install_hooks()
    steps = [json.loads(line) for line in plan_path(from_run).read_text().splitlines() if line.strip()]
    start_run_row(db, to_run, tier=tier, gate_enabled=gate_enabled, seed=seed, mode="treatment",
                  plan_from_run=from_run)
    events.emit("agent.note", to_run, None, phase="start",
                text=f"TREATMENT: replaying run {from_run}'s exact tool calls and matcher verdicts; only the gate differs.")
    clone_matching(db, from_run, to_run)
    old_tag, new_tag = er.run_tag(from_run), er.run_tag(to_run)
    bmap: dict[str, str] = {}
    for s in steps:
        t, a = s["tool"], dict(s["args"])
        if t in ("declare_mapping", "stage_candidates", "run_matching"):
            continue  # cloned above
        if "sql" in a:
            a["sql"] = re.sub(rf"\b(MATCHES|NEW|DEDUP)_{re.escape(old_tag)}\b", rf"\1_{new_tag}", a["sql"])
        if "branch_id" in a:
            a["branch_id"] = bmap.get(a["branch_id"], a["branch_id"])
        if t == "open_branch":
            out = branch.open_branch(db, a["purpose"], to_run, a.get("defect_class"), opened_by="replay",
                                     gate_enabled=gate_enabled)
            bmap[s["result"]["branch_id"]] = out["branch_id"]
        elif t == "run_in_branch":
            branch.run_in_branch(db, a["branch_id"], a["sql"], a.get("rationale", ""), run_id=to_run)
        elif t == "request_merge":
            gate.request_merge(db, a["branch_id"], a.get("confidence"), a.get("rationale", ""), requested_by="replay")
        elif t == "discard_branch":
            branch.discard_branch(db, a["branch_id"], a.get("reason", "replayed discard"))
    return end_run(db, to_run)
