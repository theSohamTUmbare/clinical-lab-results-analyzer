"""
Gemini provider for the explanation stage.

Three things matter here and they are all cost/quality decisions rather than
plumbing:

1. **The model never sees a patient identifier.** Only the grounded fact bundle
   built by the agent is sent - test name, value, unit, interval, status.
2. **One call per panel, not one per result.** Every result still gets its own
   model-authored explanation, but they are produced in a single batched
   request. On a free tier that is the difference between a demo that works and
   one that trips the rate limit on the first CSV.
3. **A miss is never fatal.** Quota exhaustion, a timeout or malformed JSON all
   degrade to the deterministic explanation the engine can always produce, and
   the response says so honestly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.0-flash"
REQUEST_TIMEOUT_S = 45
MAX_RETRIES = 2

SYSTEM_INSTRUCTION = """\
You are a clinical laboratory communication assistant embedded in a decision-support tool used by healthcare staff.

Your ONLY job is to put already-decided findings into clear clinical language.

ABSOLUTE RULES - these are enforced by an automated checker after you respond, and \
any output that breaks them is discarded:

1. The severity status (Normal / Warning / Critical) has ALREADY been decided by a \
   validated deterministic rule engine. Never dispute it, never re-classify, never \
   hedge about it. Write as though it is settled fact, because it is.
2. Use ONLY the numbers given to you in the fact bundle. Do not introduce any other \
   numeric value - no thresholds, no percentages, no population statistics, no \
   alternative reference ranges you may recall. If you want to cite a boundary, cite \
   the reference interval you were given.
3. Do not diagnose. Say what the result is consistent with, not what the patient has. \
   Write "is consistent with", "can indicate", "warrants assessment for" - never \
   "you have" or "the patient has".
4. Never name a drug, a dose, or a treatment regimen.
5. Never discourage seeking care, and never promise reassurance about a Warning or \
   Critical result.
6. Explain in clinically relevant language, but keep the patient_friendly field free \
   of jargon and readable by a non-clinician.
7. Be concise. One to two sentences per field.

Respond with JSON only, matching the schema given in the request."""

RESPONSE_SCHEMA_DOC = """\
{
  "explanations": [
    {
      "id": <integer, echo the id from the fact bundle>,
      "what_it_measures": "<one sentence: what this test actually measures in the body>",
      "why_flagged": "<one to two sentences: why THIS value produced THIS status, referring to the reference interval you were given>",
      "clinical_significance": "<one to two sentences: what an abnormal result of this kind can indicate clinically. For Normal results, state plainly that no action follows.>",
      "patient_friendly": "<one to two sentences a non-clinician can understand, no jargon>",
      "suggested_next_steps": ["<0-3 short, concrete, non-pharmacological next steps>"]
    }
  ]
}"""


@dataclass
class LLMOutcome:
    """Result of one explanation batch, including why it failed if it did."""
    explanations: dict[int, dict]
    provider: str
    model: str
    ok: bool
    detail: str
    latency_ms: int = 0
    cached: int = 0
    prompt_chars: int = 0


class GeminiProvider:
    """Thin wrapper over google-genai with caching, retries and graceful failure."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.model = model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip()
        self._client: Any = None
        self._cache: dict[str, dict] = {}
        self._init_error: str | None = None

        if not self.api_key:
            self._init_error = ("GEMINI_API_KEY is not set. The app runs in "
                                "rule-based-only mode; explanations come from the "
                                "deterministic templates.")
            return
        try:
            from google import genai  # imported lazily so the app boots without the SDK
            self._client = genai.Client(api_key=self.api_key)
        except Exception as exc:  # noqa: BLE001 - surfaced to /health, never raised
            self._init_error = f"Could not initialise the Gemini client: {exc}"
            log.warning(self._init_error)

    @property
    def available(self) -> bool:
        return self._client is not None

    def status(self) -> dict:
        return {
            "provider": "google-gemini",
            "model": self.model,
            "available": self.available,
            "detail": self._init_error or "ready",
            "cache_entries": len(self._cache),
        }

    # -- caching -----------------------------------------------------------

    @staticmethod
    def _cache_key(fact: dict) -> str:
        """Cache on the grounded facts, not on the patient's row.

        Two patients with the same test, status and rounded value get the same
        explanation, so a 200-row CSV of routine panels collapses to a handful
        of distinct explanations.
        """
        v = fact.get("value")
        bucket = round(float(v), 1) if isinstance(v, (int, float)) else fact.get("value_display")
        material = json.dumps([
            fact.get("concept_key"), fact.get("status"), fact.get("direction"),
            bucket, fact.get("reference_low"), fact.get("reference_high"),
            fact.get("canonical_unit"),
        ], sort_keys=True, default=str)
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    # -- explanation -------------------------------------------------------

    def explain_batch(self, facts: list[dict]) -> LLMOutcome:
        """Explain a whole panel in one request, serving what it can from cache."""
        if not facts:
            return LLMOutcome({}, "google-gemini", self.model, True, "nothing to explain")

        out: dict[int, dict] = {}
        pending: list[dict] = []
        for f in facts:
            hit = self._cache.get(self._cache_key(f))
            if hit is not None:
                out[f["id"]] = {**hit, "_cached": True}
            else:
                pending.append(f)

        if not self.available:
            return LLMOutcome(out, "google-gemini", self.model, False,
                              self._init_error or "provider unavailable",
                              cached=len(out))
        if not pending:
            return LLMOutcome(out, "google-gemini", self.model, True,
                              "served entirely from cache", cached=len(out))

        prompt = self._build_prompt(pending)
        started = time.monotonic()
        text, err = self._generate(prompt)
        latency = int((time.monotonic() - started) * 1000)

        if err:
            return LLMOutcome(out, "google-gemini", self.model, False, err,
                              latency_ms=latency, cached=len(out),
                              prompt_chars=len(prompt))

        parsed, perr = _parse_json(text or "")
        if perr:
            return LLMOutcome(out, "google-gemini", self.model, False,
                              f"model returned unparseable JSON: {perr}",
                              latency_ms=latency, cached=len(out),
                              prompt_chars=len(prompt))

        by_id = {f["id"]: f for f in pending}
        received = 0
        for item in parsed.get("explanations", []):
            try:
                idx = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if idx not in by_id:
                continue
            clean = {
                "what_it_measures": str(item.get("what_it_measures", "")).strip(),
                "why_flagged": str(item.get("why_flagged", "")).strip(),
                "clinical_significance": str(item.get("clinical_significance", "")).strip(),
                "patient_friendly": str(item.get("patient_friendly", "")).strip(),
                "suggested_next_steps": [
                    str(s).strip() for s in (item.get("suggested_next_steps") or [])
                    if str(s).strip()
                ][:3],
            }
            out[idx] = clean
            self._cache[self._cache_key(by_id[idx])] = clean
            received += 1

        missing = [i for i in by_id if i not in out]
        detail = f"explained {received}/{len(pending)} results in one request"
        if missing:
            detail += f"; {len(missing)} missing from the response"
        return LLMOutcome(out, "google-gemini", self.model, True, detail,
                          latency_ms=latency, cached=len(facts) - len(pending),
                          prompt_chars=len(prompt))

    def _generate(self, prompt: str) -> tuple[str | None, str | None]:
        """Call Gemini, retrying transient failures with backoff."""
        from google.genai import types  # local import keeps module import cheap

        last = "unknown error"
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        response_mime_type="application/json",
                        # Low but non-zero: explanations should be stable across
                        # runs of the same panel without being robotic.
                        temperature=0.2,
                        max_output_tokens=8192,
                        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000),
                    ),
                )
                return (resp.text or ""), None
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                last = f"{type(exc).__name__}: {exc}"
                transient = any(t in last.lower() for t in
                                ("429", "resource_exhausted", "503", "unavailable",
                                 "deadline", "timeout", "500", "internal"))
                if attempt < MAX_RETRIES and transient:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        return None, last

    @staticmethod
    def _build_prompt(facts: list[dict]) -> str:
        """Assemble the grounded fact bundle. Nothing else reaches the model."""
        lines = [
            "Explain the following laboratory results. Each has ALREADY been "
            "classified by the rule engine; the status is final.",
            "",
            "FACT BUNDLE (these are the only facts and the only numbers you may use):",
        ]
        for f in facts:
            interval = f.get("reference_text") or "not applicable (qualitative test)"

            # Where a unit conversion was applied, the reported figure and the
            # figure actually compared against the interval are different numbers
            # in different units. They must be presented as two separate facts.
            # Pairing the reported number with the canonical unit produces a
            # value that contradicts the status - "13 g/dL" for a result of
            # 13 mmol/L, sitting inside a 12-15.5 g/dL interval while the status
            # says Critical - and the model then writes reassuring wording that
            # the grounding checks (correctly) reject.
            converted = f.get("converted_text")
            result_lines = f"  result_as_reported: {f.get('result_text') or 'n/a'}\n"
            if converted:
                result_lines += (
                    f"  value_compared_against_the_interval: {converted}\n"
                    f"  note: the reported result was converted to {f.get('canonical_unit')} "
                    "before comparison; both figures describe the same measurement\n"
                )

            lines.append(
                f"\n- id: {f['id']}\n"
                f"  test: {f['display_name']}\n"
                f"  category: {f.get('category', 'n/a')}\n"
                + result_lines +
                f"  reference_interval: {interval}\n"
                f"  status: {f['status']}\n"
                f"  direction: {f['direction']}\n"
                f"  what_this_test_measures: {f.get('measures', 'n/a')}\n"
                f"  rule_engine_rationale: {f.get('rationale', 'n/a')}"
            )
        lines += [
            "",
            f"Return exactly {len(facts)} explanation objects, one per id, in this shape:",
            RESPONSE_SCHEMA_DOC,
        ]
        return "\n".join(lines)


def _parse_json(text: str) -> tuple[dict, str | None]:
    """Parse model JSON, tolerating code fences and leading prose."""
    s = (text or "").strip()
    if not s:
        return {}, "empty response"
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return {}, "no JSON object found"
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            return {}, str(exc)
    return (data, None) if isinstance(data, dict) else ({}, "top level was not an object")
