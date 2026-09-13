"""Gemini 3.5 Flash configuration, pinned offline. The live proof that the model supports each
feature is scripts/verify.py check G1; these tests stop the request shape drifting."""

from __future__ import annotations

import inspect

import pytest

from drydock import llm
from drydock.config import Settings

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}


def test_default_model_is_gemini_3_5_flash_for_both_roles(monkeypatch):
    for v in ("DRYDOCK_PLANNER_MODEL", "DRYDOCK_ADJ_MODEL"):
        monkeypatch.delenv(v, raising=False)
    s = Settings()
    assert s.planner_model == "gemini-3.5-flash" and s.adjudicator_model == "gemini-3.5-flash"
    assert s.adjudicator_rung == "gemini_fallback" or s.adjudicator_rung.startswith("exasol_ai")


def test_json_config_has_schema_system_and_thinking_but_no_temperature():
    c = llm.json_config("sys", SCHEMA, "MEDIUM")
    assert c.response_mime_type == "application/json" and c.response_json_schema == SCHEMA
    assert c.system_instruction == "sys"
    assert c.thinking_config.thinking_level.value == "MEDIUM"
    assert c.temperature is None          # Gemini 3: keep the default 1.0


def test_tools_config_disables_automatic_calling_and_sets_thinking():
    from agent.loop import LOCAL_TOOLS, declarations
    c = llm.tools_config("sys", declarations(LOCAL_TOOLS), "HIGH")
    assert c.automatic_function_calling.disable is True
    assert c.thinking_config.thinking_level.value == "HIGH"
    assert c.temperature is None
    names = [f.name for f in c.tools[0].function_declarations]
    assert names == ["profile_finding", "state_hypothesis", "self_check"]
    assert c.tools[0].function_declarations[1].parameters_json_schema["required"] == ["match_class", "expected_count", "rationale"]


@pytest.mark.parametrize("bad", ["ultra", "", "0"])
def test_invalid_thinking_level_rejected(bad):
    with pytest.raises(ValueError):
        llm.thinking(bad)


def test_agent_loop_returns_model_turn_unchanged_for_thought_signatures():
    from agent import loop
    src = inspect.getsource(loop._run)
    assert "contents.append(model_content)" in src
    assert "temperature" not in src.replace("No temperature override", "")


def test_no_temperature_anywhere_in_llm_calls():
    from drydock import adjudicate
    for mod in (llm, adjudicate):
        assert "temperature=" not in inspect.getsource(mod)


def test_missing_key_refuses(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(llm.LlmUnavailable):
        llm.api_key()


def test_shared_client_is_held_not_a_temporary(monkeypatch):
    """A temporary genai.Client is closed by its __del__ mid-call, so client() must return one held
    instance. No network call is made here."""
    import gc
    monkeypatch.setenv("GEMINI_API_KEY", "offline-test-key")
    monkeypatch.setattr(llm, "_client", None)
    a = llm.client()
    gc.collect()
    assert llm.client() is a
    assert not a._api_client._httpx_client.is_closed


def test_quota_and_outage_are_degradable_but_real_faults_are_not():
    """A quota refusal or an outage escalates pairs to a reviewer instead of aborting the run; anything
    else (bad key, bad request) must still surface."""
    from drydock.adjudicate import _is_quota

    class E(Exception):
        def __init__(self, code, msg=""):
            super().__init__(msg)
            self.code = code
    assert _is_quota(E(429, "RESOURCE_EXHAUSTED quota exceeded"))
    assert _is_quota(E(503, "UNAVAILABLE"))
    assert not _is_quota(E(400, "INVALID_ARGUMENT"))
    assert not _is_quota(E(403, "API key not valid"))
    assert not _is_quota(ValueError("schema mismatch"))


def test_adjudication_batch_is_large_enough_to_fit_a_full_run_in_few_requests(monkeypatch):
    from drydock.config import Settings
    monkeypatch.delenv("DRYDOCK_ADJ_BATCH", raising=False)
    assert Settings().adj_batch >= 100
