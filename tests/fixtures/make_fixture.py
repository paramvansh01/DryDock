"""Builds tests/fixtures/run_replay.jsonl, an event stream for developing the UI
without a backend.

    uv run python tests/fixtures/make_fixture.py

The numbers in this fixture are illustrative. The pair records are real rows from the
synthetic generator (seed 20260913), but every count, timing and score is invented to
exercise the UI. The stream announces itself as mode "replay-fixture" and the UI
watermarks it.
"""

from __future__ import annotations

import datetime as dt
import json
import zlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.generate import generate  # noqa: E402
from drydock import events  # noqa: E402
from drydock.er import compare_summary  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "run_replay.jsonl"
T0 = dt.datetime(2026, 9, 12, 14, 0, 0, tzinfo=dt.timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def tick(self, s=0.4):
        self.t += dt.timedelta(seconds=s)
        return self.t.isoformat(timespec="milliseconds")


def main() -> None:
    ds = generate(20260913)
    a = {x.CUST_ID: x for x in ds.a}
    b = {x.CLIENT_REF: x for x in ds.b}
    clk = Clock()
    out: list[dict] = []

    def ev(type_, run, branch=None, dt_s=0.4, **payload):
        e = events.validate({"type": type_, "ts": clk.tick(dt_s), "run_id": run, "branch_id": branch,
                             "payload": events._jsonable(payload)})
        out.append(e)

    def arec(x):
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in x.__dict__.items()}

    def golden(x, **over):
        d = {"GOLDEN_ID": f"G-{x.CUST_ID}", "FULL_NAME": x.FULL_NAME, "EMAIL": x.EMAIL, "PHONE": x.PHONE,
             "ADDR_LINE": x.ADDR_LINE, "CITY": x.CITY, "POSTCODE": x.POSTCODE, "COUNTRY": x.COUNTRY,
             "DATE_OF_BIRTH": x.DATE_OF_BIRTH.isoformat() if x.DATE_OF_BIRTH else None, "SOURCE_A_REF": x.CUST_ID,
             "SOURCE_B_REF": None, "MERGED_A_REFS": None, "MERGED_AT": None, "SURVIVORSHIP": None}
        d.update(over)
        return d

    def card(a_id, b_id, *, panel, votes, sig, verdict="MERGE", ai=None, approved=True, reason="DEFAULT",
             precedents=(), cls="CHANGED", cols=("SOURCE_B_REF", "MERGED_AT", "SURVIVORSHIP")):
        x, y = a[a_id], b[b_id]
        before = golden(x)
        after = golden(x, SOURCE_B_REF=b_id, MERGED_AT="2026-09-12T14:03:10",
                       SURVIVORSHIP='{"FULL_NAME":"A","ADDRESS":"A"}')
        return {"key": f"G-{a_id}", "class": cls, "cols_changed": list(cols), "before": before, "after": after,
                "approved": approved, "default_reason": reason,
                "pair": {"pair_id": zlib.crc32((a_id + b_id).encode()) % 100000, "kind": "AB", "a_id": a_id, "b_id": b_id,
                         "a": arec(x), "b": arec(y), "signals": sig, "summary": compare_summary(sig),
                         "score": round(sum(v or 0 for k, v in sig.items() if k in ("email", "phone", "name", "addr", "dob")) / 5000, 3),
                         "votes": votes, "panel": panel, "ai_verdict": ai,
                         "ai_score": 0.91 if ai else None, "adjudicator": "gemini_fallback" if ai else None,
                         "rationale": "dates of birth decades apart at a shared address: parent and child" if ai == "DIFFERENT_PERSON" else None,
                         "verdict": verdict, "component_size": 2, "precedents": list(precedents)}}

    matches = [p for p in ds.true_pairs if p[2]][:14]
    decoys = {k: [p for p in ds.true_pairs if p[4] == k] for k in
              ("FAMILY_SAME_ADDRESS", "SPOUSE_SHARED_CONTACT", "RECYCLED_EMAIL", "PERSON_VS_BUSINESS")}
    smith = decoys["FAMILY_SAME_ADDRESS"][0]

    for run, gate_on in (("fixture-control", False), ("fixture-gated", True)):
        ev("run.started", run, tier=2, gate_enabled=gate_on, mode="replay-fixture", seed=20260913,
           min_confidence=0.0, plan_from_run=None if not gate_on else "fixture-control")
        ev("agent.note", run, text="ILLUSTRATIVE FIXTURE — not a real run. Records are synthetic; every count is invented.",
           phase="start")
        if gate_on:
            continue
    run = "fixture-gated"
    ev("profile.found", run, source="SOURCE_A", table="CUSTOMERS", column="FULL_NAME",
       finding="single full-name column; 36,600 rows; 0 nulls", stats={"rows": 36600})
    ev("profile.found", run, source="SOURCE_B", table="CLIENTS", column="NATION",
       finding="ISO-2 country codes (GB, US, ES, DE, NL) where SOURCE_A stores full names", stats={"distinct": 5})
    ev("profile.found", run, source="SOURCE_B", table="CLIENTS", column="DOB",
       finding="date of birth as text DD/MM/YYYY; 7.9% null", stats={"null_rate": 0.079})
    ev("mapping.declared", run, mapping={"FULL_NAME": "FIRST_NAME || ' ' || LAST_NAME", "EMAIL": "EMAIL_ADDR",
                                         "PHONE": "MOBILE", "ADDR_LINE": "STREET", "CITY": "TOWN", "POSTCODE": "ZIP",
                                         "COUNTRY": "CASE NATION WHEN 'GB' THEN 'United Kingdom' ... END",
                                         "DATE_OF_BIRTH": "TO_DATE(DOB, 'DD/MM/YYYY')"},
       rationale="split vs combined name, ISO codes vs names, text dates")
    ev("candidates.ready", run, dt_s=2.4, kind="AB", possible_pairs=36600 * 32400, candidates=41200,
       reduction_ratio=round(36600 * 32400 / 41200, 1),
       per_rule={"EXACT_EMAIL": 24110, "PHONE_LAST7": 26020, "POSTCODE_SURNAME3": 19880, "SURNAME_SOUNDEX_CITY": 9020,
                 "DOB_SURNAME": 21030}, ms=2400.0)
    ev("adjudicating", run, pairs=71, adjudicator="gemini_fallback")
    ev("precedent.cited", run, pair_id=4411, precedent_ids=[14], verdict="DIFFERENT_PERSON", score=0.91,
       notes=["father and son — never merge on shared address alone"])
    ev("adjudicated", run, dt_s=3.0, pairs=71, adjudicator="gemini_fallback",
       verdicts={"SAME_PERSON": 44, "DIFFERENT_PERSON": 23, "INSUFFICIENT_EVIDENCE": 4}, latency_ms=3100.0)
    ev("cluster.flagged", run, components=6, largest=3, pairs_flagged=12,
       examples=[{"component": "A:A1000001", "size": 3, "pairs": 2}])
    ev("panel.voted", run, unanimous_merge=26180, split=71, unanimous_reject=14949,
       per_class={"EXACT_EMAIL": {"merge": 21020, "split": 0}, "PHONE_ADDRESS": {"merge": 3110, "split": 0},
                  "FUZZY_NAME": {"merge": 847, "split": 71}, "NEW_CUSTOMERS": {"merge": 4210, "split": 0},
                  "INTERNAL_DEDUP": {"merge": 588, "split": 9}})

    fp0 = "a3f9c1d27e5b44f08c9a1e6d3b2f7c21aa90b4d1e2f3a4b5c6d7e8f9a0b1c2d3"
    ev("golden.fingerprint", run, table="GOLDEN.CUSTOMERS", fingerprint=fp0, rows=36600)

    # EXACT_EMAIL: merges clean
    ev("hypothesis.stated", run, match_class="EXACT_EMAIL", expected_count=21020,
       rationale="identical normalised email on both sides")
    br1 = "BR_1A2B3C4D"
    ev("branch.opened", run, br1, defect_class="EXACT_EMAIL", purpose="apply EXACT_EMAIL decisions", schema=br1)
    ev("branch.materialised", run, br1, table="GOLDEN.CUSTOMERS", rows=36600, copy_ms=184.0, base_fingerprint=fp0,
       key_mode="KEYED")
    ev("branch.op", run, br1, op_id=1, status="OK", rows_affected=21020, tables_written=["GOLDEN.CUSTOMERS"],
       rationale="enrich golden rows with their EXACT_EMAIL match", sql="MERGE INTO GOLDEN.CUSTOMERS g USING (...) m ON ...")
    rows1 = [card(p[0], p[1], panel="UNANIMOUS_MERGE",
                  votes={"deterministic": "MERGE", "probabilistic": "MERGE", "skeptic": "MERGE"},
                  sig={"email": 1000, "phone": 1000, "name": 1000, "addr": 1000, "dob": 1000, "dob_gap_years": 0,
                       "suffix_conflict": 0, "business_conflict": 0, "given_initial_conflict": 0, "same_family": 1})
             for p in matches[:6]]
    ev("diff.computed", run, br1, dt_s=0.9, table="GOLDEN.CUSTOMERS", mode="KEYED", added=0, changed=21020, deleted=0,
       unchanged=15580, columns_touched={"SOURCE_B_REF": 21020, "MERGED_AT": 21020, "SURVIVORSHIP": 21020,
                                         "ADDR_LINE": 2210, "EMAIL": 0},
       column_order=["GOLDEN_ID", "FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "COUNTRY",
                     "DATE_OF_BIRTH", "SOURCE_A_REF", "SOURCE_B_REF", "MERGED_A_REFS", "MERGED_AT", "SURVIVORSHIP"],
       rows=rows1, rows_total=21020, base_drifted=False, diff_ms=612.0, defaults={"needs_review": 0})
    ev("selfcheck", run, br1, expected=21020, observed=21020, discrepancy=0, confidence_before=0.99,
       confidence_after=0.99, note="observed exactly the published count")
    ev("merge.requested", run, br1, merge_id=1, confidence=0.99, rationale="21,020 rows observed, 21,020 expected",
       total_changed=21020)
    ev("merge.gated", run, br1, merge_id=1, decision="MERGE", reason="WITHIN_TIER", risk=147.1, tier=2,
       limits={"max_changed": 30000, "max_risk": 200, "max_delete_pct": 1.0, "hard_delete_pct": 5.0,
               "min_confidence": 0.0, "threshold": 29700.0, "delete_pct": 0.0, "total_changed": 21020, "confidence": 0.99},
       rows_needing_review=0)
    fp1 = "7c21e0b4d1e2f3a4b5c6d7e8f9a0b1c2d3a3f9c1d27e5b44f08c9a1e6d3b2f00"
    ev("merge.applied", run, br1, dt_s=1.2, merge_id=1, rows_applied=21020, rows_excluded=0, stage_ms=231.0, swap_ms=9.0,
       pre_fingerprint=fp0, post_fingerprint=fp1)
    ev("golden.fingerprint", run, table="GOLDEN.CUSTOMERS", fingerprint=fp1, rows=36600)

    # FUZZY_NAME: the one that bounces
    ev("hypothesis.stated", run, match_class="FUZZY_NAME", expected_count=812,
       rationale="name similarity with weak corroboration")
    br3 = "BR_9C1E77F0"
    ev("branch.opened", run, br3, defect_class="FUZZY_NAME", purpose="apply FUZZY_NAME decisions", schema=br3)
    ev("branch.materialised", run, br3, table="GOLDEN.CUSTOMERS", rows=36600, copy_ms=179.0, base_fingerprint=fp1,
       key_mode="KEYED")
    ev("branch.op", run, br3, op_id=4, status="OK", rows_affected=847, tables_written=["GOLDEN.CUSTOMERS"],
       rationale="enrich golden rows with their FUZZY_NAME match", sql="MERGE INTO GOLDEN.CUSTOMERS g USING (...) m ON ...")
    smith_sig = {"email": 0, "phone": 1000, "name": 1000, "addr": 1000, "dob": 0, "dob_gap_years": 28,
                 "suffix_conflict": 1, "business_conflict": 0, "given_initial_conflict": 0, "same_family": 1}
    prec = [{"id": 14, "verdict": "REJECTED", "note": "father and son — never merge on shared address alone",
             "by": "param", "at": "2026-09-12 14:02:00"}]
    rows3 = [card(smith[0], smith[1], panel="SPLIT", verdict="MERGE", ai="DIFFERENT_PERSON", approved=False,
                  reason="PANEL_SPLIT", precedents=prec,
                  votes={"deterministic": "MERGE", "probabilistic": "MERGE", "skeptic": "REJECT"}, sig=smith_sig)]
    for kind in ("SPOUSE_SHARED_CONTACT", "RECYCLED_EMAIL"):
        p = decoys[kind][0]
        rows3.append(card(p[0], p[1], panel="SPLIT", ai="DIFFERENT_PERSON", approved=False, reason="PANEL_SPLIT",
                          votes={"deterministic": "MERGE", "probabilistic": "REJECT", "skeptic": "REJECT"},
                          sig={"email": 1000 if kind == "RECYCLED_EMAIL" else 0, "phone": 1000 if kind != "RECYCLED_EMAIL" else 0,
                               "name": 420, "addr": 1000 if kind != "RECYCLED_EMAIL" else 0, "dob": 0, "dob_gap_years": 3,
                               "suffix_conflict": 0, "business_conflict": 0, "given_initial_conflict": 1, "same_family": 1}))
    for p in matches[6:12]:
        rows3.append(card(p[0], p[1], panel="SPLIT", ai="SAME_PERSON", approved=False, reason="PANEL_SPLIT",
                          votes={"deterministic": "REJECT", "probabilistic": "MERGE", "skeptic": "MERGE"},
                          sig={"email": 600, "phone": 0, "name": 910, "addr": 800, "dob": 1000, "dob_gap_years": 0,
                               "suffix_conflict": 0, "business_conflict": 0, "given_initial_conflict": 0, "same_family": 1}))
    ev("diff.computed", run, br3, dt_s=0.8, table="GOLDEN.CUSTOMERS", mode="KEYED", added=0, changed=847, deleted=0,
       unchanged=35753, columns_touched={"SOURCE_B_REF": 847, "MERGED_AT": 847, "SURVIVORSHIP": 847, "ADDR_LINE": 402,
                                         "EMAIL": 212, "DATE_OF_BIRTH": 3},
       column_order=["GOLDEN_ID", "FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "COUNTRY",
                     "DATE_OF_BIRTH", "SOURCE_A_REF", "SOURCE_B_REF", "MERGED_A_REFS", "MERGED_AT", "SURVIVORSHIP"],
       rows=rows3, rows_total=847, base_drifted=True, diff_ms=410.0,
       defaults={"needs_review": 71, "panel_split": 71, "unexplained": 0})
    ev("selfcheck", run, br3, expected=812, observed=847, discrepancy=35, confidence_before=0.8, confidence_after=0.6,
       note="expected 812 rows, the diff shows 847 — 35 more — and DATE_OF_BIRTH changed on 3 rows I did not intend")
    ev("merge.requested", run, br3, merge_id=3, confidence=0.6, rationale="847 observed vs 812 expected; lowered confidence",
       total_changed=847)
    ev("merge.gated", run, br3, merge_id=3, decision="PENDING", reason="RISK (237.2 > 200.0)", risk=237.2, tier=2,
       limits={"max_changed": 30000, "max_risk": 200, "max_delete_pct": 1.0, "hard_delete_pct": 5.0,
               "min_confidence": 0.0, "threshold": 18000.0, "delete_pct": 0.0, "total_changed": 847, "confidence": 0.6},
       rows_needing_review=71)
    ev("agent.note", run, text="FUZZY_NAME is held for review: 71 pairs the matchers disagreed on start unchecked.",
       phase="plan")
    ev("rows.deselected", run, br3, dt_s=3.0, keys=[r["key"] for r in rows3[3:]], approved=True, approved_count=824,
       total=847)
    ev("precedent.recorded", run, precedent_id=15, verdict="REJECTED", defect_class="FUZZY_NAME",
       note="father and son — never merge on shared address alone")
    ev("merge.applied", run, br3, dt_s=1.0, merge_id=3, rows_applied=824, rows_excluded=23, stage_ms=226.0, swap_ms=8.0,
       pre_fingerprint=fp1, post_fingerprint="d27e5b44f08c9a1e6d3b2f7c21aa90b4d1e2f3a4b5c6d7e8f9a0b1c2d3a3f9c1")

    # a discarded branch and an unmerge, so the strip and the animations have something to show
    br4 = "BR_5EED0042"
    ev("branch.opened", run, br4, defect_class="PHONE_ADDRESS", purpose="exploratory: broader phone rule", schema=br4)
    ev("branch.discarded", run, br4, reason="rule too broad; restarting with postcode corroboration", drop_ms=11.0)
    ev("unmerge.applied", run, br3, dt_s=1.5, merge_id=3, fingerprint_match=True, restored_fingerprint=fp1,
       expected_fingerprint=fp1)
    ev("policy.adapted", run, min_confidence_before=0.0, min_confidence_after=0.05, reason="merge rejected: WRONG_MATCH")

    ev("score.updated", "fixture-control", precision=0.981, recall=0.943, f1=0.962, false_merges_in_golden=23,
       decoy_false_merges=19, detail={"illustrative": True})
    ev("score.updated", run, precision=0.981, recall=0.943, f1=0.962, false_merges_in_golden=0, decoy_false_merges=0,
       detail={"illustrative": True})
    ev("run.ended", run, status="OK", summary={"illustrative": True})

    OUT.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in out))
    print(f"wrote {len(out)} events to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
