"""In-database model ladder. Each rung runs real SQL/CLI against the instance, prints the
complete output, appends it to VERIFIED.md and writes a signed entry to ladder.json
(same HMAC key as verify.py). adjudicate.py uses the in-database adjudicator only after
rung b6 is a signed PASS.

    uv run python scripts/ladder.py b0          # trivial PYTHON3 UDF
    uv run python scripts/ladder.py b1          # stdlib imports in a UDF
    uv run python scripts/ladder.py b2          # numpy in a UDF (non-stdlib, no BucketFS)
    uv run python scripts/ladder.py b3-token    # prints a token; upload a file containing it to BucketFS
    uv run python scripts/ladder.py b3 --path /buckets/<service>/<bucket>/drydock_ladder.txt
    uv run python scripts/ladder.py b4-help     # records `python -m exasol_transformers_extension.deploy --help`
    uv run python scripts/ladder.py b4 -- <deploy flags as --help documents>
    uv run python scripts/ladder.py b5 --schema <S> [--model ... --task ...]     # small model, one inference
    uv run python scripts/ladder.py b6 --schema <S> [--model ... --task ...]     # the NLI adjudicator over 100 pairs
"""

from __future__ import annotations

__dialect_lint__ = "exempt"  # rungs deliberately run unproven SQL on the probe path

import argparse
import datetime as dt
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _common import SCRATCH, append_verified, connect, identity_user, now_iso, run  # noqa: E402

from drydock.verification import KEY_ENV, SignatureError, check, key_in_file, key_problem, sign  # noqa: E402

LADDER_JSON = ROOT / "ladder.json"
TIMEBOX_MIN = 90
TINY_MODEL = ("distilbert/distilbert-base-uncased-finetuned-sst-2-english", "text-classification")
NLI_MODEL = ("MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli", "text-classification")
HYPOTHESIS = "The two records describe the same real person."


def _key() -> str:
    if key_in_file(ROOT / ".env"):
        sys.exit(f"REFUSING: {KEY_ENV} is in .env; it must never be in a file the agent can read.")
    k = os.environ.get(KEY_ENV)
    if not k:
        import getpass
        k = getpass.getpass(f"{KEY_ENV} (the same signing key you gave verify.py): ")
    if (why := key_problem(k)):
        sys.exit(f"REFUSING: {why}")
    return k


def _load(key: str) -> tuple[list[dict], dict]:
    if not LADDER_JSON.exists():
        return [], {}
    raw = json.loads(LADDER_JSON.read_text())
    good = []
    for e in raw.get("entries", []):
        try:
            check(e, key)
            good.append(e)
        except SignatureError as err:
            print(f"dropping untrusted ladder entry: {err}", file=sys.stderr)
    return good, raw.get("meta", {})


def _save(entries: list[dict], meta: dict) -> None:
    LADDER_JSON.write_text(json.dumps({"meta": meta, "entries": entries}, indent=2, default=str))


def _timebox(meta: dict, override: bool) -> None:
    start = meta.setdefault("started_at", dt.datetime.now().isoformat(timespec="seconds"))
    mins = (dt.datetime.now() - dt.datetime.fromisoformat(start)).total_seconds() / 60
    print(f"[ladder] {mins:.0f} of {TIMEBOX_MIN} minutes used (first rung at {start})")
    if mins > TIMEBOX_MIN and not override:
        sys.exit("[ladder] TIME-BOX EXCEEDED. The Gemini adjudicator stays in use; the reason is in VERIFIED.md. "
                 "Re-run with --override-timebox to spend more time.")


def _record(rung: str, question: str, status: str, summary: str, steps: list[str], stderr: str = "") -> None:
    key = _key()
    entries, meta = _load(key)
    entries = [e for e in entries if e["id"] != rung]
    stdout = "\n".join(steps)
    entries.append(sign({"id": rung, "question": question, "status": status, "summary": summary,
                         "sql": "", "stdout": stdout, "stderr": stderr, "ms": 0,
                         "runner": identity_user("svc") or identity_user("admin"), "ts": now_iso()}, key))
    _save(sorted(entries, key=lambda e: e["id"]), meta)
    append_verified("BucketFS ladder", f"### {now_iso()} · {rung} · **{status}** · {summary}\n```\n{stdout}\n{stderr}\n```")
    print(f"\n{rung}: {status} — {summary}\nrecorded -> ladder.json (signed) and VERIFIED.md")


def _conn():
    ident = "svc" if os.environ.get("DRYDOCK_SVC_USER") else "admin"
    c = connect(ident)
    run(c, f"CREATE SCHEMA IF NOT EXISTS {SCRATCH}")
    run(c, f"OPEN SCHEMA {SCRATCH}")
    return c


def _sql(c, sql: str, steps: list[str]):
    o = run(c, sql)
    steps.append(f"> {sql}\n{o.render(20)}")
    print(steps[-1])
    return o


def _udf(c, name: str, body: str, steps: list[str], ret: str = "VARCHAR(20000)"):
    return _sql(c, f"CREATE OR REPLACE PYTHON3 SCALAR SCRIPT {SCRATCH}.{name}(x VARCHAR(2000)) RETURNS {ret} AS\n{body}\n", steps)


def b0(a):
    steps: list[str] = []
    c = _conn()
    _udf(c, "LADDER_HELLO", "def run(ctx):\n    return 'hi ' + ctx.x", steps)
    o = _sql(c, f"SELECT {SCRATCH}.LADDER_HELLO('there')", steps)
    ok = o.ok and o.rows and o.rows[0][0] == "hi there"
    _record("B0", "Can a trivial PYTHON3 UDF be created and called?", "PASS" if ok else "FAIL",
            "returned 'hi there'" if ok else (o.error_text or "wrong result"), steps)


def b1(a):
    steps: list[str] = []
    c = _conn()
    _udf(c, "LADDER_STDLIB", "import json, hashlib, re\ndef run(ctx):\n    return json.dumps("
         "{'h': hashlib.sha256(ctx.x.encode()).hexdigest()[:8], 'd': re.sub('[^0-9]', '', ctx.x)})", steps)
    o = _sql(c, f"SELECT {SCRATCH}.LADDER_STDLIB('(555) 010-2233')", steps)
    _record("B1", "Do stdlib imports work inside a UDF?", "PASS" if o.ok else "FAIL",
            str(o.rows[0][0]) if o.ok else (o.error_text or ""), steps)


def b2(a):
    steps: list[str] = []
    c = _conn()
    _udf(c, "LADDER_NUMPY", "def run(ctx):\n    import numpy\n    return numpy.__version__", steps)
    o = _sql(c, f"SELECT {SCRATCH}.LADDER_NUMPY('x')", steps)
    _record("B2", "Can a UDF import numpy without BucketFS work?", "PASS" if o.ok else "FAIL",
            f"numpy {o.rows[0][0]}" if o.ok else f"not importable: {o.error_text} -> a language container is REQUIRED",
            steps)


def b3_token(a):
    tok = secrets.token_hex(16)
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "ladder_b3_token.txt").write_text(tok)
    print(f"Upload a ~1KB text file named drydock_ladder.txt whose FIRST LINE is:\n\n    {tok}\n\n"
          "to your BucketFS bucket, using the BucketFS endpoint and write credentials your Exasol Personal "
          "install reports (the launcher output / its docs). Then run:\n"
          "    uv run python scripts/ladder.py b3 --path /buckets/<service>/<bucket>/drydock_ladder.txt\n"
          "(`verify.py --only V12` lists the /buckets tree a UDF sees, to find <service>/<bucket>.)")


def b3(a):
    steps: list[str] = []
    tok = (ROOT / "runs" / "ladder_b3_token.txt").read_text().strip()
    c = _conn()
    body = ("def run(ctx):\n    try:\n        with open(ctx.x) as f:\n            return f.readline().strip()\n"
            "    except Exception as e:\n        return 'ERROR ' + type(e).__name__ + ': ' + str(e)")
    _udf(c, "LADDER_READFILE", body, steps)
    o = _sql(c, f"SELECT {SCRATCH}.LADDER_READFILE('{a.path}')", steps)
    got = o.rows[0][0] if o.ok and o.rows else None
    ok = got == tok
    _record("B3", "Is a file uploaded to BucketFS readable from a UDF at the expected path?", "PASS" if ok else "FAIL",
            f"read the token from {a.path}" if ok else f"got {got!r} (expected the b3 token)", steps)


def _cli(cmd: list[str], steps: list[str]) -> subprocess.CompletedProcess:
    shown = re.sub(r"(--[\w-]*pass[\w-]*(?:=|\s+))(\S+)", r"\1***", " ".join(shlex.quote(x) for x in cmd), flags=re.I)
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    steps.append(f"$ {shown}\n(exit {p.returncode}, {time.perf_counter() - t0:.0f}s)\n{p.stdout[-20000:]}")
    print(steps[-1])
    if p.stderr:
        print(p.stderr[-8000:], file=sys.stderr)
    return p


def b4_help(a):
    steps: list[str] = []
    p = _cli(["uv", "run", "--with", "exasol-transformers-extension", "python", "-m",
              "exasol_transformers_extension.deploy", "--help"], steps)
    _record("B4H", "What are the exact deploy flags on this installed version?", "PASS" if p.returncode == 0 else "FAIL",
            "deploy --help captured; build the b4 command from it" if p.returncode == 0 else "deploy --help failed",
            steps, p.stderr[-8000:])


def b4(a):
    steps: list[str] = []
    if not a.flags:
        sys.exit("give the deploy flags after --, exactly as `ladder.py b4-help` documented them")
    p = _cli(["uv", "run", "--with", "exasol-transformers-extension", "python", "-m",
              "exasol_transformers_extension.deploy", *a.flags], steps)
    c = _conn()
    o = _sql(c, "SELECT SCRIPT_SCHEMA, SCRIPT_NAME FROM EXA_ALL_SCRIPTS WHERE SCRIPT_NAME IN "
                "('AI_ENTAILMENT_EXTENDED', 'AI_CUSTOM_CLASSIFY_EXTENDED', 'TE_MODEL_DOWNLOADER_UDF', 'TE_LIST_MODELS_UDF')",
             steps)
    found = {r[1]: r[0] for r in o.rows} if o.ok else {}
    ok = p.returncode == 0 and {"AI_ENTAILMENT_EXTENDED", "TE_MODEL_DOWNLOADER_UDF"} <= set(found)
    _record("B4", "Did the transformers-extension deploy (language container + UDF scripts)?", "PASS" if ok else "FAIL",
            f"scripts in schema {sorted(set(found.values()))}: {sorted(found)}" if ok else "deploy failed or scripts missing",
            steps, p.stderr[-8000:])


def _download(c, a, model: str, task: str, steps: list[str]) -> bool:
    o = _sql(c, f"SELECT {a.schema}.TE_MODEL_DOWNLOADER_UDF('{a.conn}', '{a.sub_dir}', '{model}', '{task}', '')", steps)
    _sql(c, f"SELECT {a.schema}.TE_LIST_MODELS_UDF('{a.conn}', '{a.sub_dir}')", steps)
    return o.ok


def b5(a):
    steps: list[str] = []
    c = _conn()
    model, task = a.model or TINY_MODEL[0], a.task or TINY_MODEL[1]
    t0 = time.perf_counter()
    if not _download(c, a, model, task, steps):
        return _record("B5", "Does a small model download into BucketFS and run one inference?", "FAIL",
                       "model download failed (see output)", steps)
    dl_s = time.perf_counter() - t0
    o = _sql(c, f"SELECT {a.schema}.AI_CUSTOM_CLASSIFY_EXTENDED(NULL, '{a.conn}', '{a.sub_dir}', '{model}', "
                "'The parcel arrived broken and nobody answered the phone.', 'HIGHEST')", steps)
    steps.append(f"emitted columns: {o.columns}")
    _record("B5", "Does a small model download into BucketFS and run one inference?", "PASS" if o.ok and o.rows else "FAIL",
            f"{model}: download {dl_s:.0f}s, inference {o.ms:.0f}ms, columns {o.columns}" if o.ok else (o.error_text or ""),
            steps)


def b6(a):
    from drydock.adjudicate import HYPOTHESIS as H
    from drydock.er import compare_summary
    import random
    steps: list[str] = []
    c = _conn()
    model, task = a.model or NLI_MODEL[0], a.task or NLI_MODEL[1]
    if not _download(c, a, model, task, steps):
        return _record("B6", "Does the NLI adjudicator run in-database over 100 pairs?", "FAIL", "model download failed", steps)
    r = random.Random(7)
    _sql(c, f"CREATE OR REPLACE TABLE {SCRATCH}.LADDER_PAIRS (FIRST_TEXT VARCHAR(2000), SECOND_TEXT VARCHAR(2000))", steps)
    rows = []
    for _ in range(100):
        sig = {"name": r.choice([400, 700, 900, 1000]), "same_family": r.choice([0, 1]), "email": r.choice([None, 0, 600, 1000]),
               "phone": r.choice([None, 0, 700, 1000]), "addr": r.choice([0, 500, 1000]), "dob": r.choice([None, 0, 1000]),
               "dob_gap_years": r.choice([None, 0, 3, 28]), "suffix_conflict": r.choice([0, 0, 1]),
               "business_conflict": r.choice([0, 0, 1]), "given_initial_conflict": r.choice([0, 1])}
        rows.append((compare_summary(sig), H))
    c.import_from_iterable(rows, (SCRATCH, "LADDER_PAIRS"))
    o = _sql(c, f"SELECT {a.schema}.AI_ENTAILMENT_EXTENDED(NULL, '{a.conn}', '{a.sub_dir}', '{model}', "
                f"t.FIRST_TEXT, t.SECOND_TEXT, 'HIGHEST') FROM {SCRATCH}.LADDER_PAIRS t", steps)
    steps.append(f"emitted columns: {o.columns}")
    labels: dict[str, int] = {}
    errors = 0
    if o.ok:
        cols = [x.upper() for x in o.columns]
        li = next((cols.index(n) for n in ("LABEL", "_LABEL_") if n in cols), None)
        ei = next((cols.index(n) for n in ("ERROR_MESSAGE", "_ERROR_MESSAGE_") if n in cols), None)
        for row in o.rows:
            if ei is not None and row[ei]:
                errors += 1
            elif li is not None:
                labels[str(row[li])] = labels.get(str(row[li]), 0) + 1
    ok = o.ok and sum(labels.values()) >= 95
    summary = (f"{model}: {sum(labels.values())} labelled / {errors} errors in {o.ms / 1000:.1f}s "
               f"({o.ms / 100:.0f} ms/pair); labels {labels}; columns {o.columns}") if o.ok else (o.error_text or "")
    if ok:
        print("\nSet these in the terminal that runs the orchestrator, then DRYDOCK_ADJUDICATOR=exasol_ai_precedent:\n"
              f"  DRYDOCK_AI_SCHEMA={a.schema}\n  DRYDOCK_AI_BUCKETFS_CONN={a.conn}\n  DRYDOCK_AI_SUBDIR={a.sub_dir}\n"
              f"  DRYDOCK_AI_MODEL={model}\n(and check the model licence; record it in VERIFIED.md)")
    _record("B6", "Does the NLI adjudicator run in-database over 100 pairs?", "PASS" if ok else "FAIL", summary, steps)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rung", choices=["b0", "b1", "b2", "b3-token", "b3", "b4-help", "b4", "b5", "b6", "status"])
    ap.add_argument("--path")
    ap.add_argument("--schema", help="schema the extension deployed its scripts into (from b4)")
    ap.add_argument("--conn", default="EXA_AI_MODEL_LOCATION", help="BucketFS connection name (extension default)")
    ap.add_argument("--sub-dir", dest="sub_dir", default="drydock_models")
    ap.add_argument("--model")
    ap.add_argument("--task")
    ap.add_argument("--override-timebox", action="store_true")
    ap.add_argument("flags", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.flags and a.flags[0] == "--":
        a.flags = a.flags[1:]
    key = _key()
    entries, meta = _load(key)
    if a.rung == "status":
        for e in entries:
            print(f"{e['id']:4} {e['status']:5} {e['summary']}")
        print(json.dumps(meta))
        return 0
    _timebox(meta, a.override_timebox)
    _save(entries, meta)
    if a.rung in ("b5", "b6") and not a.schema:
        sys.exit("--schema is required: the schema b4 reported the extension's scripts in")
    {"b0": b0, "b1": b1, "b2": b2, "b3-token": b3_token, "b3": b3, "b4-help": b4_help, "b4": b4,
     "b5": b5, "b6": b6}[a.rung](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
