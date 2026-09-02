"""
Tests for the explanation stage, with the Gemini client stubbed.

These pin the behaviour that has to hold whether or not an API key is present:
batching, caching, tolerant JSON parsing, and - most importantly - that a
hallucinated explanation never reaches the response.
"""

from __future__ import annotations

import json

import pytest

from app.agent import LabAnalysisAgent
from app.llm import GeminiProvider, _parse_json


class FakeGemini(GeminiProvider):
    """A provider whose model returns a scripted payload instead of calling out."""

    def __init__(self, payload, *, fail: str | None = None):
        self.api_key = "test-key"
        self.model = "fake-model"
        self._client = object()  # non-None so `available` is True
        self._cache = {}
        self._init_error = None
        self._payload = payload
        self._fail = fail
        self.calls = 0

    def _generate(self, prompt):  # type: ignore[override]
        self.calls += 1
        self.last_prompt = prompt
        if self._fail:
            return None, self._fail
        payload = self._payload(prompt) if callable(self._payload) else self._payload
        return payload, None


def explanation_for(idx: int, **overrides) -> dict:
    base = {
        "id": idx,
        "what_it_measures": "Potassium is the main electrolyte inside cells.",
        "why_flagged": "At 6.9 mmol/L this sits above the 3.5-5.1 mmol/L interval.",
        "clinical_significance": "A raised potassium can affect cardiac conduction.",
        "patient_friendly": "Your potassium is higher than the usual range.",
        "suggested_next_steps": ["Repeat the sample"],
    }
    return {**base, **overrides}


FACTS = [{
    "id": 0,
    "concept_key": "potassium",
    "display_name": "Potassium",
    "category": "Chemistry",
    "value": 6.9,
    "value_display": "6.9",
    "canonical_unit": "mmol/L",
    "unit": "mmol/L",
    "result_text": "6.9 mmol/L",
    "converted_text": "",
    "reference_low": 3.5,
    "reference_high": 5.1,
    "reference_text": "3.5-5.1 mmol/L",
    "status": "Critical",
    "direction": "High",
    "deviation_index": 1.125,
    "measures": "The main electrolyte inside cells.",
    "rationale": "6.9 mmol/L breaches the action limit of 6.5 mmol/L.",
}]


# -- provider mechanics --------------------------------------------------------

def test_a_whole_panel_costs_one_request():
    facts = [{**FACTS[0], "id": i, "display_name": f"Test {i}", "concept_key": f"t{i}"}
             for i in range(12)]
    provider = FakeGemini(json.dumps({"explanations": [explanation_for(i) for i in range(12)]}))
    outcome = provider.explain_batch(facts)
    assert provider.calls == 1
    assert len(outcome.explanations) == 12


def test_repeated_facts_are_served_from_cache_without_a_second_request():
    provider = FakeGemini(json.dumps({"explanations": [explanation_for(0)]}))
    provider.explain_batch(FACTS)
    outcome = provider.explain_batch(FACTS)
    assert provider.calls == 1
    assert outcome.cached == 1
    assert outcome.ok is True


def test_a_provider_failure_is_reported_not_raised():
    provider = FakeGemini(None, fail="429 RESOURCE_EXHAUSTED")
    outcome = provider.explain_batch(FACTS)
    assert outcome.ok is False
    assert "429" in outcome.detail
    assert outcome.explanations == {}


def test_missing_api_key_leaves_the_provider_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiProvider(api_key="")
    assert provider.available is False
    assert "GEMINI_API_KEY" in provider.status()["detail"]


@pytest.mark.parametrize("raw", [
    '{"explanations": []}',
    '```json\n{"explanations": []}\n```',
    'Here you go:\n{"explanations": []}',
])
def test_json_parsing_tolerates_fences_and_preamble(raw):
    data, err = _parse_json(raw)
    assert err is None and data == {"explanations": []}


def test_unparseable_json_is_reported():
    _, err = _parse_json("not json at all")
    assert err


def test_the_prompt_carries_the_facts_and_no_patient_data():
    provider = FakeGemini(json.dumps({"explanations": [explanation_for(0)]}))
    provider.explain_batch(FACTS)
    prompt = provider.last_prompt
    assert "Potassium" in prompt and "3.5-5.1 mmol/L" in prompt and "Critical" in prompt
    assert "reference" not in prompt.lower().split("reference_interval")[0].replace(
        "reference_interval", "")


# -- the agent's explain stage -------------------------------------------------

class StubMCP:
    """Minimal MCP stand-in that classifies via the engine directly."""

    def __init__(self):
        from mcp_server.engine import KnowledgeBase
        self.kb = KnowledgeBase()
        self.connected = True

    async def call(self, tool, args):
        from mcp_server.engine import classify
        assert tool == "classify_lab_result"
        return classify(self.kb, args["test_name"], args["value"], args.get("unit"),
                        args.get("sex"), args.get("age_years"))


async def run_agent(provider):
    agent = LabAnalysisAgent(StubMCP(), provider)
    return await agent.analyze(
        [{"test_name": "Potassium", "value": "6.9", "unit": "mmol/L"}],
        None, {"explain": True, "include_evidence": True})


async def test_a_verified_explanation_is_used():
    provider = FakeGemini(json.dumps({"explanations": [explanation_for(0)]}))
    report = await run_agent(provider)
    result = report["results"][0]
    assert result["explanation"]["source"] == "ai"
    assert result["verification"]["passed"] is True
    assert report["ai"]["verified_ok"] == 1


async def test_a_hallucinated_number_never_reaches_the_response():
    """The whole point of the verifier, end to end."""
    provider = FakeGemini(json.dumps({"explanations": [explanation_for(
        0, clinical_significance="Anything above 8.4 mmol/L causes cardiac arrest.")]}))
    report = await run_agent(provider)
    result = report["results"][0]

    assert result["explanation"]["source"] == "rule_based"
    assert result["verification"]["passed"] is False
    assert report["ai"]["replaced_after_failed_check"] == 1

    # The invented figure must not survive in any explanation field the user
    # reads as clinical content. It is deliberately still named in the audit
    # trail - source_note and the check detail - because a reviewer needs to see
    # what was rejected and why.
    content = " ".join(result["explanation"][f] for f in (
        "what_it_measures", "why_flagged", "clinical_significance", "patient_friendly"))
    assert "8.4" not in content
    assert "8.4" in result["explanation"]["source_note"]


async def test_an_unsafe_explanation_is_replaced():
    provider = FakeGemini(json.dumps({"explanations": [explanation_for(
        0, patient_friendly="You have kidney failure.")]}))
    report = await run_agent(provider)
    assert report["results"][0]["explanation"]["source"] == "rule_based"
    assert report["ai"]["replaced_after_failed_check"] == 1


async def test_provider_failure_degrades_to_rule_based_text():
    report = await run_agent(FakeGemini(None, fail="503 UNAVAILABLE"))
    result = report["results"][0]
    assert result["explanation"]["source"] == "rule_based"
    assert result["explanation"]["why_flagged"]           # still a real explanation
    assert "503" in result["explanation"]["source_note"]
    assert report["ai"]["used"] is False


async def test_classification_is_identical_with_and_without_ai():
    """AI must not be able to move a severity, by construction."""
    with_ai = await run_agent(FakeGemini(json.dumps({"explanations": [explanation_for(0)]})))
    without = await run_agent(FakeGemini(None, fail="no key"))
    a, b = with_ai["results"][0], without["results"][0]
    assert a["status"] == b["status"] == "Critical"
    assert a["deviation_index"] == b["deviation_index"]
    assert a["evidence"]["rule"]["id"] == b["evidence"]["rule"]["id"]


async def test_a_missing_explanation_falls_back_for_that_row_only():
    provider = FakeGemini(json.dumps({"explanations": []}))
    report = await run_agent(provider)
    assert report["results"][0]["explanation"]["source"] == "rule_based"


# -- Windows event-loop guard --------------------------------------------------

def test_selector_loop_failure_gives_an_actionable_message(monkeypatch):
    """A bare NotImplementedError must never reach the user.

    Uvicorn switches Windows to a SelectorEventLoop whenever --reload is on, and
    that loop cannot spawn the MCP server subprocess. The resulting error has an
    empty message, so the client substitutes one that says how to fix it.
    """
    from app.mcp_client import MCPToolClient

    monkeypatch.setattr("app.mcp_client.sys.platform", "win32")
    hint = MCPToolClient._loop_hint()
    assert "does not support subprocesses" in hint
    assert "run.py" in hint
    # The CLI rejects `--loop none`, so the hint must not recommend it there.
    assert "drop --reload" in hint
