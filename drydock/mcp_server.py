"""Drydock MCP server: the agent's write path, alongside Exasol's official read-only server.

    uv run python -m drydock.mcp_server        # stdio, runs as DRYDOCK_SVC

Approve, reject, unmerge, tier changes and row selection are deliberately not tools:
they are reviewer-only REST endpoints on the orchestrator. Scores are not exposed
either, because they are computed from the ground truth.

Every tool returns JSON. Errors are {"ok": false, "error": CODE, "message": ...}, so
the agent can read them and retry.
"""

from __future__ import annotations

import functools
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import branch, diff, er, events, gate, precedent
from .config import SETTINGS, NotVerified
from .db import DbError, get
from .lintguard import DialectError

INSTRUCTIONS = """Drydock is the governed write path for Exasol. You never write GOLDEN directly.
Open a branch, run any SQL against GOLDEN.* in it (it is redirected to your branch copy), look at the
observed diff, then request a merge with an honest confidence. A person approves what the gate holds.
Reads of SOURCE_A, SOURCE_B and GOLDEN_V go through the official Exasol MCP server."""

server = MCPServer(name="drydock", instructions=INSTRUCTIONS)
RUN_ID = os.environ.get("DRYDOCK_RUN_ID")


def _db():
    return get("svc")


def tool(fn):
    @functools.wraps(fn)
    def wrapped(*a, **kw) -> dict[str, Any]:
        try:
            out = fn(*a, **kw)
            return out if isinstance(out, dict) else {"ok": True, "result": out}
        except branch.BranchError as e:
            return {"ok": False, "error": e.code, "message": e.message}
        except er.ErError as e:
            return {"ok": False, "error": "ER_INPUT", "message": str(e)}
        except NotVerified as e:
            return {"ok": False, "error": "NOT_VERIFIED", "message": str(e)}
        except DialectError as e:
            return {"ok": False, "error": "DIALECT", "message": str(e)[:2000]}
        except DbError as e:
            return {"ok": False, "error": f"EXASOL_{e.code}", "message": e.message[:2000]}
        except (ValueError, LookupError) as e:
            return {"ok": False, "error": "BAD_REQUEST", "message": str(e)}
    return server.tool()(wrapped)


def _run(run_id: str | None) -> str | None:
    return run_id or RUN_ID


@tool
def open_branch(purpose: str, defect_class: str | None = None, run_id: str | None = None) -> dict:
    """Open a branch: an isolated schema that copies a GOLDEN table only when you first write to it.
    Costs nothing until written. defect_class is the match class this branch handles
    (EXACT_EMAIL, PHONE_ADDRESS, FUZZY_NAME, NEW_CUSTOMERS, INTERNAL_DEDUP) — one branch per class."""
    return branch.open_branch(_db(), purpose, _run(run_id), defect_class, opened_by="agent")


@tool
def run_in_branch(branch_id: str, sql: str, rationale: str, idem_key: str | None = None) -> dict:
    """Run ONE SQL statement in your branch. Any SQL: CTEs, MERGE, subqueries, CREATE TABLE.
    References to GOLDEN.<table> are redirected to your branch copy; SOURCE_A, SOURCE_B and ER_WORK
    are readable; everything else is refused with a reason. Qualify every table name."""
    return branch.run_in_branch(_db(), branch_id, sql, rationale, idem_key, run_id=RUN_ID)


@tool
def diff_branch(branch_id: str) -> dict:
    """The observed row-level diff of your branch against GOLDEN at copy time: added / changed / deleted
    counts, and how many rows changed each column. Counts only — no row values. Never mutates anything."""
    return diff.summary_for_agent(diff.compute(_db(), branch_id))


@tool
def request_merge(branch_id: str, confidence: float, rationale: str, idem_key: str | None = None) -> dict:
    """Ask to merge your branch into GOLDEN. confidence is your honest probability (0..1) that every
    changed row is correct; a lower confidence tightens the gate. Returns MERGED, PENDING (a person will
    review) or BLOCKED (with the reason). Row-level decisions are the reviewer's, not yours."""
    return gate.request_merge(_db(), branch_id, confidence, rationale, idem_key)


@tool
def discard_branch(branch_id: str, reason: str) -> dict:
    """Drop the branch entirely (DROP SCHEMA ... CASCADE). Nothing in GOLDEN was ever touched."""
    return branch.discard_branch(_db(), branch_id, reason)


@tool
def list_pending(include_resolved: bool = True, run_id: str | None = None) -> dict:
    """Your merge requests and their resolutions (MERGED / PENDING / BLOCKED / REJECTED with codes)."""
    return {"merges": gate.list_pending(_db(), include_resolved, _run(run_id))}


@tool
def cite_precedents(comparison: str, k: int = 3) -> dict:
    """Retrieve past human decisions on pair comparisons similar to `comparison` (a description of how two
    records compare, never the records themselves). Case law, retrieved in the database."""
    return {"precedents": precedent.retrieve(_db(), comparison, k=k)}


@tool
def branch_log(branch_id: str | None = None, limit: int = 50) -> dict:
    """Your statements and their outcomes (OK / ERROR / BLOCKED, rows affected)."""
    return branch.branch_log(_db(), branch_id, RUN_ID, limit)


@tool
def declare_mapping(mapping: dict, rationale: str, run_id: str | None = None) -> dict:
    """Declare how SOURCE_B.CLIENTS maps onto the golden fields, after profiling both schemas.
    Keys: FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, POSTCODE, COUNTRY, DATE_OF_BIRTH (optionally LAST_SEEN).
    Values: one scalar SQL expression over SOURCE_B.CLIENTS columns (unqualified), e.g.
    "FIRST_NAME || ' ' || LAST_NAME" or "TO_DATE(DOB, 'DD/MM/YYYY')"."""
    return {"ok": True, "mapping": er.declare_mapping(_db(), _run(run_id), mapping, rationale)}


@tool
def stage_candidates(rule: str, select_sql: str, kind: str = "AB", run_id: str | None = None) -> dict:
    """Blocking. Stage candidate pairs from ONE blocking rule: a SELECT returning exactly two columns
    named A_ID and B_ID, reading SOURCE_A / SOURCE_B only. kind='AB' pairs SOURCE_A.CUST_ID with
    SOURCE_B.CLIENT_REF; kind='AA' pairs two SOURCE_A.CUST_IDs (duplicates inside system A).
    Call once per rule. Returns the pair count only."""
    return er.stage_candidates(_db(), _run(run_id), rule, select_sql, kind)


@tool
def run_matching(run_id: str | None = None) -> dict:
    """Score every staged candidate pair in the database, let three matchers vote (deterministic,
    probabilistic, skeptic), adjudicate the split votes, detect transitive clusters (never merged), and
    publish read-only decision tables in ER_WORK for your branch SQL. Returns counts and table names only."""
    return er.run_matching(_db(), _run(run_id))


def main() -> None:
    er.install_hooks()
    if SETTINGS.event_url:
        events.add_sink(events.http_sink(SETTINGS.event_url))
    else:
        events.add_sink(events.jsonl_sink(SETTINGS.events_jsonl))
    server.run("stdio")


if __name__ == "__main__":
    main()
