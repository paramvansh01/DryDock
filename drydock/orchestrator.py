"""Orchestrator: FastAPI app serving the UI, the event WebSocket, the reviewer REST actions
(approve, reject, deselect, unmerge, discard, tier), run control and the branch TTL sweeper.

    uv run uvicorn drydock.orchestrator:app --port 8765

The UI never talks to Exasol, the agent never writes Exasol directly, and every write to
GOLDEN goes through drydock/merge.py. GET /ws?replay=<path.jsonl> streams a recorded run,
which the UI labels as a replay.
"""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import branch, diff, er, events, gate, merge, precedent, system
from .config import REJECT_CODES, ROOT, SETTINGS, NotVerified
from .db import DbError, get, lit

HISTORY: list[dict] = []
CLIENTS: set[WebSocket] = set()
LOOP: asyncio.AbstractEventLoop | None = None
RUNS: dict[str, threading.Thread] = {}


def _broadcast(ev: dict) -> None:
    if ev["type"] == "db.snapshot":            # only the newest snapshot is worth replaying to a new browser
        HISTORY[:] = [e for e in HISTORY if e["type"] != "db.snapshot"]
    HISTORY.append(ev)
    if ev["type"] != "db.snapshot":
        _schedule_snapshot(f"after {ev['type']}")
    if LOOP is None:
        return
    for ws in list(CLIENTS):
        asyncio.run_coroutine_threadsafe(_send(ws, ev), LOOP)


# ------------------------------------------------------------------ live Exasol metadata (System view)
# After any event, re-read what Exasol reports (row counts, session, DB clock) so the System view's numbers track
# the database within about a second. Throttled: events that arrive while a snapshot is pending ride along with it.
_snap_lock = threading.Lock()
_snap_timer: threading.Timer | None = None


def _schedule_snapshot(trigger: str, delay: float = 0.8) -> None:
    global _snap_timer
    with _snap_lock:
        if _snap_timer is not None:
            return
        _snap_timer = threading.Timer(delay, _run_snapshot, args=(trigger,))
        _snap_timer.daemon = True
        _snap_timer.start()


def _run_snapshot(trigger: str) -> None:
    global _snap_timer
    with _snap_lock:
        _snap_timer = None
    try:
        _snapshot_now(trigger)
    except Exception as e:  # the diagram going stale must never break a run
        print(f"[snapshot] {type(e).__name__}: {e}")


def _snapshot_now(trigger: str) -> dict:
    ev = events.validate(events.Event("db.snapshot", None, None, system.snapshot(_db(), trigger)).to_dict())
    _broadcast(ev)
    return ev


async def _send(ws: WebSocket, ev: dict) -> None:
    try:
        await ws.send_text(json.dumps(ev))
    except Exception:
        CLIENTS.discard(ws)


def _db():
    return get("svc")


def _load_history(db) -> int:
    """Seed the in-memory history from DRYDOCK.EVENTS, so a browser that connects after a restart
    rebuilds the same state (the UI is a pure function of the event stream)."""
    for (body,) in db.rows("SELECT BODY FROM DRYDOCK.EVENTS ORDER BY EVENT_SEQ"):
        try:
            HISTORY.append(events.validate(json.loads(body)))
        except (ValueError, TypeError, KeyError):
            continue
    return len(HISTORY)


async def _sweeper():
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(branch.sweep_expired, _db())
        except Exception as e:  # never kill the sweeper; report it
            print(f"[sweeper] {type(e).__name__}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LOOP
    LOOP = asyncio.get_running_loop()
    events.clear_sinks()
    events.add_sink(events.jsonl_sink(SETTINGS.events_jsonl))
    events.add_sink(_broadcast)
    er.install_hooks()
    try:
        db = _db()
        print(f"[startup] {_load_history(db)} saved events will be replayed to each new browser connection")
        snap = _snapshot_now("startup")["payload"]
        print(f"[startup] Exasol {snap['version']} session {snap['session']}: {len(snap['tables'])} tables "
              f"in {snap['ms']} ms")
        events.add_sink(events.db_sink(db))
        notes = merge.recover(db)
        for n in notes:
            print(f"[recover] {n}")
    except Exception as e:
        print(f"[startup] database not reachable yet ({type(e).__name__}: {e}); UI replay still works")
    task = asyncio.create_task(_sweeper())
    yield
    task.cancel()


app = FastAPI(title="Drydock orchestrator", lifespan=lifespan)


@app.exception_handler(NotVerified)
async def _nv(_r, e: NotVerified):
    return JSONResponse({"ok": False, "error": "NOT_VERIFIED", "message": str(e)}, status_code=409)


@app.exception_handler(DbError)
async def _dbe(_r, e: DbError):
    return JSONResponse({"ok": False, "error": f"EXASOL_{e.code}", "message": e.message}, status_code=500)


@app.exception_handler(ValueError)
async def _ve(_r, e: ValueError):
    return JSONResponse({"ok": False, "error": "BAD_REQUEST", "message": str(e)}, status_code=400)


# ------------------------------------------------------------------ events

@app.post("/internal/events")
async def ingest(request: Request):
    """Events from the Drydock MCP server process. Validated against the frozen contract."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "local only")
    ev = events.validate(await request.json())
    for s in list(events._sinks):
        if s is not _broadcast and getattr(s, "__name__", "").startswith("jsonl"):
            s(ev)
    try:
        events.db_sink(_db())(ev)
    except Exception:
        pass
    _broadcast(ev)
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    replay = websocket.query_params.get("replay")
    if replay:
        path = (ROOT / replay).resolve()
        if ROOT not in path.parents or not path.exists():
            await websocket.close(code=4404)
            return
        delay = float(websocket.query_params.get("delay", "0.15"))
        await websocket.send_text(json.dumps({"type": "__replay__", "path": replay}))
        for ev in events.read_jsonl(path):
            await websocket.send_text(json.dumps(ev))
            await asyncio.sleep(delay)
        return
    CLIENTS.add(websocket)
    for ev in list(HISTORY):                    # a copy: other threads append while we send
        await websocket.send_text(json.dumps(ev))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        CLIENTS.discard(websocket)


# ------------------------------------------------------------------ human-only verbs

class Rows(BaseModel):
    keys: list[str]
    approved: bool


class Reject(BaseModel):
    code: str
    note: str | None = None


class Reason(BaseModel):
    reason: str = "discarded by reviewer"


class Tier(BaseModel):
    tier: int


@app.post("/branches/{branch_id}/rows")
def set_rows(branch_id: str, body: Rows):
    db = _db()
    info = db.dicts(f"SELECT RUN_ID, DEFECT_CLASS FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(branch_id)}")
    run_id = info[0]["RUN_ID"] if info else None
    keys = list(body.keys)
    if info and info[0]["DEFECT_CLASS"] == "INTERNAL_DEDUP" and run_id:
        # a dedup decision is ONE pair = two golden keys (kept + dropped); never approve half of it
        tag = er.run_tag(run_id)
        ks = ", ".join(lit(k) for k in keys)
        for keep, drop in db.rows(f"SELECT KEEP_GOLDEN_ID, DROP_GOLDEN_ID FROM ER_WORK.DEDUP_{tag} "
                                  f"WHERE KEEP_GOLDEN_ID IN ({ks}) OR DROP_GOLDEN_ID IN ({ks})"):
            keys += [keep, drop]
    return diff.set_approval(db, branch_id, sorted(set(keys)), body.approved, run_id)


def _rescore_for_merge(db, merge_id: int) -> None:
    """After a human decision the gate's number (false merges in GOLDEN) changes: re-score so the
    scoreboard reflects what is in GOLDEN now, not what it was when the run ended."""
    run_id = db.scalar(f"SELECT b.RUN_ID FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b ON b.BRANCH_ID = m.BRANCH_ID "
                       f"WHERE m.MERGE_ID = {int(merge_id)}")
    if run_id:
        from bench.score import score
        score(db, run_id)


@app.post("/merges/{merge_id}/approve")
def approve(merge_id: int, by: str = "human"):
    db = _db()
    st = db.scalar(f"SELECT b.STATUS FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b ON b.BRANCH_ID = m.BRANCH_ID "
                   f"WHERE m.MERGE_ID = {int(merge_id)}")
    if st != "OPEN":
        # checked BEFORE recording a precedent: a failed approval must not teach the adjudicator anything
        raise HTTPException(409, f"merge {merge_id} cannot be applied: its branch is {st}")
    precedent.record_for_merge(db, merge_id, by=by)
    out = merge.apply(db, merge_id, resolved_by=by)
    _rescore_for_merge(db, merge_id)
    return out


@app.post("/merges/{merge_id}/reject")
def reject(merge_id: int, body: Reject, by: str = "human"):
    if body.code not in REJECT_CODES:
        raise HTTPException(400, f"code must be one of {REJECT_CODES}")
    db = _db()
    _precedents_on_reject(db, merge_id, body)
    out = gate.reject(db, merge_id, body.code, body.note, by)
    _rescore_for_merge(db, merge_id)
    return out


def _precedents_on_reject(db, merge_id: int, body: Reject) -> None:
    """A whole-branch rejection is case law for the pairs the panel could not agree on."""
    m = db.dicts(f"SELECT m.BRANCH_ID, b.RUN_ID, b.DEFECT_CLASS FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b "
                 f"ON b.BRANCH_ID = m.BRANCH_ID WHERE m.MERGE_ID = {int(merge_id)}")
    if not m or not m[0]["RUN_ID"] or m[0]["DEFECT_CLASS"] not in er.MATCH_CLASSES:
        return
    rows = db.dicts(f"SELECT c.SIGNALS, c.A_ID, c.B_ID FROM DRYDOCK.CANDIDATES c WHERE c.RUN_ID = {lit(m[0]['RUN_ID'])} "
                    f"AND c.MATCH_CLASS = {lit(m[0]['DEFECT_CLASS'])} AND c.PANEL_RESULT = 'SPLIT' "
                    "AND c.VERDICT = 'MERGE' ORDER BY c.PAIR_ID LIMIT 20")
    for r in rows:
        precedent.record(db, defect_class=m[0]["DEFECT_CLASS"], context=er.compare_summary(json.loads(r["SIGNALS"])),
                         verdict="REJECTED", before=f"A {r['A_ID']}", after=f"B {r['B_ID']}",
                         reject_code=body.code, note=body.note, merge_id=merge_id, run_id=m[0]["RUN_ID"])


@app.post("/merges/{merge_id}/unmerge")
def unmerge(merge_id: int, by: str = "human"):
    out = merge.unmerge(_db(), merge_id, by)
    _rescore_for_merge(_db(), merge_id)
    return out


@app.post("/branches/{branch_id}/discard")
def discard(branch_id: str, body: Reason):
    return branch.discard_branch(_db(), branch_id, body.reason)


@app.put("/runs/{run_id}/tier")
def set_tier(run_id: str, body: Tier):
    _db().run(f"UPDATE DRYDOCK.RUNS SET TIER = {int(body.tier)} WHERE RUN_ID = {lit(run_id)}")
    return {"ok": True}


class NoteBody(BaseModel):
    note: str


@app.post("/precedents/{pid}/note")
def precedent_note(pid: int, body: NoteBody):
    precedent.add_note(_db(), pid, body.note)
    return {"ok": True}


@app.delete("/precedents/{pid}")
def precedent_delete(pid: int):
    precedent.delete(_db(), pid)
    return {"ok": True}


@app.post("/pairs/{pair_id}/readjudicate")
def readjudicate(pair_id: int):
    """The precedent demo beat: re-run adjudication for one pair with whatever case law exists NOW."""
    from . import adjudicate
    db = _db()
    r = db.dicts(f"SELECT RUN_ID, KIND, SIGNALS FROM DRYDOCK.CANDIDATES WHERE PAIR_ID = {int(pair_id)}")
    if not r:
        raise HTTPException(404)
    item = {"pair_id": int(pair_id), "kind": r[0]["KIND"], "summary": er.compare_summary(json.loads(r[0]["SIGNALS"]))}
    return adjudicate.adjudicate(db, r[0]["RUN_ID"], [item]).get(int(pair_id))


# ------------------------------------------------------------------ runs

class StartRun(BaseModel):
    run_id: str
    mode: str = "agent"               # agent | scripted | treatment
    gate_enabled: bool = True
    tier: int = 2
    seed: int = 20260913
    plan_from_run: str | None = None  # treatment: replay this run's frozen plan


@app.post("/runs")
def start_run(body: StartRun):
    if body.run_id in RUNS and RUNS[body.run_id].is_alive():
        raise HTTPException(409, "run already in progress")
    from agent import loop as agent_loop
    from agent import playbook

    def work():
        try:
            if body.mode == "agent":
                agent_loop.run(body.run_id, tier=body.tier, gate_enabled=body.gate_enabled, seed=body.seed)
            elif body.mode == "scripted":
                playbook.run_scripted(_db(), body.run_id, tier=body.tier, gate_enabled=body.gate_enabled, seed=body.seed)
            elif body.mode == "treatment":
                if not body.plan_from_run:
                    raise ValueError("treatment needs plan_from_run")
                playbook.replay_plan(_db(), body.plan_from_run, body.run_id, tier=body.tier,
                                     gate_enabled=body.gate_enabled, seed=body.seed)
            else:
                raise ValueError(body.mode)
        except Exception as e:
            traceback.print_exc()
            events.emit("run.ended", body.run_id, None, status="ERROR", summary={"error": f"{type(e).__name__}: {e}"})

    t = threading.Thread(target=work, daemon=True, name=f"run-{body.run_id}")
    RUNS[body.run_id] = t
    t.start()
    return {"ok": True, "run_id": body.run_id}


@app.get("/runs/{run_id}/report")
def report(run_id: str):
    from bench.score import score
    return score(_db(), run_id)


@app.get("/runs")
def runs():
    return _db().dicts("SELECT RUN_ID, STARTED_AT, ENDED_AT, TIER, GATE_ENABLED, MODE, MIN_CONFIDENCE, "
                       "PLAN_FROM_RUN, METRICS FROM DRYDOCK.RUNS ORDER BY STARTED_AT")


@app.get("/replays")
def replays():
    base = ROOT / "tests" / "fixtures"
    extra = ROOT / "runs"
    files = sorted([p for p in [*base.glob("*.jsonl"), *extra.glob("*.jsonl")]])
    return [str(p.relative_to(ROOT)) for p in files]


@app.post("/system/snapshot")
def system_snapshot():
    """Re-read Exasol now. The result arrives on the event stream like everything else."""
    p = _snapshot_now("refresh")["payload"]
    return {"ok": True, "db_time": p["db_time"], "session": p["session"], "ms": p["ms"]}


@app.get("/health")
def health():
    from .config import verified_status
    ids = ("V1", "V2", "V3", "V4", "V5", "V6", "V9", "V11", "V12", "V14", "L3")
    return {"verified": {c: verified_status(c) for c in ids}, "adjudicator": SETTINGS.adjudicator_rung}


_dist = ROOT / "ui" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(_dist / "index.html")
