"""Event contract.

    agent step -> emit() -> sinks (JSONL file, DRYDOCK.EVENTS, orchestrator) -> WebSocket -> UI

Every event is {type, ts, run_id, branch_id, payload}. The UI is a pure function of
this stream, so any run can be replayed. tests/test_events.py pins the set of types.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

NUM = (int, float)
OPT = object()  # marker: key optional

# type -> {payload key: python type(s)}; keys listed are REQUIRED unless wrapped in Opt.


class Opt:
    def __init__(self, *types):
        self.types = types


SCHEMAS: dict[str, dict[str, Any]] = {
    "run.started":        {"tier": int, "gate_enabled": bool, "mode": str, "seed": int,
                           "min_confidence": NUM, "plan_from_run": Opt(str, type(None))},
    "run.ended":          {"status": str, "summary": Opt(dict)},
    "agent.note":         {"text": str, "phase": Opt(str)},
    "profile.found":      {"source": str, "table": str, "finding": str, "column": Opt(str),
                           "stats": Opt(dict)},
    "mapping.declared":   {"mapping": dict, "rationale": Opt(str)},
    "hypothesis.stated":  {"match_class": str, "expected_count": int, "rationale": str},
    "candidates.ready":   {"kind": str, "possible_pairs": int, "candidates": int,
                           "reduction_ratio": NUM, "per_rule": dict, "ms": NUM},
    "panel.voted":        {"unanimous_merge": int, "split": int, "unanimous_reject": int,
                           "per_class": dict},
    "adjudicating":       {"pairs": int, "adjudicator": str},
    "adjudicated":        {"pairs": int, "adjudicator": str, "verdicts": dict, "latency_ms": NUM},
    "precedent.cited":    {"pair_id": int, "precedent_ids": list, "verdict": str,
                           "score": Opt(*NUM), "notes": Opt(list)},
    "precedent.recorded": {"precedent_id": int, "verdict": str, "defect_class": str,
                           "note": Opt(str, type(None))},
    "cluster.flagged":    {"components": int, "largest": int, "pairs_flagged": int,
                           "examples": Opt(list)},
    "branch.opened":      {"defect_class": str, "purpose": str, "schema": str},
    "branch.materialised": {"table": str, "rows": int, "copy_ms": NUM, "base_fingerprint": str,
                            "key_mode": str},
    "branch.op":          {"op_id": int, "status": str, "rows_affected": int, "tables_written": list,
                           "rationale": str, "sql": str, "error": Opt(str, type(None))},
    "branch.blocked":     {"code": str, "message": str, "sql": str},
    "selfcheck":          {"expected": int, "observed": int, "discrepancy": int,
                           "confidence_before": NUM, "confidence_after": NUM, "note": str},
    "diff.computed":      {"table": str, "mode": str, "added": int, "changed": int, "deleted": int,
                           "unchanged": int, "columns_touched": dict, "column_order": list,
                           "rows": list, "rows_total": int, "base_drifted": bool, "diff_ms": NUM,
                           "defaults": Opt(dict)},
    "merge.requested":    {"merge_id": int, "confidence": NUM, "rationale": str, "total_changed": int},
    "merge.gated":        {"merge_id": int, "decision": str, "reason": str, "risk": NUM, "tier": int,
                           "limits": dict, "rows_needing_review": int},
    "rows.deselected":    {"keys": list, "approved": bool, "approved_count": int, "total": int},
    "merge.applied":      {"merge_id": int, "rows_applied": int, "rows_excluded": int,
                           "stage_ms": NUM, "swap_ms": NUM, "pre_fingerprint": str,
                           "post_fingerprint": str},
    "merge.rejected":     {"merge_id": int, "code": str, "note": Opt(str, type(None))},
    "branch.discarded":   {"reason": str, "drop_ms": Opt(*NUM)},
    "branch.expired":     {"age_seconds": NUM},
    "unmerge.applied":    {"merge_id": int, "fingerprint_match": bool, "restored_fingerprint": str,
                           "expected_fingerprint": str},
    "policy.adapted":     {"min_confidence_before": NUM, "min_confidence_after": NUM, "reason": str},
    "score.updated":      {"precision": NUM, "recall": NUM, "f1": NUM, "false_merges_in_golden": int,
                           "decoy_false_merges": int, "detail": Opt(dict)},
    "golden.fingerprint": {"table": str, "fingerprint": str, "rows": int},
    # GOLDEN now holds another dataset (the demo, or a person's own two files). Everything before it in the
    # stream was cleared with the run state it described; the orchestrator tells browsers to reset first.
    "dataset.activated":  {"name": str, "label": str, "rows_a": int, "rows_b": int, "golden_rows": int,
                           "scored": bool, "files": Opt(dict, type(None)), "quality": Opt(dict, type(None))},
    # What Exasol reports right now, for the Live System view. Broadcast to the UI only, never persisted
    # (it would count its own inserts into DRYDOCK.EVENTS).
    "db.snapshot":        {"db_time": str, "session": str, "version": str, "tables": list, "columns": dict,
                           "keys": list, "config": dict, "ms": NUM, "trigger": str},
}

EVENT_TYPES = tuple(SCHEMAS)


class EventError(ValueError):
    pass


def _check(t, v) -> bool:
    if isinstance(t, tuple):
        return any(_check(x, v) for x in t)
    if t is int:
        return isinstance(v, int) and not isinstance(v, bool)
    if t is float:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, t)


def validate(event: dict) -> dict:
    for k in ("type", "ts", "run_id", "branch_id", "payload"):
        if k not in event:
            raise EventError(f"event missing {k!r}")
    et = event["type"]
    if et not in SCHEMAS:
        raise EventError(f"unknown event type {et!r}")
    if not isinstance(event["payload"], dict):
        raise EventError("payload must be an object")
    for key, t in SCHEMAS[et].items():
        opt = isinstance(t, Opt)
        types = t.types if opt else (t if isinstance(t, tuple) else (t,))
        if key not in event["payload"]:
            if opt:
                continue
            raise EventError(f"{et}: payload missing required {key!r}")
        if not _check(tuple(types), event["payload"][key]):
            raise EventError(f"{et}: payload[{key!r}] has type {type(event['payload'][key]).__name__}")
    extra = set(event["payload"]) - set(SCHEMAS[et])
    if extra:
        raise EventError(f"{et}: unexpected payload keys {sorted(extra)}")
    json.dumps(event)  # must be JSON-serialisable
    return event


def json_schema() -> dict:
    """A JSON Schema (draft-07) of the whole contract, for the UI and the round-trip test."""
    def js(t):
        if isinstance(t, Opt):
            return {"anyOf": [js(x) for x in t.types]}
        if isinstance(t, tuple):
            return {"anyOf": [js(x) for x in t]}
        return {int: {"type": "integer"}, float: {"type": "number"}, str: {"type": "string"},
                bool: {"type": "boolean"}, dict: {"type": "object"}, list: {"type": "array"},
                type(None): {"type": "null"}}[t]
    variants = []
    for et, keys in SCHEMAS.items():
        variants.append({
            "type": "object",
            "properties": {
                "type": {"const": et}, "ts": {"type": "string"},
                "run_id": {"type": ["string", "null"]}, "branch_id": {"type": ["string", "null"]},
                "payload": {"type": "object", "additionalProperties": False,
                            "properties": {k: js(t) for k, t in keys.items()},
                            "required": [k for k, t in keys.items() if not isinstance(t, Opt)]},
            },
            "required": ["type", "ts", "run_id", "branch_id", "payload"],
        })
    return {"$schema": "http://json-schema.org/draft-07/schema#", "oneOf": variants}


@dataclass
class Event:
    type: str
    run_id: str | None
    branch_id: str | None
    payload: dict = field(default_factory=dict)
    ts: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"))

    def to_dict(self) -> dict:
        d = asdict(self)
        return {"type": d["type"], "ts": d["ts"], "run_id": d["run_id"],
                "branch_id": d["branch_id"], "payload": d["payload"]}


# ------------------------------------------------------------------ bus

Sink = Callable[[dict], None]
_sinks: list[Sink] = []
_lock = threading.Lock()


def add_sink(fn: Sink) -> None:
    with _lock:
        _sinks.append(fn)


def clear_sinks() -> None:
    with _lock:
        _sinks.clear()


def emit(type: str, run_id: str | None = None, branch_id: str | None = None, **payload) -> dict:
    ev = validate(Event(type, run_id, branch_id, _jsonable(payload)).to_dict())
    with _lock:
        sinks = list(_sinks)
    for s in sinks:
        try:
            s(ev)
        except Exception as e:  # a failing sink must never break the write path
            import sys
            print(f"[events] sink {getattr(s, '__name__', s)} failed: {e}", file=sys.stderr)
    return ev


def _jsonable(v):
    from decimal import Decimal
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def jsonl_sink(path: str | Path) -> Sink:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lk = threading.Lock()

    def sink(ev: dict) -> None:
        with lk, p.open("a") as f:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    sink.__name__ = f"jsonl:{p.name}"
    return sink


def http_sink(url: str) -> Sink:
    """Used by the Drydock MCP server process to forward events to the orchestrator."""
    import httpx

    client = httpx.Client(timeout=2.0)

    def sink(ev: dict) -> None:
        client.post(url, json=ev)
    sink.__name__ = f"http:{url}"
    return sink


def db_sink(db) -> Sink:
    from .db import lit

    def sink(ev: dict) -> None:
        body = json.dumps(ev, separators=(",", ":"))
        db.run("INSERT INTO DRYDOCK.EVENTS (RUN_ID, BRANCH_ID, TS, TYPE, BODY) VALUES ("
               f"{lit(ev['run_id'])}, {lit(ev['branch_id'])}, CURRENT_TIMESTAMP, {lit(ev['type'])}, {lit(body)})")
    sink.__name__ = "db:DRYDOCK.EVENTS"
    return sink


def read_jsonl(path: str | Path) -> list[dict]:
    return [validate(json.loads(line)) for line in Path(path).read_text().splitlines() if line.strip()]
