"""Adjudication of the pairs the matcher panel split on.

Adjudicators, recorded per decision in ADJUDICATIONS.ADJUDICATOR:
  gemini_fallback      Google Gemini API, batched (default)
  exasol_ai            in-database model
  exasol_ai_precedent  in-database model with precedent retrieval
The in-database options run only after scripts/ladder.py has verified a deployed model.

The Gemini adjudicator receives, per pair, a non-identifying comparison summary
(e.g. "same surname; generational suffix conflict; dates of birth 28 years apart")
and the retrieved precedents with reviewer notes. It never receives names, emails,
phone numbers, addresses, dates or record ids. Reviewer notes are sent as written.
"""

from __future__ import annotations

import hashlib
import json
import time

from . import events, precedent
from .config import SETTINGS, require_verified
from .db import Db, lit

VERDICTS = ("SAME_PERSON", "DIFFERENT_PERSON", "INSUFFICIENT_EVIDENCE")
# Pairs per request. Larger batches mean fewer requests (the Gemini free tier allows 20 per day per model).
# 150 summaries is a few tens of thousands of tokens against a 1,048,576-token window, and 150 decisions fit
# well within the 65,536-token output limit.
BATCH = SETTINGS.adj_batch
UNAVAILABLE = "gemini_unavailable"

SYSTEM = """You adjudicate whether two customer records from different systems describe the same real person.
You never see the records themselves, only a comparison summary of how their fields relate, plus precedent:
past decisions by this organisation's human reviewers on similar comparisons, with their notes.

Precedent is binding guidance when it clearly applies: if reviewers rejected a comparison pattern
(for example shared address and surname but dates of birth decades apart, which is a parent and child),
reject the same pattern. When evidence is thin or contradictory, answer INSUFFICIENT_EVIDENCE rather than guess.
A false merge silently destroys two people's histories; a missed merge only leaves a duplicate. Weigh that asymmetry."""

SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair_id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "confidence": {"type": "number"},
                    "cited_precedents": {"type": "array", "items": {"type": "integer"}},
                    "reason": {"type": "string"},
                },
                "required": ["pair_id", "verdict", "confidence", "cited_precedents", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


def render_item(item: dict, precs: list[dict]) -> str:
    lines = [f"PAIR {item['pair_id']} ({'two records inside the same system' if item.get('kind') == 'AA' else 'one record from each system'})",
             f"  comparison: {item['summary']}"]
    if precs:
        lines.append("  precedent:")
        for p in precs:
            note = f" — reviewer note: \"{p['note']}\"" if p.get("note") else ""
            lines.append(f"    #{p['id']} {p['verdict']}: {p['context']}{note}")
    else:
        lines.append("  precedent: none on record")
    return "\n".join(lines)


def adjudicate(db: Db, run_id: str, items: list[dict], rung: str | None = None, k: int = 3) -> dict[int, dict]:
    """items: [{pair_id, kind, summary}]. Returns pair_id -> {verdict, score, adjudicator, precedents, rationale}."""
    rung = rung or SETTINGS.adjudicator_rung
    if not items:
        return {}
    events.emit("adjudicating", run_id, None, pairs=len(items), adjudicator=rung)
    t0 = time.perf_counter()
    if rung == "gemini_fallback":
        out = _gemini(db, run_id, items, k)
    elif rung in ("exasol_ai", "exasol_ai_precedent"):
        out = _exasol_ai(db, run_id, items, k, with_precedent=(rung == "exasol_ai_precedent"))
    else:
        raise ValueError(f"unknown adjudicator rung {rung!r}")
    ms = (time.perf_counter() - t0) * 1000
    counts = {v: sum(1 for d in out.values() if d["verdict"] == v) for v in VERDICTS}
    events.emit("adjudicated", run_id, None, pairs=len(out), adjudicator=rung, verdicts=counts, latency_ms=round(ms, 1))
    return out


def _record(db: Db, run_id: str, pid: int, text: str, d: dict, adjudicator: str, ms: float) -> None:
    db.run("INSERT INTO DRYDOCK.ADJUDICATIONS (RUN_ID, PAIR_ID, INPUT_TEXT, PRECEDENTS_USED, VERDICT, SCORE, "
           f"ADJUDICATOR, LATENCY_MS) VALUES ({lit(run_id)}, {int(pid)}, {lit(text[:2000])}, "
           f"{lit(json.dumps(d.get('precedents', [])))}, {lit(d['verdict'])}, {lit(d.get('score'))}, "
           f"{lit(adjudicator)}, {int(ms)})")


def _is_quota(e: Exception) -> bool:
    """A provider refusal we degrade on rather than abort: quota/rate limit, or the service being down.
    Anything else (a bad request, a bad key) is a real fault and must surface."""
    code = getattr(e, "code", None) or getattr(getattr(e, "response", None), "status_code", None)
    return code in (429, 503) or "RESOURCE_EXHAUSTED" in str(e) or "UNAVAILABLE" in str(e)


def _gemini(db: Db, run_id: str, items: list[dict], k: int) -> dict[int, dict]:
    """Google Gemini, batched, JSON-schema structured output, minimised input (see module docstring)."""
    from . import llm

    out: dict[int, dict] = {}
    quota_error: str | None = None      # set once the provider refuses; the run continues, escalating
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        precs = {it["pair_id"]: precedent.retrieve(db, it["summary"], k=k) for it in batch}
        texts = {it["pair_id"]: render_item(it, precs[it["pair_id"]]) for it in batch}
        prompt = ("Adjudicate each pair below. Return one decision per PAIR id.\n\n" +
                  "\n\n".join(texts[it["pair_id"]] for it in batch))
        t0 = time.perf_counter()
        if quota_error is None:
            try:
                parsed, finish = llm.generate_json(SETTINGS.adjudicator_model, SYSTEM, prompt, SCHEMA,
                                                   SETTINGS.adjudicator_thinking)
            except Exception as e:                      # provider refused (quota, auth, outage)
                if not _is_quota(e):
                    raise
                quota_error = str(e)[:300]
                events.emit("agent.note", run_id, None, phase="adjudicate",
                            text=f"adjudicator unavailable ({UNAVAILABLE}): {quota_error}. "
                                 "Remaining split pairs are left INSUFFICIENT_EVIDENCE, i.e. for a person.")
                parsed, finish = None, "PROVIDER_UNAVAILABLE"
        else:
            parsed, finish = None, "PROVIDER_UNAVAILABLE"
        ms = (time.perf_counter() - t0) * 1000 / max(1, len(batch))
        if parsed is None and quota_error is not None:
            decisions = [{"pair_id": it["pair_id"], "verdict": "INSUFFICIENT_EVIDENCE", "confidence": 0.0,
                          "cited_precedents": [],
                          "reason": f"adjudicator unavailable ({quota_error}); left for a human"} for it in batch]
        elif parsed is None:
            decisions = [{"pair_id": it["pair_id"], "verdict": "INSUFFICIENT_EVIDENCE", "confidence": 0.0,
                          "cited_precedents": [], "reason": f"adjudicator returned no decision ({finish}); left for a human"}
                         for it in batch]
        else:
            decisions = parsed.get("decisions", [])
        by_id = {int(d["pair_id"]): d for d in decisions}
        for it in batch:
            pid = it["pair_id"]
            d = by_id.get(pid) or {"verdict": "INSUFFICIENT_EVIDENCE", "confidence": 0.0, "cited_precedents": [],
                                   "reason": "no decision returned for this pair"}
            known = {p["id"] for p in precs[pid]}
            cited = [int(c) for c in d.get("cited_precedents", []) if int(c) in known]
            who = UNAVAILABLE if quota_error is not None else "gemini_fallback"
            res = {"verdict": d["verdict"] if d["verdict"] in VERDICTS else "INSUFFICIENT_EVIDENCE",
                   "score": round(float(d.get("confidence") or 0), 4), "adjudicator": who,
                   "precedents": [p["id"] for p in precs[pid]], "cited": cited, "rationale": d.get("reason", "")}
            _record(db, run_id, pid, texts[pid], res, who, ms)
            if precs[pid]:
                events.emit("precedent.cited", run_id, None, pair_id=pid, precedent_ids=[p["id"] for p in precs[pid]],
                            verdict=res["verdict"], score=res["score"],
                            notes=[p["note"] for p in precs[pid] if p.get("note")])
            out[pid] = res
    return out


HYPOTHESIS = "The two records describe the same real person."
NLI_TO_VERDICT = {"ENTAILMENT": "SAME_PERSON", "CONTRADICTION": "DIFFERENT_PERSON", "NEUTRAL": "INSUFFICIENT_EVIDENCE"}


def ai_config() -> dict:
    """Where the in-database model lives. Every value comes from the environment, set from what
    scripts/ladder.py deployed."""
    import os
    cfg = {"schema": os.environ.get("DRYDOCK_AI_SCHEMA", ""), "conn": os.environ.get("DRYDOCK_AI_BUCKETFS_CONN", ""),
           "sub_dir": os.environ.get("DRYDOCK_AI_SUBDIR", ""), "model": os.environ.get("DRYDOCK_AI_MODEL", "")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise RuntimeError(f"in-database adjudicator not configured ({', '.join(missing)}): set the DRYDOCK_AI_* values "
                           "that scripts/ladder.py b6 recorded. Until then only gemini_fallback can run.")
    return cfg


def entailment_sql(cfg: dict, input_table: str) -> str:
    """AI_ENTAILMENT_EXTENDED is a SET script that EMITS rows (transformers-extension 5.2.0
    resources/templates/ai_entailment_extended_udf.jinja.sql): one call over the whole input table.
    first_text = comparison summary (+ precedent block when enabled), second_text = the fixed hypothesis."""
    from .db import ident
    return (f"SELECT {ident(cfg['schema'])}.AI_ENTAILMENT_EXTENDED(NULL, {lit(cfg['conn'])}, {lit(cfg['sub_dir'])}, "
            f"{lit(cfg['model'])}, t.FIRST_TEXT, t.SECOND_TEXT, 'HIGHEST') FROM {input_table} t")


def _pick(row: dict, *names: str):
    """Output column names differ between the 5.2.0 template (label, score) and the user guide
    (_LABEL_, _SCORE_); ladder step b6 records which one this instance emits. Accept only these."""
    for n in names:
        for k, v in row.items():
            if k.upper() == n:
                return v
    raise KeyError(f"none of {names} in emitted columns {sorted(row)}")


def _exasol_ai(db: Db, run_id: str, items: list[dict], k: int, with_precedent: bool) -> dict[int, dict]:
    """The in-database NLI model over BucketFS. Runs only when V1, V12 and ladder step b6 are signed PASS."""
    require_verified("V1", "V12", "B6")
    cfg = ai_config()
    tbl = f"DRYDOCK.ADJ_INPUT_{hashlib.sha256(run_id.encode()).hexdigest()[:10].upper()}"
    db.run(f"CREATE OR REPLACE TABLE {tbl} (PAIR_ID DECIMAL(18,0), FIRST_TEXT VARCHAR(2000000), SECOND_TEXT VARCHAR(2000))")
    texts, precs_by = {}, {}
    for it in items:
        precs = precedent.retrieve(db, it["summary"], k=k) if with_precedent else []
        precs_by[it["pair_id"]] = precs
        texts[it["pair_id"]] = render_item(it, precs)
    db.import_rows(([pid, t, HYPOTHESIS] for pid, t in texts.items()), "DRYDOCK", tbl.split(".", 1)[1])
    t0 = time.perf_counter()
    rows = db.dicts(entailment_sql(cfg, tbl))
    ms = (time.perf_counter() - t0) * 1000 / max(1, len(items))
    db.run(f"DROP TABLE {tbl}")
    by_text = {t: pid for pid, t in texts.items()}
    adjudicator = "exasol_ai_precedent" if with_precedent else "exasol_ai"
    out = {}
    for r in rows:
        pid = by_text.get(_pick(r, "FIRST_TEXT"))
        if pid is None:
            continue
        err = _pick(r, "ERROR_MESSAGE", "_ERROR_MESSAGE_")
        label = str(_pick(r, "LABEL", "_LABEL_") or "").upper()
        verdict = "INSUFFICIENT_EVIDENCE" if err else NLI_TO_VERDICT.get(label, "INSUFFICIENT_EVIDENCE")
        res = {"verdict": verdict, "score": float(_pick(r, "SCORE", "_SCORE_") or 0), "adjudicator": adjudicator,
               "precedents": [p["id"] for p in precs_by[pid]], "rationale": f"NLI {label or 'ERROR: ' + str(err)}"}
        _record(db, run_id, pid, texts[pid], res, adjudicator, ms)
        out[pid] = res
    for it in items:   # a pair the model returned nothing for is NOT silently decided
        out.setdefault(it["pair_id"], {"verdict": "INSUFFICIENT_EVIDENCE", "score": 0.0, "adjudicator": adjudicator,
                                       "precedents": [], "rationale": "no model output for this pair"})
    return out
