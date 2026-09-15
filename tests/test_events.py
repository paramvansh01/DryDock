"""The event contract. Any change to drydock/events.py must keep these green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drydock import events

FIXTURE = Path(__file__).parent / "fixtures" / "run_replay.jsonl"
FROZEN_TYPES = {
    "run.started", "run.ended", "agent.note", "profile.found", "mapping.declared", "hypothesis.stated",
    "candidates.ready", "panel.voted", "adjudicating", "adjudicated", "precedent.cited", "precedent.recorded",
    "cluster.flagged", "branch.opened", "branch.materialised", "branch.op", "branch.blocked", "selfcheck",
    "diff.computed", "merge.requested", "merge.gated", "rows.deselected", "merge.applied", "merge.rejected",
    "branch.discarded", "branch.expired", "unmerge.applied", "policy.adapted", "score.updated", "golden.fingerprint",
    "db.snapshot", "dataset.activated",
}


def test_contract_is_frozen():
    assert set(events.EVENT_TYPES) == FROZEN_TYPES


def test_fixture_round_trips_through_the_contract():
    evs = events.read_jsonl(FIXTURE)
    assert len(evs) > 30
    for ev in evs:
        assert json.loads(json.dumps(ev)) == events.validate(json.loads(json.dumps(ev)))


def test_fixture_announces_itself_as_illustrative():
    evs = events.read_jsonl(FIXTURE)
    assert any(e["type"] == "run.started" and e["payload"]["mode"] == "replay-fixture" for e in evs)
    assert any(e["type"] == "agent.note" and "ILLUSTRATIVE" in e["payload"]["text"] for e in evs)


def test_json_schema_covers_every_type():
    s = events.json_schema()
    consts = {v["properties"]["type"]["const"] for v in s["oneOf"]}
    assert consts == FROZEN_TYPES


def test_emit_validates_and_reaches_sinks():
    got = []
    events.clear_sinks()
    events.add_sink(got.append)
    events.emit("branch.discarded", "r1", "BR_X", reason="test")
    events.clear_sinks()
    assert got and got[0]["type"] == "branch.discarded" and got[0]["payload"]["reason"] == "test"


@pytest.mark.parametrize("bad", [
    {"type": "nope", "ts": "x", "run_id": None, "branch_id": None, "payload": {}},
    {"type": "branch.discarded", "ts": "x", "run_id": None, "branch_id": None, "payload": {}},
    {"type": "branch.discarded", "ts": "x", "run_id": None, "branch_id": None, "payload": {"reason": 5}},
    {"type": "branch.discarded", "ts": "x", "run_id": None, "branch_id": None, "payload": {"reason": "a", "extra": 1}},
    {"type": "branch.discarded", "ts": "x", "run_id": None, "payload": {"reason": "a"}},
])
def test_invalid_events_rejected(bad):
    with pytest.raises(events.EventError):
        events.validate(bad)


def test_a_failing_sink_never_breaks_the_write_path():
    events.clear_sinks()
    def boom(_ev):
        raise RuntimeError("sink down")
    events.add_sink(boom)
    events.emit("agent.note", "r1", None, text="still fine")
    events.clear_sinks()


def test_dataset_activated_carries_what_the_ui_shows():
    ev = events.emit("dataset.activated", None, None, name="upload", label="crm.csv + shop.csv", rows_a=10,
                     rows_b=8, golden_rows=10, scored=False, files={"a": "crm.csv", "b": "shop.csv"}, quality=None)
    assert ev["payload"]["name"] == "upload" and ev["payload"]["scored"] is False
    with pytest.raises(events.EventError):
        events.validate({**ev, "payload": {**ev["payload"], "rows_a": "ten"}})
