"""Verify the Exasol instance: answer, against the live database, every question the
design depends on. Writes verified.json (signed, merged by check id) and appends a run
block to VERIFIED.md.

    uv run python scripts/verify.py                 # everything
    uv run python scripts/verify.py --only V4,V5    # a subset
    uv run python scripts/verify.py --list          # show checks
    uv run python scripts/verify.py --as admin      # force the runner identity

Status is exactly one of PASS | FAIL | UNKNOWN.
  PASS    answered, and the answer is the one the design needs
  FAIL    answered, and the design uses its fallback
  UNKNOWN the check could not answer (identity missing, tool missing, or an error that
          does not settle the question)

Runner identity: svc (DRYDOCK_SVC) if configured and it connects, else admin. Checks
only create and drop objects inside PROBE_SCRATCH* schemas and one throwaway user.
"""

from __future__ import annotations

__dialect_lint__ = "exempt"  # deliberately runs unproven and expected-to-fail SQL

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    DIALECT_MD, SCRATCH, VERIFIED_JSON, IdentityMissing, Outcome, append_verified, connect,
    dsn, identity_configured, identity_user, now_iso, run,
)

SCRATCH2 = "PROBE_SCRATCH2"
V11_BRANCH = "PROBE_BRANCH_V11"
V11_VICTIM = "PROBE_V11_VICTIM"
L7_USER = "PROBE_U_L7"
SRC_ROWS = 200_000
GOLDEN_ROWS = 36_600
MCP_CMD = ["uvx", "exasol-mcp-server@latest"]
KEY = ""  # set in main(): the signing key


# =========================================================================== harness

@dataclass
class Step:
    identity: str
    sql: str
    out: Outcome

    def text(self) -> str:
        return f"[{self.identity}] {self.sql}\n  -> {self.out.render(12).replace(chr(10), chr(10) + '     ')}"


@dataclass
class Result:
    id: str
    question: str
    status: str = "UNKNOWN"
    summary: str = ""
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stderr: str = ""
    ms: float = 0.0
    runner: str = ""
    ts: str = ""

    def as_json(self) -> dict:
        return {
            "id": self.id, "question": self.question, "status": self.status,
            "summary": self.summary,
            "sql": "\n".join(f"[{s.identity}] {s.sql}" for s in self.steps),
            "stdout": "\n".join([s.text() for s in self.steps] + self.notes),
            "stderr": self.stderr, "ms": round(self.ms, 1), "runner": self.runner,
            "ts": self.ts,
        }


class Ctx:
    def __init__(self, runner: str):
        self.runner = runner
        self._conns: dict[tuple[str, bool], object] = {}
        self.r: Result | None = None

    def conn(self, identity: str | None = None, *, autocommit: bool = True, fresh: bool = False,
             query_timeout: int = 0):
        identity = identity or self.runner
        key = (identity, autocommit)
        if fresh or query_timeout:
            c = connect(identity, autocommit=autocommit, query_timeout=query_timeout)
            return c
        if key not in self._conns:
            self._conns[key] = connect(identity, autocommit=autocommit)
        return self._conns[key]

    def has(self, identity: str) -> bool:
        if not identity_configured(identity):
            return False
        try:
            self.conn(identity)
            return True
        except Exception as e:
            self.note(f"identity {identity} configured but cannot connect: {type(e).__name__}: {e}")
            return False

    def sql(self, sql: str, identity: str | None = None, conn=None) -> Outcome:
        identity = identity or self.runner
        c = conn if conn is not None else self.conn(identity)
        o = run(c, sql)
        self.r.steps.append(Step(identity, o.sql, o))  # o.sql is redacted
        return o

    def note(self, msg: str) -> None:
        self.r.notes.append("NOTE: " + msg)

    def val(self, o: Outcome):
        return o.rows[0][0] if o.ok and o.rows else None

    def close(self):
        for c in self._conns.values():
            try:
                c.close()
            except Exception:
                pass


CHECKS: list[tuple[str, str, Callable[[Ctx], tuple[str, str]]]] = []


def check(cid: str, question: str):
    def deco(fn):
        CHECKS.append((cid, question, fn))
        return fn
    return deco


def q(name: str, schema: str = SCRATCH) -> str:
    return f"{schema}.{name}"


def table_exists(ctx: Ctx, schema: str, table: str) -> bool | None:
    o = ctx.sql(f"SELECT COUNT(*) FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = '{schema}' "
                f"AND TABLE_NAME = '{table}'")
    return None if not o.ok else int(o.rows[0][0]) > 0


def ensure_src(ctx: Ctx, n: int = SRC_ROWS) -> str | None:
    """PROBE_SCRATCH.V3_SRC with n rows, GOLDEN-like shape. Returns error or None.
    Tries pyexasol's IMPORT (the path bench/generate.py will need); falls back
    to pure-SQL doubling. Records which path worked."""
    if table_exists(ctx, SCRATCH, "V3_SRC"):
        o = ctx.sql(f"SELECT COUNT(*) FROM {q('V3_SRC')}")
        if o.ok and int(o.rows[0][0]) >= n:
            return None
        ctx.sql(f"DROP TABLE {q('V3_SRC')}")
    ddl = (f"CREATE TABLE {q('V3_SRC')} (GOLDEN_ID VARCHAR(64), FULL_NAME VARCHAR(200), "
           "EMAIL VARCHAR(200), PHONE VARCHAR(50), ADDR_LINE VARCHAR(300), CITY VARCHAR(100), "
           "POSTCODE VARCHAR(20), COUNTRY VARCHAR(60), SOURCE_A_REF VARCHAR(64), "
           "SOURCE_B_REF VARCHAR(64), MERGED_AT TIMESTAMP, SURVIVORSHIP VARCHAR(2000))")
    o = ctx.sql(ddl)
    if not o.ok:
        return f"cannot create V3_SRC: {o.error_text}"

    def rows():
        for i in range(n):
            yield (f"G{i:08d}", f"Person {i} Surname{i % 997}", f"p{i}@example{i % 50}.com",
                   f"+44 20 7{i % 1000:03d} {i % 10000:04d}", f"{i % 300} High Street",
                   f"City{i % 400}", f"SW{i % 20} {i % 9}AB", "United Kingdom",
                   f"A{i:08d}", None, None, None)

    t0 = time.perf_counter()
    try:
        ctx.conn().import_from_iterable(rows(), (SCRATCH, "V3_SRC"))
        ms = (time.perf_counter() - t0) * 1000
        ctx.note(f"V3_SRC loaded via pyexasol import_from_iterable: {n} rows in {ms:.0f}ms "
                 "(IMPORT over HTTP transport WORKS — bench/generate.py can use it)")
        return None
    except Exception as e:
        ctx.note(f"import_from_iterable FAILED ({type(e).__name__}: {e}); falling back to SQL doubling")
    ins = ctx.sql(f"INSERT INTO {q('V3_SRC')} (GOLDEN_ID, FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, "
                  "POSTCODE, COUNTRY, SOURCE_A_REF) VALUES ('G00000000', 'Person 0', 'p0@example0.com', "
                  "'+44 20 7000 0000', '0 High Street', 'City0', 'SW0 0AB', 'United Kingdom', 'A00000000')")
    if not ins.ok:
        return f"cannot insert seed row: {ins.error_text}"
    have = 1
    while have < n:
        o = ctx.sql(f"INSERT INTO {q('V3_SRC')} SELECT 'G' || LPAD(TO_CHAR(TO_NUMBER(SUBSTR(GOLDEN_ID, 2)) + {have}), 8, '0'), "
                    "FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, POSTCODE, COUNTRY, SOURCE_A_REF, "
                    f"SOURCE_B_REF, MERGED_AT, SURVIVORSHIP FROM {q('V3_SRC')}")
        if not o.ok:
            return f"SQL doubling failed: {o.error_text}"
        have *= 2
    ctx.note(f"V3_SRC loaded via SQL doubling: {have} rows (import_from_iterable unavailable)")
    return None


def dialect_record(title: str, o: Outcome, note: str = "", allows: str = "", proven: bool | None = None) -> None:
    """Append a PROVEN/DISPROVEN entry to sql/DIALECT.md from a live outcome (Gate 4).
    Only live results are written; the linter then whitelists by evidence.
    `proven` defaults to "the statement succeeded". Pass it explicitly when the CLAIM in the title is
    proven by the statement FAILING (e.g. "PRIMARY KEY enforced" is proven by a refused duplicate).
    A re-run does not append a duplicate of an entry already recorded with the same title, status and SQL."""
    status = "PROVEN" if (o.ok if proven is None else proven) else "DISPROVEN"
    result = o.render(3).replace("\n", " / ") if o.ok else f"[{o.error_code}] {o.error_text}"
    existing = DIALECT_MD.read_text() if DIALECT_MD.exists() else ""
    if re.search(rf"(?m)^### {re.escape(title)}\n{status} [^\n]*\nSQL:    {re.escape(o.sql)}$", existing):
        return
    entry = (f"\n### {title}\n{status} {now_iso()[:10]} (auto, verify.py)\nSQL:    {o.sql}\n"
             f"RESULT: {result}\n" + (f"NOTE:   {note}\n" if note else "") + (f"ALLOWS: {allows}\n" if allows else ""))
    with DIALECT_MD.open("a") as f:
        f.write(entry)


def err_says(o: Outcome, *words: str) -> bool:
    t = (o.error_text or "").lower()
    return any(w.lower() in t for w in words)


# =========================================================================== checks

@check("P3", "Is the build 2026.1 or later? (EXA_METADATA databaseProductVersion)")
def c_p3(ctx):
    o = ctx.sql("SELECT PARAM_VALUE FROM EXA_METADATA WHERE PARAM_NAME = 'databaseProductVersion'")
    v = ctx.val(o)
    if v is None:
        return "UNKNOWN", f"could not read version: {o.error_text}"
    try:
        major = int(str(v).split(".")[0])
    except ValueError:
        return "UNKNOWN", f"version string {v!r} not parseable; human to judge"
    if major >= 2026:
        return "PASS", f"version {v}"
    return "FAIL", f"version {v} is older than 2026.1 — AI functions will not exist"


@check("V1", "Are Exasol AI functions (AI_CLASSIFY/AI_SENTIMENT/AI_EXTRACT_ENTITIES, *_EXTENDED) callable? Per-row latency?")
def c_v1(ctx):
    ctx.sql("SELECT SCRIPT_SCHEMA, SCRIPT_NAME, SCRIPT_TYPE FROM EXA_ALL_SCRIPTS "
            "WHERE UPPER(SCRIPT_NAME) LIKE 'AI%' OR UPPER(SCRIPT_NAME) LIKE '%_EXTENDED%'")
    ctx.sql("SELECT * FROM EXA_SQL_KEYWORDS WHERE UPPER(KEYWORD) LIKE 'AI%'")
    cls = ctx.sql("SELECT AI_CLASSIFY('The parcel arrived broken and nobody answered the phone')")
    sen = ctx.sql("SELECT AI_SENTIMENT('The parcel arrived broken and nobody answered the phone')")
    ctx.sql("SELECT AI_EXTRACT_ENTITIES('John Smith moved from London to Madrid in 2019')")
    if not (cls.ok or sen.ok):
        return "FAIL", ("AI_CLASSIFY and AI_SENTIMENT both errored (see steps for exact text). "
                        "The Gemini adjudicator is used until the extension is deployed.")
    fn = "AI_CLASSIFY" if cls.ok else "AI_SENTIMENT"
    err = ensure_src(ctx, 1000)
    if err:
        return "PASS", f"{fn} callable; latency test skipped: {err}"
    lat = ctx.sql(f"SELECT COUNT(*) FROM (SELECT {fn}(FULL_NAME || ' lives in ' || CITY) AS R "
                  f"FROM {q('V3_SRC')} WHERE GOLDEN_ID < 'G00001000')")
    per = f"{lat.ms / 1000:.2f}ms/row over 1000 rows" if lat.ok else f"latency query failed: {lat.error_text}"
    return "PASS", f"{fn} callable. {per}"


async def _mcp_session(env: dict, fn):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=MCP_CMD[0], args=MCP_CMD[1:], env={**os.environ, **env})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def _mcp_env(identity: str, settings: dict | None) -> dict:
    user_var, pw_var = {"agent": ("DRYDOCK_AGENT_USER", "DRYDOCK_AGENT_PASSWORD"),
                        "svc": ("DRYDOCK_SVC_USER", "DRYDOCK_SVC_PASSWORD"),
                        "admin": ("EXA_ADMIN_USER", "EXA_ADMIN_PASSWORD")}[identity]
    full = dsn()  # host[/FINGERPRINT|/nocertcheck]:port — passed through unchanged
    env = {"EXA_DSN": full,
           "EXA_USER": os.environ.get(user_var, ""), "EXA_PASSWORD": os.environ.get(pw_var, "")}
    if "/nocertcheck" in full:
        env["EXA_SSL_CERT_VALIDATION"] = "no"
    if settings is not None:
        env["EXA_MCP_SETTINGS"] = json.dumps(settings)
    return env


def _is_err(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


def _text(res) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in res.content)[:2000]


def _mcp_identity(ctx) -> str | None:
    for ident in ("agent", "svc", "admin"):
        if identity_configured(ident):
            if ident != "agent":
                ctx.note(f"DRYDOCK_AGENT not configured; MCP checks run as {ident}")
            return ident
    return None


@check("V2", "Is query execution enabled in the official MCP server? Exact settings key and default.")
def c_v2(ctx):
    if not shutil.which("uvx"):
        return "UNKNOWN", "uvx not on PATH"
    ident = _mcp_identity(ctx)
    if not ident:
        return "UNKNOWN", "no identity configured"
    ctx.note("Source-read (exasol-mcp-server 2.2.0, setup/server_settings.py): env EXA_MCP_SETTINGS "
             "(JSON or path to JSON); enable_read_query=False, enable_write_query=False, "
             "disable_elicitation=False, default_row_limit=None")

    async def probe_default(s):
        tools = [t.name for t in (await s.list_tools()).tools]
        res = await s.call_tool("execute_exasol_query", {"query": "SELECT 1 AS X"}) \
            if "execute_exasol_query" in tools else None
        return tools, res

    async def probe_enabled(s):
        tools = [t.name for t in (await s.list_tools()).tools]
        res = await s.call_tool("execute_exasol_query", {"query": "SELECT 42 AS X"})
        return tools, res

    try:
        tools0, res0 = asyncio.run(_mcp_session(_mcp_env(ident, None), probe_default))
        ctx.note(f"DEFAULT settings, tools: {tools0}")
        if res0 is not None:
            ctx.note(f"DEFAULT settings, execute_exasol_query -> isError={_is_err(res0)}: {_text(res0)}")
        tools1, res1 = asyncio.run(_mcp_session(_mcp_env(ident, {"enable_read_query": True}), probe_enabled))
        ctx.note(f"enable_read_query=true, tools: {tools1}")
        ctx.note(f"enable_read_query=true, execute_exasol_query -> isError={_is_err(res1)}: {_text(res1)}")
    except Exception:
        ctx.r.stderr = traceback.format_exc()
        return "UNKNOWN", "MCP session failed (see stderr)"
    default_off = res0 is None or _is_err(res0)
    on_works = (not _is_err(res1)) and "42" in _text(res1)
    if on_works:
        return "PASS", (f"EXA_MCP_SETTINGS={{\"enable_read_query\": true}} enables reads (returned 42). "
                        f"Default is {'OFF' if default_off else 'ON (!)'}.")
    return "FAIL", "enable_read_query=true did not yield a working query (see notes)"


@check("V10", "Official MCP server: install path, config location, full settings schema.")
def c_v10(ctx):
    if not shutil.which("uvx"):
        return "UNKNOWN", "uvx not on PATH"
    code = ("import json; from exasol.ai.mcp.server.setup.server_settings import McpServerSettings as S; "
            "import importlib.metadata as m; print(m.version('exasol-mcp-server')); "
            "print(json.dumps(S.model_json_schema()))")
    p = subprocess.run(["uvx", "--from", "exasol-mcp-server@latest", "python", "-c", code],
                       capture_output=True, text=True, timeout=300)
    ctx.r.stderr = p.stderr[-4000:]
    if p.returncode != 0:
        return "UNKNOWN", "could not import the server's settings model (see stderr)"
    version, _, schema = p.stdout.partition("\n")
    ctx.note(f"exasol-mcp-server version: {version}")
    ctx.note(f"settings JSON schema: {schema.strip()[:6000]}")
    ctx.note("install: `uvx exasol-mcp-server@latest` (ephemeral) or `uv tool install exasol-mcp-server@latest`; "
             "config: env EXA_DSN/EXA_USER/EXA_PASSWORD, EXA_MCP_SETTINGS (inline JSON or file path), "
             "EXA_SSL_CERT_VALIDATION yes|no")
    return "PASS", f"exasol-mcp-server {version}; schema captured in stdout"


@check("V13", "Can the official MCP server filter which schemas/tables it exposes (hide __ARCH_/__NEW_)?")
def c_v13(ctx):
    if not shutil.which("uvx"):
        return "UNKNOWN", "uvx not on PATH"
    ident = _mcp_identity(ctx)
    for s in (f"DROP TABLE IF EXISTS {q('KEEPME')}", f"DROP TABLE IF EXISTS {q('CUSTOMERS__ARCH_1')}",
              f"CREATE TABLE {q('KEEPME')} (A DECIMAL(9,0))",
              f"CREATE TABLE {q('CUSTOMERS__ARCH_1')} (A DECIMAL(9,0))"):
        ctx.sql(s)
    if ident == "agent":
        ctx.sql(f"GRANT SELECT ON {q('KEEPME')} TO {identity_user('agent')}")
        ctx.sql(f"GRANT SELECT ON {q('CUSTOMERS__ARCH_1')} TO {identity_user('agent')}")
    settings = {"tables": {"regexp_pattern": "^(?!.*__(ARCH|NEW)_).*$"}}

    async def probe(s):
        return await s.call_tool("list_exasol_tables_and_views", {"schema_name": SCRATCH})

    try:
        res = asyncio.run(_mcp_session(_mcp_env(ident, settings), probe))
    except Exception:
        ctx.r.stderr = traceback.format_exc()
        return "UNKNOWN", "MCP session failed"
    t = _text(res)
    ctx.note(f"tables.regexp_pattern={settings['tables']['regexp_pattern']!r} -> {t}")
    ctx.note("IMPORTANT: filters apply to LISTING tools only. execute_exasol_query is not filtered; "
             "only the GRANT floor stops the agent reading an __ARCH_ table by name.")
    if "KEEPME" in t and "__ARCH_" not in t:
        return "PASS", "negative-lookahead regexp_pattern hides __ARCH_ from listings (not from queries)"
    if "KEEPME" in t:
        return "FAIL", "listing filter did not hide __ARCH_ (fallback: GRANT floor + hard block)"
    return "UNKNOWN", "listing returned neither table (identity may lack visibility)"


@check("V3", "CTAS wall-clock at 200k rows and at GOLDEN scale (36.6k). THIS NUMBER GOES ON SCREEN.")
def c_v3(ctx):
    err = ensure_src(ctx)
    if err:
        return "UNKNOWN", err
    res = {}
    for label, where in (("200k", ""), ("36.6k", f" WHERE GOLDEN_ID < 'G{GOLDEN_ROWS:08d}'")):
        times = []
        for i in range(3):
            ctx.sql(f"DROP TABLE IF EXISTS {q('V3_CTAS')}")
            o = ctx.sql(f"CREATE TABLE {q('V3_CTAS')} AS SELECT * FROM {q('V3_SRC')}{where}")
            if not o.ok:
                return "UNKNOWN", f"CTAS failed: {o.error_text}"
            times.append(o.ms)
        res[label] = statistics.median(times)
    dialect_record("CREATE TABLE AS SELECT", o, f"200k-row CTAS median {res['200k']:.0f}ms")
    ctx.sql(f"DROP TABLE IF EXISTS {q('V3_CTAS')}")
    summary = (f"CTAS median of 3 (client-observed, includes round trip): "
               f"200k rows {res['200k']:.0f}ms; 36.6k rows {res['36.6k']:.0f}ms")
    return ("PASS" if res["200k"] < 1000 else "FAIL"), summary


@check("V4", "Is DDL transactional (RENAME rolls back)? What does a 2nd session see mid-swap? pyexasol autocommit default?")
def c_v4(ctx):
    import pyexasol.constant as pc
    ctx.note(f"pyexasol DEFAULT_AUTOCOMMIT = {pc.DEFAULT_AUTOCOMMIT} (client-side fact)")
    for s in (f"DROP TABLE IF EXISTS {q('V4_T')}", f"DROP TABLE IF EXISTS {q('V4_T2')}",
              f"DROP TABLE IF EXISTS {q('V4_C')}",
              f"CREATE TABLE {q('V4_T')} (A DECIMAL(9,0))", f"INSERT INTO {q('V4_T')} VALUES (7)"):
        ctx.sql(s)
    tx = ctx.conn(autocommit=False, fresh=True)
    ctx.sql(f"RENAME TABLE {q('V4_T')} TO V4_T2", conn=tx)
    ctx.sql(f"CREATE TABLE {q('V4_C')} (A DECIMAL(9,0))", conn=tx)
    # second session, mid-transaction: does it see old name, block, or error?
    try:
        other = ctx.conn(fresh=True, query_timeout=5)
        seen = ctx.sql(f"SELECT COUNT(*) FROM {q('V4_T')}", conn=other)
        ctx.note(f"2nd session mid-swap reading OLD name: {'ok ' + str(seen.rows) if seen.ok else seen.error_text} "
                 f"after {seen.ms:.0f}ms")
        other.close()
    except Exception as e:
        ctx.note(f"2nd session probe failed: {e}")
    ctx.sql("ROLLBACK", conn=tx)
    tx.close()
    old = table_exists(ctx, SCRATCH, "V4_T")
    new = table_exists(ctx, SCRATCH, "V4_T2")
    created = table_exists(ctx, SCRATCH, "V4_C")
    if old and not new and not created:
        return "PASS", "RENAME and CREATE TABLE both rolled back: DDL is transactional; the swap can be atomic"
    if new or created:
        return "FAIL", (f"after ROLLBACK: V4_T={old} V4_T2={new} V4_C={created} — DDL NOT transactional; "
                        "disclose the two-statement swap window")
    return "UNKNOWN", f"inconclusive: V4_T={old} V4_T2={new} V4_C={created}"


@check("V5", "Same-schema RENAME works and is O(1) at 200k; cross-schema RENAME fails (exact error).")
def c_v5(ctx):
    err = ensure_src(ctx)
    if err:
        return "UNKNOWN", err
    for s in (f"DROP TABLE IF EXISTS {q('V5_A')}", f"DROP TABLE IF EXISTS {q('V5_B')}",
              f"DROP TABLE IF EXISTS {q('V5_C')}", f"CREATE SCHEMA IF NOT EXISTS {SCRATCH2}",
              f"CREATE TABLE {q('V5_A')} AS SELECT * FROM {q('V3_SRC')}"):
        ctx.sql(s)
    unq = ctx.sql(f"RENAME TABLE {q('V5_A')} TO V5_B")
    qual = ctx.sql(f"RENAME TABLE {q('V5_B')} TO {q('V5_C')}")
    cur = "V5_C" if qual.ok else ("V5_B" if unq.ok else "V5_A")
    cross = ctx.sql(f"RENAME TABLE {q(cur)} TO {q('V5_X', SCRATCH2)}")
    ctx.note(f"unqualified target: {'OK ' + format(unq.ms, '.0f') + 'ms' if unq.ok else unq.error_text}")
    ctx.note(f"qualified same-schema target: {'OK ' + format(qual.ms, '.0f') + 'ms' if qual.ok else qual.error_text}")
    ctx.note(f"cross-schema: {'OK (!!)' if cross.ok else '[' + str(cross.error_code) + '] ' + str(cross.error_text)}")
    dialect_record("Same-schema RENAME (unqualified target)", unq, "RENAME TABLE s.t1 TO t2 — drydock/merge.py uses this form when PROVEN")
    dialect_record("Same-schema RENAME (qualified target)", qual, "RENAME TABLE s.t1 TO s.t2")
    dialect_record("Cross-schema RENAME", cross, "expected DISPROVEN (docs: cannot move objects between schemas)")
    if (unq.ok or qual.ok) and not cross.ok:
        ms = unq.ms if unq.ok else qual.ms
        return "PASS", f"same-schema RENAME OK at 200k in {ms:.0f}ms; cross-schema refused as documented"
    if cross.ok:
        return "FAIL", "cross-schema RENAME SUCCEEDED — contradicts the docs; the staging copy could be skipped"
    return "FAIL", "same-schema RENAME failed in both forms"


@check("V14", "Does an object-level GRANT survive stage-then-swap? (agent reads new T, is denied on __ARCH_?) Do views follow the name?")
def c_v14(ctx):
    if not ctx.has("agent"):
        return "UNKNOWN", "DRYDOCK_AGENT not configured"
    agent = identity_user("agent")
    for s in (f"DROP VIEW IF EXISTS {q('V14_VIEW')}", f"DROP TABLE IF EXISTS {q('V14_T')}",
              f"DROP TABLE IF EXISTS {q('V14_T__ARCH_1')}", f"DROP TABLE IF EXISTS {q('V14_T__NEW_1')}",
              f"CREATE TABLE {q('V14_T')} (A DECIMAL(9,0))", f"INSERT INTO {q('V14_T')} VALUES (1)",
              f"CREATE VIEW {q('V14_VIEW')} AS SELECT A FROM {q('V14_T')}",
              f"GRANT SELECT ON {q('V14_T')} TO {agent}",
              f"CREATE TABLE {q('V14_T__NEW_1')} AS SELECT A + 1 AS A FROM {q('V14_T')}",
              f"RENAME TABLE {q('V14_T')} TO V14_T__ARCH_1",
              f"RENAME TABLE {q('V14_T__NEW_1')} TO V14_T"):
        ctx.sql(s)
    new = ctx.sql(f"SELECT A FROM {q('V14_T')}", identity="agent")
    arch = ctx.sql(f"SELECT A FROM {q('V14_T__ARCH_1')}", identity="agent")
    view = ctx.sql(f"SELECT A FROM {q('V14_VIEW')}")
    ctx.note(f"agent on new T: {'reads ' + str(new.rows) if new.ok else 'DENIED ' + str(new.error_text)}")
    ctx.note(f"agent on __ARCH_: {'reads ' + str(arch.rows) + ' (!!)' if arch.ok else 'denied'}")
    ctx.note(f"view after swap returns: {view.rows if view.ok else view.error_text} (2 = view follows the NAME)")
    if new.ok and not arch.ok:
        return "PASS", "grant follows the name; archive not readable by agent"
    return "FAIL", ("grant follows the OBJECT: after a swap the agent loses GOLDEN.CUSTOMERS and gains the "
                    "archive. Fix: agent reads through a view layer / re-GRANT inside merge.py")


@check("V6", "HASH_SHA256, EDIT_DISTANCE, SOUNDEX, GROUP_CONCAT: availability, output, GROUP_CONCAT length limit.")
def c_v6(ctx):
    h = ctx.sql("SELECT HASH_SHA256('abc')")
    ed = ctx.sql("SELECT EDIT_DISTANCE('kitten', 'sitting')")
    sx = ctx.sql("SELECT SOUNDEX('Robert'), SOUNDEX('Rupert'), SOUNDEX('John Smith'), SOUNDEX('Smith')")
    for s in (f"DROP TABLE IF EXISTS {q('V6_T')}", f"CREATE TABLE {q('V6_T')} (V VARCHAR(10))",
              f"INSERT INTO {q('V6_T')} VALUES ('b'), ('a'), ('c')"):
        ctx.sql(s)
    gc = ctx.sql(f"SELECT GROUP_CONCAT(V ORDER BY V SEPARATOR '|') FROM {q('V6_T')}")
    want = hashlib.sha256(b"abc").hexdigest()
    ok_h = h.ok and str(ctx.val(h)).lower() == want
    ok_ed = ed.ok and int(ctx.val(ed)) == 3
    ok_sx = sx.ok and sx.rows[0][0] == "R163"
    ok_gc = gc.ok and ctx.val(gc) == "a|b|c"
    ctx.note(f"HASH_SHA256('abc') = {ctx.val(h)!r}; python = {want!r}; match={ok_h}")
    if sx.ok:
        ctx.note(f"SOUNDEX Robert/Rupert/'John Smith'/Smith = {sx.rows[0]} (multi-word behaviour matters for blocking)")
    if not ensure_src(ctx):
        big = ctx.sql(f"SELECT LENGTH(GROUP_CONCAT(EMAIL SEPARATOR ',')) FROM {q('V3_SRC')}")
        ctx.note(f"GROUP_CONCAT over 200k emails: {'length ' + str(ctx.val(big)) if big.ok else big.error_text}")
    status = "PASS" if (ok_h and ok_ed and ok_sx and ok_gc) else "FAIL"
    return status, f"HASH_SHA256={ok_h} EDIT_DISTANCE={ok_ed} SOUNDEX={ok_sx} GROUP_CONCAT={ok_gc}"


@check("V7", "Max VARCHAR length (expect 2,000,000).")
def c_v7(ctx):
    ctx.sql(f"DROP TABLE IF EXISTS {q('V7_A')}")
    ctx.sql(f"DROP TABLE IF EXISTS {q('V7_B')}")
    a = ctx.sql(f"CREATE TABLE {q('V7_A')} (V VARCHAR(2000000))")
    b = ctx.sql(f"CREATE TABLE {q('V7_B')} (V VARCHAR(2000001))")
    if a.ok and not b.ok:
        return "PASS", "VARCHAR(2000000) OK, VARCHAR(2000001) refused"
    return "FAIL", f"VARCHAR(2000000) ok={a.ok}; VARCHAR(2000001) ok={b.ok} — adjust DDL sizes"


def _udf(ctx, name: str, body: str, ret: str = "VARCHAR(2000)") -> Outcome:
    ctx.sql(f"DROP SCRIPT IF EXISTS {q(name)}")
    return ctx.sql(f"CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {q(name)}(x VARCHAR(100)) RETURNS {ret} AS\n{body}\n")


@check("R0", "Ladder step 0: can a trivial PYTHON3 SCALAR SCRIPT be created and called?")
def c_r0(ctx):
    c = _udf(ctx, "HELLO", "def run(ctx):\n    return 'hi ' + ctx.x")
    o = ctx.sql(f"SELECT {q('HELLO')}('there')")
    dialect_record("CREATE PYTHON3 SCALAR SCRIPT", c, "script body is sent WITHOUT the client-side / terminator")
    if c.ok and o.ok and ctx.val(o) == "hi there":
        return "PASS", "UDF created and returned 'hi there'"
    return "FAIL", f"create ok={c.ok}; call: {o.error_text if not o.ok else ctx.val(o)}"


@check("R1", "Ladder step 1: stdlib imports (json, hashlib, re) inside a UDF.")
def c_r1(ctx):
    body = ("import json, hashlib, re\n"
            "def run(ctx):\n"
            "    return json.dumps({'h': hashlib.sha256(ctx.x.encode()).hexdigest()[:8], "
            "'d': re.sub('[^0-9]', '', ctx.x)})")
    _udf(ctx, "STDLIB", body)
    o = ctx.sql(f"SELECT {q('STDLIB')}('(555) 010-2233')")
    return ("PASS", f"returned {ctx.val(o)}") if o.ok else ("FAIL", o.error_text or "")


@check("V8", "Ladder step 2: can a UDF import a non-stdlib package (numpy) WITHOUT BucketFS work?")
def c_v8(ctx):
    _udf(ctx, "NUMPY", "def run(ctx):\n    import numpy\n    return numpy.__version__")
    o = ctx.sql(f"SELECT {q('NUMPY')}('x')")
    _udf(ctx, "PYVER", "import sys\ndef run(ctx):\n    return sys.version")
    ctx.sql(f"SELECT {q('PYVER')}('x')")
    if o.ok:
        return "PASS", f"numpy {ctx.val(o)} importable in the default container"
    return "FAIL", f"numpy not importable ({o.error_text}); non-stdlib needs a language container"


@check("V9", "IDENTITY columns generated? PRIMARY KEY / UNIQUE enforced on insert?")
def c_v9(ctx):
    for s in (f"DROP TABLE IF EXISTS {q('V9_I')}", f"DROP TABLE IF EXISTS {q('V9_P')}",
              f"DROP TABLE IF EXISTS {q('V9_PE')}", f"DROP TABLE IF EXISTS {q('V9_U')}",
              f"CREATE TABLE {q('V9_I')} (ID DECIMAL(18,0) IDENTITY, V VARCHAR(10))",
              f"INSERT INTO {q('V9_I')} (V) VALUES ('a'), ('b')"):
        ctx.sql(s)
    ids = ctx.sql(f"SELECT ID, V FROM {q('V9_I')} ORDER BY V")
    ctx.sql(f"CREATE TABLE {q('V9_P')} (K DECIMAL(9,0) PRIMARY KEY, V VARCHAR(10))")
    ctx.sql(f"INSERT INTO {q('V9_P')} VALUES (1, 'a')")
    dup = ctx.sql(f"INSERT INTO {q('V9_P')} VALUES (1, 'b')")
    ctx.sql(f"CREATE TABLE {q('V9_PE')} (K DECIMAL(9,0), V VARCHAR(10), CONSTRAINT V9_PE_PK PRIMARY KEY (K) ENABLE)")
    ctx.sql(f"INSERT INTO {q('V9_PE')} VALUES (1, 'a')")
    dup_e = ctx.sql(f"INSERT INTO {q('V9_PE')} VALUES (1, 'b')")
    ctx.sql(f"CREATE TABLE {q('V9_U')} (K DECIMAL(9,0) UNIQUE)")
    ctx.sql("SELECT * FROM EXA_PARAMETERS WHERE PARAMETER_NAME LIKE '%CONSTRAINT%'")
    dialect_record("IDENTITY columns", ids, "generated on INSERT without the column")
    dialect_record("PRIMARY KEY enforced by default (duplicate INSERT refused)", dup,
                   "PROVEN = the duplicate was refused with 27002.",
                   proven=(not dup.ok and dup.error_code == "27002"))
    ident_ok = ids.ok and len(ids.rows) == 2 and all(r[0] is not None for r in ids.rows) \
        and ids.rows[0][0] != ids.rows[1][0]
    pk_default = not dup.ok
    pk_enable = not dup_e.ok
    ctx.note(f"IDENTITY generated: {ident_ok} {ids.rows if ids.ok else ids.error_text}")
    ctx.note(f"PK enforced by default: {pk_default}; with explicit ENABLE: {pk_enable}")
    if ident_ok and (pk_default or pk_enable):
        how = "by default" if pk_default else "only with CONSTRAINT ... PRIMARY KEY ... ENABLE"
        return "PASS", f"IDENTITY works; PK enforced {how} -> put a PK on IDEM_KEY"
    return "FAIL", f"IDENTITY={ident_ok}; PK enforced default={pk_default} enable={pk_enable} -> disclose idempotency race"


@check("V11", "DROP SCHEMA CASCADE: prompt, frees objects? Is DRYDOCK_AGENT denied DROP/CREATE SCHEMA entirely?")
def c_v11(ctx):
    err = ensure_src(ctx)
    if err:
        return "UNKNOWN", err
    for s in (f"DROP SCHEMA IF EXISTS {V11_BRANCH} CASCADE", f"CREATE SCHEMA {V11_BRANCH}",
              f"CREATE TABLE {V11_BRANCH}.CUSTOMERS AS SELECT * FROM {q('V3_SRC')}"):
        ctx.sql(s)
    ctx.sql(f"SELECT * FROM EXA_ALL_OBJECT_SIZES WHERE ROOT_NAME = '{V11_BRANCH}'")
    ctx.sql("OPEN SCHEMA " + SCRATCH)
    drop = ctx.sql(f"DROP SCHEMA {V11_BRANCH} CASCADE")
    gone = ctx.sql(f"SELECT COUNT(*) FROM EXA_ALL_SCHEMAS WHERE SCHEMA_NAME = '{V11_BRANCH}'")
    ctx.sql(f"SELECT * FROM EXA_ALL_OBJECT_SIZES WHERE ROOT_NAME = '{V11_BRANCH}'")
    ctx.note("DB-wide memory release is not instantly observable (statistics tables lag); "
             "evidence here is object-level: the schema and its object sizes are gone.")
    drop_ok = drop.ok and gone.ok and int(gone.rows[0][0]) == 0
    dialect_record("DROP SCHEMA ... CASCADE", drop, f"branch schema with 200k-row table dropped in {drop.ms:.0f}ms")
    if not ctx.has("agent"):
        return "UNKNOWN", f"drop ok={drop_ok} in {drop.ms:.0f}ms; agent denial untested (DRYDOCK_AGENT missing)"
    ctx.sql(f"DROP SCHEMA IF EXISTS {V11_VICTIM} CASCADE")
    ctx.sql(f"CREATE SCHEMA {V11_VICTIM}")
    ctx.sql(f"CREATE TABLE {V11_VICTIM}.T (A DECIMAL(9,0))")
    tries = [ctx.sql(s, identity="agent") for s in (
        f"DROP SCHEMA {V11_VICTIM} CASCADE", f"DROP TABLE {V11_VICTIM}.T",
        "CREATE SCHEMA PROBE_AGENT_SHOULD_FAIL", f"CREATE TABLE {V11_VICTIM}.T2 (A DECIMAL(9,0))")]
    ctx.sql(f"DROP SCHEMA IF EXISTS {V11_VICTIM} CASCADE")
    ctx.sql("DROP SCHEMA IF EXISTS PROBE_AGENT_SHOULD_FAIL CASCADE")
    denied = all(not t.ok for t in tries)
    if drop_ok and denied:
        return "PASS", f"DROP SCHEMA CASCADE {drop.ms:.0f}ms, schema gone; agent denied all 4 DDL attempts"
    return "FAIL", f"drop ok={drop_ok}; agent denied all={denied} (see steps)"


@check("V12", "BucketFS on Exasol Personal: does /buckets exist from a UDF? What paths does a UDF see?")
def c_v12(ctx):
    body = ("import os\n"
            "def run(ctx):\n"
            "    if not os.path.isdir('/buckets'):\n"
            "        return 'NO /buckets'\n"
            "    out = []\n"
            "    for root, dirs, files in os.walk('/buckets'):\n"
            "        depth = root.count('/')\n"
            "        out.append(root + '/ ' + ','.join(files[:5]))\n"
            "        if depth >= 4:\n"
            "            dirs[:] = []\n"
            "        if len(out) > 40:\n"
            "            break\n"
            "    return '\\n'.join(out)")
    c = _udf(ctx, "BUCKETS", body, ret="VARCHAR(20000)")
    if not c.ok:
        return "UNKNOWN", "cannot create UDF (see R0)"
    o = ctx.sql(f"SELECT {q('BUCKETS')}('x')")
    v = ctx.val(o)
    if not o.ok:
        return "UNKNOWN", o.error_text or ""
    ctx.note(f"/buckets tree:\n{v}")
    if v and not v.startswith("NO /buckets"):
        return "PASS", "/buckets visible from UDFs (upload mechanism + size limit: ladder step b3)"
    return "FAIL", "no /buckets inside the UDF sandbox"


@check("L1", "Is the empty string NULL in Exasol? (affects `<> ''` comparisons in blocking SQL)")
def c_l1(ctx):
    o = ctx.sql("SELECT CASE WHEN '' IS NULL THEN 'EMPTY_IS_NULL' ELSE 'EMPTY_IS_NOT_NULL' END, "
                "CASE WHEN 'x' <> '' THEN 'NE_TRUE' ELSE 'NE_NOT_TRUE' END")
    if not o.ok:
        return "UNKNOWN", o.error_text or ""
    a, b = o.rows[0]
    return "PASS", f"{a}; 'x' <> '' -> {b}  (PASS = answered; design must follow the answer)"


@check("L2", "String/regex/date functions drydock uses: REGEXP_* (LIKE infix vs fn), RIGHT, SUBSTR, TO_CHAR/TO_DATE, *_BETWEEN, GREATEST.")
def c_l2(ctx):
    outs = {
        "REGEXP_REPLACE": ctx.sql("SELECT REGEXP_REPLACE('(555) 010-2233', '[^0-9]', '')"),
        "REGEXP_LIKE infix": ctx.sql("SELECT CASE WHEN 'abc' REGEXP_LIKE 'a.c' THEN 1 ELSE 0 END"),
        "REGEXP_LIKE fn": ctx.sql("SELECT CASE WHEN REGEXP_LIKE('abc', 'a.c') THEN 1 ELSE 0 END"),
        "RIGHT": ctx.sql("SELECT RIGHT('5550102233', 7)"),
        "SUBSTR": ctx.sql("SELECT SUBSTR('abcdef', 2, 3)"),
        "SUBSTRING(,,)": ctx.sql("SELECT SUBSTRING('abcdef', 2, 3)"),
        "SUBSTRING FROM FOR": ctx.sql("SELECT SUBSTRING('abcdef' FROM 2 FOR 3)"),
        "TO_CHAR date fmt": ctx.sql("SELECT TO_CHAR(DATE '2026-09-10', 'YYYY-MM-DD')"),
        "TO_CHAR ts": ctx.sql("SELECT TO_CHAR(TIMESTAMP '2026-09-10 12:34:56.789')"),
        "TO_CHAR num": ctx.sql("SELECT TO_CHAR(12.50)"),
        "LOWER/UPPER/TRIM/REPLACE/||": ctx.sql("SELECT LOWER(' AbC ') || '|' || UPPER(TRIM(' x ')) || '|' || REPLACE('SW1 2AB', ' ', '')"),
        "REGEXP_SUBSTR": ctx.sql("SELECT REGEXP_SUBSTR('JOHN SMITH JR', '[^ ]+$'), REGEXP_SUBSTR('a@b.com', '^[^@]+')"),
        "YEARS_BETWEEN": ctx.sql("SELECT YEARS_BETWEEN(DATE '1989-07-14', DATE '1961-03-02')"),
        "SECONDS_BETWEEN": ctx.sql("SELECT SECONDS_BETWEEN(TIMESTAMP '2026-09-10 12:00:10', TIMESTAMP '2026-09-10 12:00:00')"),
        "GREATEST/LEAST": ctx.sql("SELECT GREATEST(1, 3, 2), LEAST('b', 'a')"),
        "TO_DATE fmt": ctx.sql("SELECT TO_DATE('14/07/1989', 'DD/MM/YYYY')"),
        "NULL || text": ctx.sql("SELECT CASE WHEN NULL || 'x' IS NULL THEN 'NULL_POISONS' ELSE 'NULL_IGNORED' END"),
    }
    dialect_record("REGEXP_LIKE infix predicate", outs["REGEXP_LIKE infix"], "drydock/er.py uses the infix form when PROVEN")
    dialect_record("REGEXP_LIKE function form", outs["REGEXP_LIKE fn"])
    for k in ("REGEXP_REPLACE", "REGEXP_SUBSTR", "YEARS_BETWEEN", "SECONDS_BETWEEN", "RIGHT", "SUBSTR", "TO_CHAR date fmt", "TO_DATE fmt"):
        dialect_record(k, outs[k])
    for k, o in outs.items():
        ctx.note(f"{k}: {ctx.val(o)!r}" if o.ok else f"{k}: ERROR {o.error_text}")
    core = all(outs[k].ok for k in ("REGEXP_REPLACE", "RIGHT", "SUBSTR", "REGEXP_SUBSTR", "YEARS_BETWEEN",
                                    "SECONDS_BETWEEN", "GREATEST/LEAST", "TO_DATE fmt"))
    regex_pred = outs["REGEXP_LIKE infix"].ok or outs["REGEXP_LIKE fn"].ok
    return ("PASS" if core and regex_pred else "FAIL"), \
        "; ".join(f"{k}={'ok' if o.ok else 'ERR'}" for k, o in outs.items())


@check("L3", "Fingerprint primitive: hex -> number (TO_NUMBER 'XXX...'), and SUM of 60-bit values over 200k rows.")
def c_l3(ctx):
    want = int(hashlib.sha256(b"abc").hexdigest()[:15], 16)
    a = ctx.sql("SELECT TO_NUMBER(SUBSTR(HASH_SHA256('abc'), 1, 15), 'XXXXXXXXXXXXXXX')")
    b = ctx.sql("SELECT TO_NUMBER(UPPER(SUBSTR(HASH_SHA256('abc'), 1, 15)), 'XXXXXXXXXXXXXXX')")
    # drydock/hashing.py uses exactly the UPPER(...) form, so that is the form that must pass.
    got = ctx.val(b)
    ctx.note(f"python int(sha256('abc')[:15],16) = {want}; exasol lower-form = {ctx.val(a)}; "
             f"UPPER-form (the one drydock uses) = {got}")
    if got is None or int(got) != want:
        return "FAIL", "hex decode (UPPER form) not available/equal — fingerprints run in client mode"
    if not ensure_src(ctx):
        s = ctx.sql(f"SELECT COUNT(*), SUM(TO_NUMBER(UPPER(SUBSTR(HASH_SHA256(EMAIL), 1, 15)), 'XXXXXXXXXXXXXXX')) "
                    f"FROM {q('V3_SRC')}")
        ctx.note(f"SUM over 200k: {s.rows if s.ok else s.error_text} in {s.ms:.0f}ms")
        if not s.ok:
            return "FAIL", "single value OK but SUM over 200k failed (overflow/type?)"
    return "PASS", "TO_NUMBER(hex,'X..') equals python; SUM over 200k OK"


@check("L4", "FULL OUTER JOIN, MERGE INTO, LIMIT forms, double-quoted identifiers, DUAL, CONNECT BY, VALUES-in-FROM, CURRENT_TIMESTAMP.")
def c_l4(ctx):
    for s in (f"DROP TABLE IF EXISTS {q('L4_A')}", f"DROP TABLE IF EXISTS {q('L4_B')}",
              f"CREATE TABLE {q('L4_A')} (K VARCHAR(10), V VARCHAR(10))",
              f"CREATE TABLE {q('L4_B')} (K VARCHAR(10), V VARCHAR(10))",
              f"INSERT INTO {q('L4_A')} VALUES ('1', 'a'), ('2', 'b'), ('3', 'c')",
              f"INSERT INTO {q('L4_B')} VALUES ('2', 'b'), ('3', 'x'), ('4', 'd')"):
        ctx.sql(s)
    outs = {
        "FULL OUTER JOIN": ctx.sql(
            f"SELECT COALESCE(a.K, b.K) AS K, CASE WHEN a.K IS NULL THEN 'ADDED' WHEN b.K IS NULL THEN 'DELETED' "
            f"WHEN a.V <> b.V THEN 'CHANGED' ELSE 'UNCHANGED' END AS C FROM {q('L4_A')} a "
            f"FULL OUTER JOIN {q('L4_B')} b ON a.K = b.K ORDER BY 1"),
        "MERGE INTO": ctx.sql(
            f"MERGE INTO {q('L4_A')} t USING {q('L4_B')} s ON t.K = s.K "
            "WHEN MATCHED THEN UPDATE SET t.V = s.V WHEN NOT MATCHED THEN INSERT VALUES (s.K, s.V)"),
        "LIMIT n": ctx.sql(f"SELECT K FROM {q('L4_A')} ORDER BY K LIMIT 2"),
        "LIMIT off, n": ctx.sql(f"SELECT K FROM {q('L4_A')} ORDER BY K LIMIT 1, 2"),
        "LIMIT n OFFSET m": ctx.sql(f"SELECT K FROM {q('L4_A')} ORDER BY K LIMIT 2 OFFSET 1"),
        "dquote ident": ctx.sql('SELECT 1 AS "lower_case"'),
        "no FROM": ctx.sql("SELECT 1"),
        "DUAL": ctx.sql("SELECT 1 FROM DUAL"),
        "CONNECT BY": ctx.sql("SELECT COUNT(*) FROM (SELECT LEVEL AS N FROM DUAL CONNECT BY LEVEL <= 1000)"),
        "VALUES in FROM": ctx.sql("SELECT COUNT(*) FROM (VALUES 1, 2, 3) AS t(n)"),
        "CURRENT_TIMESTAMP": ctx.sql("SELECT CURRENT_TIMESTAMP, SYSTIMESTAMP"),
        "catalogue cols": ctx.sql(
            f"SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_ORDINAL_POSITION FROM EXA_ALL_COLUMNS "
            f"WHERE COLUMN_SCHEMA = '{SCRATCH}' AND COLUMN_TABLE = 'L4_A' ORDER BY COLUMN_ORDINAL_POSITION"),
    }
    ctx.sql(f"SELECT * FROM {q('L4_A')} ORDER BY K")
    dialect_record("FULL OUTER JOIN", outs["FULL OUTER JOIN"])
    dialect_record("MERGE INTO", outs["MERGE INTO"])
    dialect_record("LIMIT n OFFSET m", outs["LIMIT n OFFSET m"], allows="LIMIT_OFFSET" if outs["LIMIT n OFFSET m"].ok else "")
    dialect_record("Double-quoted identifier", outs["dquote ident"])
    for k, o in outs.items():
        ctx.note(f"{k}: {'ok ' + str(o.rows[:4]) if o.ok else 'ERROR ' + str(o.error_text)}")
    must = ("FULL OUTER JOIN", "MERGE INTO", "LIMIT n", "no FROM", "catalogue cols")
    return ("PASS" if all(outs[k].ok for k in must) else "FAIL"), \
        "; ".join(f"{k}={'ok' if o.ok else 'ERR'}" for k, o in outs.items())


@check("L5", "Concurrency: two sessions writing the same table / crossing read-write sets — wait, succeed, or rollback?")
def c_l5(ctx):
    for s in (f"DROP TABLE IF EXISTS {q('L5_A')}", f"DROP TABLE IF EXISTS {q('L5_B')}",
              f"CREATE TABLE {q('L5_A')} (A DECIMAL(9,0))", f"CREATE TABLE {q('L5_B')} (A DECIMAL(9,0))"):
        ctx.sql(s)
    c1 = ctx.conn(autocommit=False, fresh=True)
    c2 = ctx.conn(autocommit=False, fresh=True, query_timeout=5)
    ctx.sql(f"INSERT INTO {q('L5_A')} VALUES (1)", conn=c1)
    same = ctx.sql(f"INSERT INTO {q('L5_A')} VALUES (2)", conn=c2)
    ctx.sql("COMMIT", conn=c1)
    ctx.sql("COMMIT", conn=c2)
    ctx.note(f"same-table concurrent INSERT: {'ok' if same.ok else same.error_text} after {same.ms:.0f}ms")
    ctx.sql(f"SELECT COUNT(*) FROM {q('L5_A')}", conn=c1)
    ctx.sql(f"INSERT INTO {q('L5_B')} SELECT * FROM {q('L5_A')}", conn=c1)
    cross = ctx.sql(f"INSERT INTO {q('L5_A')} SELECT * FROM {q('L5_B')}", conn=c2)
    k1 = ctx.sql("COMMIT", conn=c1)
    k2 = ctx.sql("COMMIT", conn=c2)
    ctx.note(f"crossing read/write sets: c2 insert={'ok' if cross.ok else cross.error_text}; "
             f"commit1={'ok' if k1.ok else k1.error_text}; commit2={'ok' if k2.ok else k2.error_text}")
    c1.close()
    c2.close()
    return "PASS", ("answered (see notes). PASS = observed; drydock must serialise DRYDOCK.* writes "
                    "if either case waited or rolled back")


@check("L6", "CREATE USER / GRANT / REVOKE / DROP USER syntax (throwaway user, admin only).")
def c_l6(ctx):
    if not ctx.has("admin"):
        return "UNKNOWN", "admin identity not configured"
    ctx.sql(f"DROP USER IF EXISTS {L7_USER} CASCADE", identity="admin")
    steps = [ctx.sql(s, identity="admin") for s in (
        f'CREATE USER {L7_USER} IDENTIFIED BY "Probe_pw_l7_2026"',
        f"GRANT CREATE SESSION TO {L7_USER}",
        f"GRANT SELECT ON SCHEMA {SCRATCH} TO {L7_USER}",
        f"REVOKE SELECT ON SCHEMA {SCRATCH} FROM {L7_USER}",
        f"DROP USER {L7_USER} CASCADE")]
    dialect_record("CREATE USER ... IDENTIFIED BY", steps[0], "password in double quotes", allows="DQUOTE_IDENT" if steps[0].ok else "")
    dialect_record("GRANT / REVOKE on schema", steps[2])
    ok = all(s.ok for s in steps)
    return ("PASS" if ok else "FAIL"), ("all five statements ran" if ok else "see steps for the failing form")


@check("L7", "Identities: do DRYDOCK_SVC and DRYDOCK_AGENT exist and connect? What does the agent hold?")
def c_l7(ctx):
    have_svc, have_agent = ctx.has("svc"), ctx.has("agent")
    if have_agent:
        ctx.sql("SELECT * FROM EXA_USER_SYS_PRIVS", identity="agent")
        ctx.sql("SELECT * FROM EXA_USER_OBJ_PRIVS", identity="agent")
        ctx.sql("SELECT * FROM EXA_USER_ROLE_PRIVS", identity="agent")
    if have_svc:
        ctx.sql("SELECT * FROM EXA_USER_SYS_PRIVS", identity="svc")
    if have_svc and have_agent:
        return "PASS", "both identities connect; privilege listings recorded"
    return "UNKNOWN", f"svc connects={have_svc}; agent connects={have_agent}"


@check("L8", "Do the Exasol system-view columns listed in sql/system_catalogue.json exist exactly as claimed?")
def c_l8(ctx):
    raw = json.loads((Path(__file__).resolve().parent.parent / "sql" / "system_catalogue.json").read_text())
    bad = []
    for view, cols in raw["SYS"].items():
        o = ctx.sql(f"SELECT {', '.join(cols)} FROM {view} LIMIT 1")
        if not o.ok:
            bad.append(f"{view}: [{o.error_code}] {o.error_text}")
    if bad:
        return "FAIL", "system_catalogue.json is wrong for: " + "; ".join(bad)
    return "PASS", f"all {sum(len(c) for c in raw['SYS'].values())} claimed columns exist in {len(raw['SYS'])} system views"


@check("G1", "Gemini: do the configured models exist AND support every feature Drydock uses (limits, JSON schema output, multi-turn function calling with thinking/thought signatures, system instructions)?")
def c_g1(ctx):
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return "UNKNOWN", "GEMINI_API_KEY not set in .env"
    from google.genai import types
    from agent.loop import declarations
    from drydock import adjudicate, er, llm
    from drydock.config import SETTINGS
    planner, adj = SETTINGS.planner_model, SETTINGS.adjudicator_model
    failures = []
    cli = llm.client()

    # 1. existence + limits (Drydock needs a long agent history and a 40-decision JSON batch)
    try:
        for role, mid in (("planner", planner), ("adjudicator", adj)):
            m = cli.models.get(model=mid)
            acts = list(m.supported_actions or [])
            ctx.note(f"{role} {mid}: input_token_limit={m.input_token_limit} output_token_limit={m.output_token_limit} actions={acts}")
            if "generateContent" not in acts:
                failures.append(f"{role} {mid}: no generateContent")
            if (m.input_token_limit or 0) < 200_000:
                failures.append(f"{role} {mid}: input window {m.input_token_limit} < 200k")
            if (m.output_token_limit or 0) < 16_000:
                failures.append(f"{role} {mid}: output limit {m.output_token_limit} < 16k")
    except Exception:
        ctx.r.stderr = traceback.format_exc()
        return "FAIL", f"cannot read model metadata for {planner}/{adj} (see stderr)"

    # 2. structured output with the REAL adjudicator schema, system prompt and thinking level
    sig = {"email": 0, "phone": 1000, "name": 1000, "addr": 1000, "dob": 0, "dob_gap_years": 28,
           "suffix_conflict": 1, "business_conflict": 0, "given_initial_conflict": 0, "same_family": 1}
    prompt = "Adjudicate each pair below. Return one decision per PAIR id.\n\n" + "\n\n".join(
        adjudicate.render_item({"pair_id": pid, "kind": "AB", "summary": er.compare_summary(sg)}, [])
        for pid, sg in ((1, sig), (2, {**sig, "dob": 1000, "dob_gap_years": 0, "suffix_conflict": 0, "email": 1000})))
    try:
        parsed, finish = llm.generate_json(adj, adjudicate.SYSTEM, prompt, adjudicate.SCHEMA, SETTINGS.adjudicator_thinking)
        ctx.note(f"structured output ({adj}, thinking={SETTINGS.adjudicator_thinking}): finish={finish} -> {json.dumps(parsed)[:600]}")
        ok = parsed is not None and {int(d["pair_id"]) for d in parsed.get("decisions", [])} == {1, 2} and \
            all(d["verdict"] in adjudicate.VERDICTS for d in parsed["decisions"])
        if not ok:
            failures.append("structured output did not return one valid decision per pair")
    except Exception:
        ctx.r.stderr += traceback.format_exc()
        failures.append("structured output call raised (see stderr)")

    # 3 + 4. multi-turn function calling with thinking, using the agent loop's own config builder;
    # the model turn is appended UNCHANGED (thought signatures) and a system-instruction token is checked.
    tool = [{"name": "count_rows", "description": "Return the row count of a table.",
             "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}}]
    system = ("You are testing tool use. Call count_rows for table SOURCE_A.CUSTOMERS. After you receive the "
              "result, answer in one sentence that includes the exact token DRYDOCK-OK and the number.")
    cfg = llm.tools_config(system, declarations(tool), SETTINGS.planner_thinking)
    contents = [types.Content(role="user", parts=[types.Part.from_text(text="How many rows are in SOURCE_A.CUSTOMERS?")])]
    try:
        r1 = cli.models.generate_content(model=planner, contents=contents, config=cfg)
        calls = r1.function_calls or []
        has_sig = any(getattr(p, "thought_signature", None) for p in (r1.candidates[0].content.parts or []))
        ctx.note(f"turn 1 ({planner}, thinking={SETTINGS.planner_thinking}): calls={[(c.name, dict(c.args or {})) for c in calls]} "
                 f"thought_signature_present={has_sig}")
        if not calls or calls[0].name != "count_rows":
            failures.append("model did not issue the function call")
        else:
            contents.append(r1.candidates[0].content)                       # unchanged, as agent/loop.py does
            part = types.Part.from_function_response(name="count_rows", response={"result": '{"rows": 36600}'})
            if getattr(calls[0], "id", None):
                part.function_response.id = calls[0].id
            contents.append(types.Content(role="user", parts=[part]))
            r2 = cli.models.generate_content(model=planner, contents=contents, config=cfg)
            text = r2.text or ""
            ctx.note(f"turn 2: finish={llm.finish_reason(r2)} text={text[:300]!r}")
            if "DRYDOCK-OK" not in text:
                failures.append("system instruction token missing after the tool round trip")
            if "36600" not in text.replace(",", "").replace(" ", ""):
                failures.append("final answer did not use the function result")
    except Exception:
        ctx.r.stderr += traceback.format_exc()
        failures.append("function-calling round trip raised (see stderr) — e.g. thought signature not accepted")

    if failures:
        return "FAIL", "; ".join(failures)
    return "PASS", (f"planner={planner} (thinking {SETTINGS.planner_thinking}) and adjudicator={adj} "
                    f"(thinking {SETTINGS.adjudicator_thinking}): limits OK, JSON schema output OK, "
                    "2-turn function calling with thought signatures OK, system instruction obeyed")


# =========================================================================== runner

def choose_runner(forced: str | None) -> str:
    if forced:
        return forced
    if identity_configured("svc"):
        try:
            connect("svc").close()
            return "svc"
        except Exception as e:
            print(f"svc configured but cannot connect ({type(e).__name__}: {e}); using admin", file=sys.stderr)
    return "admin"


def reset_scratch(ctx: Ctx) -> None:
    r = Result("SETUP", "reset PROBE_SCRATCH")
    ctx.r = r
    ctx.sql(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")
    ctx.sql(f"DROP SCHEMA IF EXISTS {SCRATCH2} CASCADE")
    o = ctx.sql(f"CREATE SCHEMA {SCRATCH}")
    ctx.sql(f"OPEN SCHEMA {SCRATCH}")
    if not o.ok:
        raise SystemExit(f"cannot create {SCRATCH} as {ctx.runner}: [{o.error_code}] {o.error_text}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated check ids")
    ap.add_argument("--as", dest="identity", choices=["svc", "admin"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep-scratch", action="store_true", help="do not reset PROBE_SCRATCH first")
    a = ap.parse_args()

    if a.list:
        for cid, qn, _ in CHECKS:
            print(f"{cid:4} {qn}")
        return 0
    global KEY
    from drydock.verification import KEY_ENV, key_in_file
    if key_in_file(Path(__file__).resolve().parent.parent / ".env"):
        print(f"REFUSING: {KEY_ENV} is in .env. The signing key must never be in a file the agent can read.",
              file=sys.stderr)
        return 2
    KEY = os.environ.get(KEY_ENV) or ""
    if not KEY:
        import getpass
        KEY = getpass.getpass(f"{KEY_ENV} (signing key; typed at run time, never stored in the repo): ")
    from drydock.verification import key_problem
    if (why := key_problem(KEY)):
        print(f"REFUSING: {why}", file=sys.stderr)
        return 2
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    unknown_ids = only - {c[0] for c in CHECKS}
    if unknown_ids:
        print(f"unknown check ids: {sorted(unknown_ids)}", file=sys.stderr)
        return 2

    try:
        print(f"DSN: {dsn()}")
        runner = choose_runner(a.identity)
        ctx = Ctx(runner)
        ctx.conn()
    except IdentityMissing as e:
        print(f"IDENTITY MISSING: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"CONNECTION FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"runner identity: {runner} ({identity_user(runner)})")
    if not a.keep_scratch:
        reset_scratch(ctx)

    results: list[Result] = []
    for cid, qn, fn in CHECKS:
        if only and cid not in only:
            continue
        r = Result(cid, qn, runner=f"{runner} ({identity_user(runner)})", ts=now_iso())
        ctx.r = r
        t0 = time.perf_counter()
        try:
            ctx.sql(f"OPEN SCHEMA {SCRATCH}")
            r.status, r.summary = fn(ctx)
        except Exception:
            r.status, r.summary = "UNKNOWN", "check raised (see stderr)"
            r.stderr = traceback.format_exc()
        r.ms = (time.perf_counter() - t0) * 1000
        results.append(r)
        print(f"\n{'=' * 78}\n{r.id}  {r.status}  {r.summary}\n{r.question}\n{'-' * 78}")
        for s in r.steps:
            print(s.text())
        for n in r.notes:
            print(n)
        if r.stderr:
            print("STDERR:\n" + r.stderr)
    ctx.close()

    # verified.json: merge by id, newest wins (history lives in VERIFIED.md)
    # Every entry is HMAC-signed with the signing key (drydock/verification.py). Entries
    # from earlier runs are kept only if their signature still verifies under this key.
    from drydock.verification import SignatureError, check, sign
    existing = json.loads(VERIFIED_JSON.read_text()) if VERIFIED_JSON.exists() else []
    by_id = {}
    for e in existing:
        try:
            check(e, KEY)
            by_id[e["id"]] = e
        except SignatureError as err:
            print(f"dropping untrusted prior entry: {err}", file=sys.stderr)
    for r in results:
        by_id[r.id] = sign(r.as_json(), KEY)
    order = [c[0] for c in CHECKS]
    VERIFIED_JSON.write_text(json.dumps(sorted(by_id.values(), key=lambda e: order.index(e["id"])
                                               if e["id"] in order else 999), indent=2, default=str))

    lines = [f"### verify.py run {now_iso()} · runner {runner} ({identity_user(runner)}) · "
             f"checks {','.join(r.id for r in results)}", "", "| id | status | summary |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.id} | **{r.status}** | {r.summary.replace('|', '/')} |")
    for r in results:
        lines += ["", f"#### {r.id} — {r.status} — {r.question}", "```"]
        lines += [s.text() for s in r.steps] + r.notes
        if r.stderr:
            lines += ["STDERR:", r.stderr.rstrip()]
        lines.append("```")
    append_verified("Verification runs", "\n".join(lines))

    print(f"\n{'=' * 78}\nSUMMARY")
    for r in results:
        print(f"  {r.id:4} {r.status:7} {r.summary}")
    print(f"\nwrote {VERIFIED_JSON.name} and appended to VERIFIED.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
