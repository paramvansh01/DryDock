"""Gemini client (google-genai).

Used by agent/loop.py (the planner, via function calling) and drydock/adjudicate.py
(structured JSON output). Credentials: GEMINI_API_KEY (or GOOGLE_API_KEY). Models:
DRYDOCK_PLANNER_MODEL / DRYDOCK_ADJ_MODEL, default gemini-3.5-flash.

Gemini 3 notes: temperature is left at its default, as Google recommends; thinking is
set with ThinkingConfig(thinking_level=...); in multi-turn function calling the model's
Content is sent back unchanged so its thought signatures are preserved.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

BLOCKED_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


class LlmUnavailable(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise LlmUnavailable("GEMINI_API_KEY is not set (put it in .env). The planner and the Gemini adjudicator "
                             "cannot run without it; nothing falls back silently.")
    return key


_client = None
_client_lock = threading.Lock()


def client():
    """One process-wide client. It must be held: genai.Client.__del__ closes its httpx client, so a
    temporary client can be collected mid-call ("Cannot send a request, as the client has been closed")."""
    global _client
    with _client_lock:
        if _client is None:
            _client = new_client()
        return _client


def new_client():
    """A fresh client the CALLER must keep referenced for as long as it is used. agent/loop.py takes one
    per run: each run has its own event loop, and client.aio should not be shared across loops."""
    from google import genai
    from google.genai import types

    from .config import SETTINGS
    retry = types.HttpRetryOptions(attempts=max(1, SETTINGS.llm_attempts), initial_delay=1.0, max_delay=8.0)
    return genai.Client(api_key=api_key(),
                        http_options=types.HttpOptions(timeout=SETTINGS.llm_timeout_s * 1000, retry_options=retry))


def finish_reason(resp) -> str | None:
    cands = getattr(resp, "candidates", None) or []
    if not cands:
        fb = getattr(resp, "prompt_feedback", None)
        return f"PROMPT_BLOCKED:{getattr(fb, 'block_reason', None)}" if fb else "NO_CANDIDATES"
    fr = getattr(cands[0], "finish_reason", None)
    return getattr(fr, "value", None) or (str(fr) if fr else None)


def was_blocked(resp) -> bool:
    fr = finish_reason(resp) or ""
    return fr.startswith("PROMPT_BLOCKED") or fr == "NO_CANDIDATES" or fr in BLOCKED_REASONS


def thinking(level: str):
    from google.genai import types
    lvl = level.upper()
    if lvl not in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
        raise ValueError(f"thinking level must be MINIMAL|LOW|MEDIUM|HIGH, got {level!r}")
    return types.ThinkingConfig(thinking_level=lvl)


def json_config(system: str, schema: dict[str, Any], level: str):
    """Structured output (JSON schema) + system instruction + thinking. No temperature (Gemini 3 guidance)."""
    from google.genai import types
    return types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                       response_json_schema=schema, thinking_config=thinking(level))


def tools_config(system: str, tools: list, level: str):
    """Function calling with the SDK's automatic calling DISABLED (the caller executes and records every call)."""
    from google.genai import types
    return types.GenerateContentConfig(system_instruction=system, tools=tools, thinking_config=thinking(level),
                                       automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def generate_json(model: str, system: str, prompt: str, schema: dict[str, Any],
                  level: str = "MEDIUM") -> tuple[dict | None, str | None]:
    """One structured-output call. Returns (parsed JSON or None if blocked/empty, finish_reason)."""
    resp = client().models.generate_content(model=model, contents=prompt, config=json_config(system, schema, level))
    fr = finish_reason(resp)
    if was_blocked(resp) or not resp.text:
        return None, fr
    try:
        return json.loads(resp.text), fr
    except json.JSONDecodeError:
        # e.g. finish_reason MAX_TOKENS truncated the object. The caller treats None as "no decision".
        return None, f"{fr}:UNPARSEABLE_JSON"


def normalise_args(v: Any) -> Any:
    """Gemini returns JSON numbers as floats (3.0); tools typed int expect 3."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: normalise_args(x) for k, x in v.items()}
    if isinstance(v, list):
        return [normalise_args(x) for x in v]
    return v
