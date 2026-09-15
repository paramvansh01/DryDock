"""Settings and the verification guard.

require_verified() is called at the entry of every function whose correctness
depends on a property of the Exasol instance established by scripts/verify.py.
A missing, failed or altered verification raises immediately and names the check.
It is called per function rather than at import, so pure logic stays testable offline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
VERIFIED_JSON = Path(os.environ.get("DRYDOCK_VERIFIED_JSON", ROOT / "verified.json"))
LADDER_JSON = Path(os.environ.get("DRYDOCK_LADDER_JSON", ROOT / "ladder.json"))


class NotVerified(RuntimeError):
    pass


def _signing_key(key: str | None) -> str:
    from .verification import KEY_ENV, is_published, key_from_env, key_in_file
    if key_in_file(ROOT / ".env"):
        raise NotVerified(f"{KEY_ENV} found in .env — the signing key must never live in a file the coding "
                          "agent can read. Remove it from .env and export it only in your own terminal.")
    k = key or key_from_env()
    if not k:
        raise NotVerified(f"{KEY_ENV} is not set in this process, so verified.json signatures cannot be "
                          "checked and no verification can be trusted. Export it in the terminal that runs "
                          "the orchestrator/tests (the same key you typed into scripts/verify.py).")
    if is_published(k):
        raise NotVerified(f"{KEY_ENV} is an example value from the documentation, so signatures made with it "
                          "prove nothing. Choose your own secret and re-run scripts/verify.py.")
    return k


def verified_status(check_id: str, path: Path | None = None, key: str | None = None) -> str:
    """PASS | FAIL | UNKNOWN from verified.json — or TAMPERED if the entry's HMAC does not
    verify, UNSIGNED-KEY-MISSING if the key is not available. Only signed entries count."""
    from .verification import SignatureError, check
    # B* = in-database model ladder steps, signed by scripts/ladder.py.
    path = path or (LADDER_JSON if check_id.startswith("B") else VERIFIED_JSON)
    if not path.exists():
        return "UNKNOWN"
    raw = json.loads(path.read_text())
    entries = raw.get("entries", []) if isinstance(raw, dict) else raw   # ladder.json wraps entries with meta
    for entry in entries:
        if entry.get("id") == check_id:
            try:
                check(entry, _signing_key(key))
            except NotVerified:
                return "UNSIGNED-KEY-MISSING"
            except SignatureError:
                return "TAMPERED"
            return entry.get("status", "UNKNOWN")
    return "UNKNOWN"


def require_verified(*check_ids: str, path: Path | None = None, key: str | None = None) -> None:
    """Raise unless every named check is a correctly SIGNED PASS in verified.json."""
    _signing_key(key)
    bad = {c: verified_status(c, path, key) for c in check_ids}
    bad = {c: s for c, s in bad.items() if s != "PASS"}
    if bad:
        detail = ", ".join(f"{c}={s}" for c, s in bad.items())
        if any(s == "TAMPERED" for s in bad.values()):
            raise NotVerified(f"verified.json was altered outside scripts/verify.py: {detail}. "
                              "Only scripts/verify.py may write it. Re-run it.")
        raise NotVerified(
            f"required verification not PASS: {detail}. Run "
            f"`uv run python scripts/verify.py --only {','.join(bad)}` against your Exasol instance.")


def require_answered(*check_ids: str, path: Path | None = None, key: str | None = None) -> dict[str, str]:
    """For facts where the code implements BOTH outcomes (e.g. V4: DDL transactional
    or not). Refuses to run on UNKNOWN — the fact must have been observed — and
    returns the observed statuses so the caller takes the matching path."""
    _signing_key(key)
    got = {c: verified_status(c, path, key) for c in check_ids}
    unknown = [c for c, s in got.items() if s not in ("PASS", "FAIL")]
    if unknown:
        raise NotVerified(f"verification not answered: {', '.join(f'{c}=UNKNOWN' for c in unknown)}. "
                          f"Run `uv run python scripts/verify.py --only {','.join(unknown)}`.")
    return got


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    max_open_branches: int = field(default_factory=lambda: _i("DRYDOCK_MAX_OPEN_BRANCHES", 8))
    branch_ttl_seconds: int = field(default_factory=lambda: _i("DRYDOCK_BRANCH_TTL", 1800))
    materialisation_ceiling: int = field(default_factory=lambda: _i("DRYDOCK_MAT_CEILING", 5_000_000))
    diff_ceiling: int = field(default_factory=lambda: _i("DRYDOCK_DIFF_CEILING", 5_000_000))
    hard_delete_pct: float = field(default_factory=lambda: _f("DRYDOCK_HARD_DELETE_PCT", 5.0))
    default_tier: int = field(default_factory=lambda: _i("DRYDOCK_TIER", 2))
    sample_rows: int = field(default_factory=lambda: _i("DRYDOCK_SAMPLE_ROWS", 50))
    ui_rows_cap: int = field(default_factory=lambda: _i("DRYDOCK_UI_ROWS", 400))
    # "sql" needs check L3 (hex -> number in SQL); "client" streams row hashes
    # (not PII) and sums them in Python. Both are order-independent.
    fingerprint_mode: str = field(default_factory=lambda: os.environ.get("DRYDOCK_FINGERPRINT_MODE", "auto"))
    planner_model: str = field(default_factory=lambda: os.environ.get("DRYDOCK_PLANNER_MODEL", "gemini-3.5-flash"))
    # Gemini 3 thinking levels: MINIMAL | LOW | MEDIUM | HIGH (model default HIGH).
    planner_thinking: str = field(default_factory=lambda: os.environ.get("DRYDOCK_PLANNER_THINKING", "HIGH"))
    adjudicator_model: str = field(default_factory=lambda: os.environ.get("DRYDOCK_ADJ_MODEL", "gemini-3.5-flash"))
    adjudicator_thinking: str = field(default_factory=lambda: os.environ.get("DRYDOCK_ADJ_THINKING", "MEDIUM"))
    adjudicator_rung: str = field(default_factory=lambda: os.environ.get("DRYDOCK_ADJUDICATOR", "gemini_fallback"))
    adj_batch: int = field(default_factory=lambda: _i("DRYDOCK_ADJ_BATCH", 150))
    # Gemini calls: how long one request may take, and how many attempts on a transient error (429/5xx).
    # The SDK's own default backs off for well over a minute on a 503, and a run sits silent meanwhile;
    # after these attempts the adjudicator leaves its pairs for a person instead.
    llm_timeout_s: int = field(default_factory=lambda: _i("DRYDOCK_LLM_TIMEOUT_S", 120))
    llm_attempts: int = field(default_factory=lambda: _i("DRYDOCK_LLM_ATTEMPTS", 3))
    # Optional shared access token. Set, every API call, download and the live stream need it (the browser keeps
    # it in a same-site cookie after asking once). Unset, Drydock is open to whoever can reach it: fine on
    # localhost, not on a network.
    access_token: str = field(default_factory=lambda: os.environ.get("DRYDOCK_ACCESS_TOKEN", "").strip())
    # What the official Exasol MCP server exposes to the agent (agent/loop.py passes these; the UI shows them).
    mcp_row_limit: int = field(default_factory=lambda: _i("DRYDOCK_MCP_ROW_LIMIT", 50))
    mcp_schema_pattern: str = "^(SOURCE_A|SOURCE_B|GOLDEN_V)$"
    event_url: str = field(default_factory=lambda: os.environ.get("DRYDOCK_EVENT_URL", "http://127.0.0.1:8765/internal/events"))
    events_jsonl: str = field(default_factory=lambda: os.environ.get("DRYDOCK_EVENTS_JSONL", str(ROOT / "runs" / "events.jsonl")))
    lock_dir: str = field(default_factory=lambda: os.environ.get("DRYDOCK_LOCK_DIR", str(ROOT / "runs")))


SETTINGS = Settings()

# Schemas
GOLDEN = "GOLDEN"
GOLDEN_V = "GOLDEN_V"
DRYDOCK = "DRYDOCK"
ER_WORK = "ER_WORK"
MATCH_CLASSES = ("EXACT_EMAIL", "PHONE_ADDRESS", "FUZZY_NAME", "NEW_CUSTOMERS", "INTERNAL_DEDUP")
REJECT_CODES = ("WRONG_MATCH", "TOO_BROAD", "INSUFFICIENT_EVIDENCE", "POLICY", "OTHER")
TIGHTENING_CODES = ("WRONG_MATCH", "TOO_BROAD", "INSUFFICIENT_EVIDENCE")
