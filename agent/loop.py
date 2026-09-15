"""The reconciliation agent. Gemini plans and chooses tools; it never writes directly.

    uv run python -m agent.loop --run-id demo1 --tier 2 [--gate-off]

Automatic function calling is disabled: this loop executes every tool call itself, so
each one is routed, recorded and disclosed. Two MCP servers over stdio:
  exasol_db  Exasol's official MCP server, as DRYDOCK_AGENT: read-only, and only
             SOURCE_A, SOURCE_B and GOLDEN_V are visible
  drydock    the Drydock MCP server, as DRYDOCK_SVC: the only write path
Three local narration tools (profile_finding, state_hypothesis, self_check) only emit events.

Every Drydock call is recorded to runs/<run_id>/plan.jsonl so an A/B treatment can replay
it. Rows returned to the model by execute_exasol_query are counted and disclosed as events.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

from drydock import adjudicate, events, llm
from drydock.config import ROOT, SETTINGS
from drydock.db import dsn, get

from . import playbook

OFFICIAL_READ_TOOLS = {
    "list_exasol_schemas", "find_exasol_schemas", "list_exasol_tables_and_views", "find_exasol_tables_and_views",
    "describe_exasol_tables_and_views", "execute_exasol_query", "list_exasol_built_in_functions",
    "describe_exasol_built_in_function",
}
MAX_TURNS = 80
SYSTEM = (ROOT / "agent" / "prompts" / "system.md").read_text()

LOCAL_TOOLS = [
    {"name": "profile_finding", "description": "Report one profiling finding (aggregates only) for the reviewer's narrative.",
     "input_schema": {"type": "object", "properties": {
         "source": {"type": "string"}, "table": {"type": "string"}, "column": {"type": "string"},
         "finding": {"type": "string"}}, "required": ["source", "table", "finding"]}},
    {"name": "state_hypothesis", "description": "Before a match-class branch: name the class and the number of GOLDEN rows you expect to change.",
     "input_schema": {"type": "object", "properties": {
         "match_class": {"type": "string"}, "expected_count": {"type": "integer"}, "rationale": {"type": "string"}},
         "required": ["match_class", "expected_count", "rationale"]}},
    {"name": "self_check", "description": "After diff_branch, before request_merge: compare expected vs observed and state the confidence you will request with.",
     "input_schema": {"type": "object", "properties": {
         "branch_id": {"type": "string"}, "expected": {"type": "integer"}, "observed": {"type": "integer"},
         "confidence_before": {"type": "number"}, "confidence_after": {"type": "number"}, "note": {"type": "string"}},
         "required": ["branch_id", "expected", "observed", "confidence_before", "confidence_after", "note"]}},
]


def _official_env() -> dict:
    full = dsn()
    env = {**os.environ, "EXA_DSN": full, "EXA_USER": os.environ.get("DRYDOCK_AGENT_USER", ""),
           "EXA_PASSWORD": os.environ.get("DRYDOCK_AGENT_PASSWORD", ""),
           "EXA_MCP_SETTINGS": json.dumps({"enable_read_query": True, "enable_write_query": False,
                                           "default_row_limit": SETTINGS.mcp_row_limit,
                                           "schemas": {"regexp_pattern": SETTINGS.mcp_schema_pattern}})}
    if "/nocertcheck" in full:
        env["EXA_SSL_CERT_VALIDATION"] = "no"
    return env


def _drydock_env(run_id: str) -> dict:
    return {**os.environ, "DRYDOCK_RUN_ID": run_id, "DRYDOCK_EVENT_URL": SETTINGS.event_url}


def _schema(t) -> dict:
    s = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
    return s if isinstance(s, dict) else s.model_dump()


def _text(res) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in res.content)


def _is_err(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


def _rows_returned(text: str) -> int:
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return 0
    if isinstance(d, dict) and isinstance(d.get("rows"), list):
        return len(d["rows"])
    if isinstance(d, list):
        return len(d)
    return 0


def declarations(tools: list[dict]):
    """MCP/local tool definitions -> Gemini FunctionDeclarations (JSON Schema passed through as-is)."""
    from google.genai import types
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"])
        for t in tools])]


async def _run(run_id: str, tier: int, gate_enabled: bool, seed: int) -> dict:
    from google.genai import types
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    client = llm.new_client()                   # held for the whole run; raises LlmUnavailable if no GEMINI_API_KEY
    db = get("svc")
    playbook.plan_path(run_id).unlink(missing_ok=True)
    playbook.start_run_row(db, run_id, tier=tier, gate_enabled=gate_enabled, seed=seed, mode="agent")

    async with AsyncExitStack() as stack:
        sessions: dict[str, ClientSession] = {}
        for name, params in (
            ("exasol_db", StdioServerParameters(command="uvx", args=["exasol-mcp-server@latest"], env=_official_env())),
            ("drydock", StdioServerParameters(command="uv", args=["run", "python", "-m", "drydock.mcp_server"],
                                              env=_drydock_env(run_id), cwd=str(ROOT))),
        ):
            r, w = await stack.enter_async_context(stdio_client(params))
            s = await stack.enter_async_context(ClientSession(r, w))
            await s.initialize()
            sessions[name] = s

        route: dict[str, str] = {}
        tools: list[dict] = []
        for name, s in sessions.items():
            for t in (await s.list_tools()).tools:
                if name == "exasol_db" and t.name not in OFFICIAL_READ_TOOLS:
                    continue
                route[t.name] = name
                tools.append({"name": t.name, "description": (t.description or "")[:1024], "input_schema": _schema(t)})
        tools += LOCAL_TOOLS
        tools.sort(key=lambda t: t["name"])

        # No temperature override: Gemini 3 guidance is to keep the default 1.0 (lower values can loop).
        # Determinism for the A/B is provided by replaying the control's recorded plan (agent/playbook.py).
        config = llm.tools_config(SYSTEM, declarations(tools), SETTINGS.planner_thinking)
        contents: list = [types.Content(role="user", parts=[types.Part.from_text(
            text=f"Run id: {run_id}. Reconcile SOURCE_B into GOLDEN following your instructions. "
                 "Begin by profiling both source schemas.")])]
        rows_seen = 0
        for _turn in range(MAX_TURNS):
            try:
                resp = await client.aio.models.generate_content(model=SETTINGS.planner_model, contents=contents,
                                                                config=config)
            except Exception as e:
                # Every planner turn is one provider request, so a quota cap can land mid-run. Stop cleanly
                # and record why: work already done stands, and nothing merged without passing the gate.
                if not adjudicate._is_quota(e):
                    raise
                events.emit("agent.note", run_id, None, phase="stop",
                            text=f"planner unavailable after {_turn} turns ({str(e)[:200]}); stopping the run. "
                                 "Work already done stands; nothing bypassed the gate.")
                break
            if llm.was_blocked(resp):
                events.emit("agent.note", run_id, None, phase="stop",
                            text=f"planner response blocked or empty ({llm.finish_reason(resp)}); stopping")
                break
            model_content = resp.candidates[0].content if resp.candidates else None
            if model_content is None:
                events.emit("agent.note", run_id, None, phase="stop",
                            text=f"planner returned no content ({llm.finish_reason(resp)}); stopping")
                break
            # Appended UNCHANGED: it carries Gemini 3's thought signatures, which must be returned
            # on the next turn of a function-calling conversation.
            contents.append(model_content)
            for part in model_content.parts or []:
                if getattr(part, "text", None) and part.text.strip() and not getattr(part, "thought", False):
                    events.emit("agent.note", run_id, None, text=part.text.strip()[:4000], phase="plan")
            calls = resp.function_calls or []
            if not calls:
                break
            reply_parts = []
            for call in calls:
                name = call.name or ""
                args = llm.normalise_args(dict(call.args or {}))
                text, err = await _call(sessions, route, run_id, name, args)
                if name == "execute_exasol_query" and not err:
                    n = _rows_returned(text)
                    if n:
                        rows_seen += n
                        events.emit("agent.note", run_id, None, phase="disclosure",
                                    text=f"{n} raw rows returned to the planner by this query ({rows_seen} total this run)")
                if route.get(name) == "drydock":
                    try:
                        playbook.record_step(run_id, name, args, json.loads(text) if text else {})
                    except ValueError:
                        playbook.record_step(run_id, name, args, {})
                part = types.Part.from_function_response(
                    name=name, response={"error": text[:20000]} if err else {"result": text[:20000]})
                if getattr(call, "id", None) and part.function_response is not None:
                    part.function_response.id = call.id
                reply_parts.append(part)
            contents.append(types.Content(role="user", parts=reply_parts))
    return playbook.end_run(db, run_id)


async def _call(sessions, route, run_id: str, name: str, args: dict) -> tuple[str, bool]:
    if name == "profile_finding":
        events.emit("profile.found", run_id, None, source=args.get("source", ""), table=args.get("table", ""),
                    column=args.get("column"), finding=args.get("finding", ""))
        return "noted", False
    if name == "state_hypothesis":
        events.emit("hypothesis.stated", run_id, None, match_class=args["match_class"],
                    expected_count=int(args["expected_count"]), rationale=args["rationale"])
        return "noted", False
    if name == "self_check":
        events.emit("selfcheck", run_id, args.get("branch_id"), expected=int(args["expected"]),
                    observed=int(args["observed"]), discrepancy=int(args["observed"]) - int(args["expected"]),
                    confidence_before=float(args["confidence_before"]), confidence_after=float(args["confidence_after"]),
                    note=args["note"])
        return "noted", False
    if name not in route:
        return f"unknown tool {name}", True
    res = await sessions[route[name]].call_tool(name, args)
    return _text(res), _is_err(res)


def run(run_id: str, tier: int = 2, gate_enabled: bool = True, seed: int = 20260913) -> dict:
    from drydock import dataset
    from drydock.db import get
    if not dataset.active(get("svc")).agent:
        # The agent reads sources through the official Exasol MCP server, whose grants and schema filter
        # (SETTINGS.mcp_schema_pattern) cover the demo sources only. Refuse rather than plan against the wrong tables.
        raise ValueError("Agent mode works on the demo data only for now. For your own files, choose "
                         "'scripted' mode: the same matching, gate and review, with a fixed plan.")
    return asyncio.run(_run(run_id, tier, gate_enabled, seed))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--tier", type=int, default=2)
    ap.add_argument("--gate-off", action="store_true", help="A/B control: every merge auto-applies")
    ap.add_argument("--seed", type=int, default=20260913)
    a = ap.parse_args()
    events.add_sink(events.jsonl_sink(Path(SETTINGS.events_jsonl)))
    print(json.dumps(run(a.run_id, a.tier, not a.gate_off, a.seed), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
