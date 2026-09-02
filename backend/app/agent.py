"""
The lab analysis agent: Classify -> Route -> Explain -> Verify.

The agent holds no clinical knowledge of its own. Every reference interval,
alias and care pathway it uses arrives through an MCP tool call, and the tool
calls it made are reported back in the response so the reasoning can be audited
rather than taken on trust.

The stage order is the point. Classification finishes - deterministically -
before the model is invoked at all, so the model can only ever describe a
decision, never make one.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .llm import GeminiProvider
from .mcp_client import MCPToolClient, MCPUnavailable
from .verifier import rule_based_explanation, verify_explanation

log = logging.getLogger(__name__)

# Critical first, then Warning, then rows a human must look at, then Normal.
STATUS_ORDER = {"Critical": 0, "Warning": 1, "Unknown": 2, "Normal": 3}
GROUP_ORDER = ["Critical", "Warning", "Unknown", "Normal"]

DISCLAIMER = (
    "Clinical decision support only. Classifications come from a deterministic rule "
    "engine using reference intervals; AI is used solely to word the explanations and "
    "every explanation is machine-checked against those facts before display. "
    "This tool does not diagnose and does not replace clinical judgement or the "
    "issuing laboratory's own reference intervals."
)


class LabAnalysisAgent:
    def __init__(self, mcp: MCPToolClient, llm: GeminiProvider) -> None:
        self.mcp = mcp
        self.llm = llm

    async def analyze(self, labs: list[dict], patient: dict | None,
                      options: dict) -> dict[str, Any]:
        patient = patient or {}
        sex = patient.get("sex") if patient.get("sex") != "unspecified" else None
        age = patient.get("age_years")
        pipeline: list[dict] = []

        classified, kb_version = await self._classify(labs, sex, age, pipeline)
        routed, groups, summary = self._route(classified, pipeline)
        ai_report = await self._explain(routed, options.get("explain", True), pipeline)

        results = [self._present(r, options.get("include_evidence", True)) for r in routed]
        by_status = {s: [] for s in GROUP_ORDER}
        for r in results:
            by_status[r["status"]].append(r)

        return {
            "request_id": uuid.uuid4().hex[:12],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "patient_reference": patient.get("reference"),
            "summary": summary,
            "groups": [{"status": s, "count": len(by_status[s]), "results": by_status[s]}
                       for s in GROUP_ORDER if by_status[s]],
            "results": results,
            "ai": ai_report,
            "pipeline": pipeline,
            "knowledge_base_version": kb_version,
            "disclaimer": DISCLAIMER,
        }

    # -- stage 1: classify --------------------------------------------------

    async def _classify(self, labs: list[dict], sex: str | None, age: float | None,
                        pipeline: list[dict]) -> tuple[list[dict], str]:
        """One MCP call per result. No model involvement whatsoever."""
        started = time.monotonic()
        tool_calls: list[str] = []
        out: list[dict] = []
        kb_version = ""
        lookups = 0

        for i, lab in enumerate(labs):
            args = {
                "test_name": lab["test_name"],
                "value": "" if lab.get("value") is None else str(lab["value"]),
                "unit": lab.get("unit"),
                "sex": sex,
                "age_years": age,
                "reference_low": lab.get("reference_low"),
                "reference_high": lab.get("reference_high"),
                "reference_range_text": lab.get("reference_range"),
            }
            try:
                res = await self.mcp.call("classify_lab_result", args)
                tool_calls.append("classify_lab_result")
            except MCPUnavailable as exc:
                log.warning("classify failed for %r: %s", lab["test_name"], exc)
                res = _unavailable_result(lab, str(exc))

            # The assignment's optional tool, used where it actually earns its
            # place: a name the catalogue could not resolve. The lookup cannot
            # invent an interval, but it can return near-miss candidates that
            # turn a dead end into an actionable "did you mean".
            if res.get("status") == "Unknown" and res.get("concept_key") is None:
                try:
                    hint = await self.mcp.call("reference_range_lookup", {
                        "test_name": lab["test_name"], "sex": sex,
                        "age_years": age, "unit": lab.get("unit"),
                    })
                    tool_calls.append("reference_range_lookup")
                    lookups += 1
                    res.setdefault("evidence", {})["fallback_lookup"] = hint
                except MCPUnavailable as exc:
                    log.debug("fallback lookup failed: %s", exc)

            res["id"] = i
            res["collected_on"] = lab.get("collected_on")
            res["reference_text"] = _interval_text(res, lab)
            kb_version = kb_version or (res.get("evidence", {}) or {}).get("kb_version", "")
            out.append(res)

        detail = f"Classified {len(out)} result(s) through the MCP knowledge server"
        if lookups:
            detail += f"; {lookups} unresolved name(s) sent to reference_range_lookup"
        pipeline.append({
            "step": "classify",
            "detail": detail + ". Deterministic rules only - no AI involved.",
            "duration_ms": _ms(started),
            "tool_calls": sorted(set(tool_calls)),
        })
        return out, kb_version

    # -- stage 2: route -----------------------------------------------------

    def _route(self, results: list[dict],
               pipeline: list[dict]) -> tuple[list[dict], dict, dict]:
        """Order by severity, then by how far outside the interval each result sits."""
        started = time.monotonic()
        ordered = sorted(
            results,
            key=lambda r: (STATUS_ORDER.get(r["status"], 9),
                           -(r.get("deviation_index") or 0.0),
                           r.get("display_name", "")),
        )
        counts = {s: sum(1 for r in ordered if r["status"] == s) for s in GROUP_ORDER}
        needs_review = sum(1 for r in ordered if "needs_human_review" in (r.get("flags") or []))
        highest = next((s for s in GROUP_ORDER if counts[s]), "Normal")

        summary = {
            "total": len(ordered),
            "critical": counts["Critical"],
            "warning": counts["Warning"],
            "normal": counts["Normal"],
            "unknown": counts["Unknown"],
            "needs_review": needs_review,
            "highest_severity": highest,
            "headline": _headline(counts, len(ordered)),
        }
        pipeline.append({
            "step": "route",
            "detail": (f"Ordered by severity then deviation: {counts['Critical']} critical, "
                       f"{counts['Warning']} warning, {counts['Unknown']} unclassifiable, "
                       f"{counts['Normal']} normal."),
            "duration_ms": _ms(started),
            "tool_calls": [],
        })
        return ordered, counts, summary

    # -- stage 3+4: explain, then verify -----------------------------------

    async def _explain(self, results: list[dict], want_ai: bool,
                       pipeline: list[dict]) -> dict:
        """One batched model call for the whole panel, then check every answer."""
        started = time.monotonic()

        for r in results:
            r["_fallback"] = rule_based_explanation(r)

        if not want_ai:
            for r in results:
                r["_explanation"] = {**r["_fallback"], "source": "rule_based",
                                     "source_note": "AI explanations were switched off "
                                                    "for this request."}
                r["_verification"] = None
            pipeline.append({"step": "explain", "duration_ms": _ms(started), "tool_calls": [],
                             "detail": "AI explanations disabled; rule-based text used."})
            return {"provider": "google-gemini", "model": self.llm.model, "used": False,
                    "detail": "disabled for this request", "latency_ms": 0,
                    "explanations_from_cache": 0, "verified_ok": 0,
                    "replaced_after_failed_check": 0}

        facts = [_fact_bundle(r) for r in results]
        outcome = self.llm.explain_batch(facts)
        fact_by_id = {f["id"]: f for f in facts}

        verified_ok = replaced = 0
        for r in results:
            raw = outcome.explanations.get(r["id"])
            if not raw:
                r["_explanation"] = {
                    **r["_fallback"], "source": "rule_based",
                    "source_note": f"No AI explanation available ({outcome.detail}). "
                                   "Rule-based text shown instead.",
                }
                r["_verification"] = None
                continue

            report = verify_explanation(raw, fact_by_id[r["id"]])
            if report["passed"]:
                verified_ok += 1
                r["_explanation"] = {
                    "what_it_measures": raw["what_it_measures"],
                    "why_flagged": raw["why_flagged"],
                    "clinical_significance": raw["clinical_significance"],
                    "patient_friendly": raw["patient_friendly"],
                    "ai_suggested_next_steps": raw.get("suggested_next_steps", []),
                    "source": "ai",
                    "source_note": ("Generated by the AI provider and passed all "
                                    "grounding checks."),
                }
            else:
                replaced += 1
                r["_explanation"] = {
                    **r["_fallback"], "source": "rule_based",
                    "source_note": ("The AI explanation failed a grounding check "
                                    f"({report['summary']}), so it was discarded and "
                                    "rule-based text is shown instead."),
                }
            r["_verification"] = report

        pipeline.append({
            "step": "explain",
            "detail": (f"{outcome.detail}. Verified {verified_ok}, "
                       f"replaced {replaced} after a failed grounding check."),
            "duration_ms": _ms(started),
            "tool_calls": [],
        })
        return {
            "provider": outcome.provider, "model": outcome.model,
            "used": outcome.ok and bool(outcome.explanations),
            "detail": outcome.detail, "latency_ms": outcome.latency_ms,
            "explanations_from_cache": outcome.cached,
            "verified_ok": verified_ok, "replaced_after_failed_check": replaced,
        }

    # -- presentation ------------------------------------------------------

    @staticmethod
    def _present(r: dict, include_evidence: bool) -> dict:
        pathway = r.get("care_pathway") or {}
        return {
            "id": r["id"],
            "test_name": r.get("test_name", ""),
            "display_name": r.get("display_name") or r.get("test_name", ""),
            "category": r.get("category") or "Unclassified",
            "loinc": r.get("loinc"),
            "value": r.get("value"),
            "value_display": r.get("value_display") or "",
            "unit": r.get("unit") or "",
            "canonical_value": r.get("canonical_value"),
            "canonical_unit": r.get("canonical_unit") or "",
            "unit_converted": bool(r.get("unit_converted")),
            "status": r.get("status", "Unknown"),
            "direction": r.get("direction") or "",
            "deviation_index": r.get("deviation_index"),
            "reference_low": r.get("reference_low"),
            "reference_high": r.get("reference_high"),
            "reference_text": r.get("reference_text") or "",
            "flags": r.get("flags") or [],
            "error": r.get("error"),
            "explanation": r["_explanation"],
            "verification": r.get("_verification"),
            "next_steps": {
                "urgency": pathway.get("urgency", "review"),
                "sla": pathway.get("sla", "Manual review"),
                "specialty": pathway.get("specialty", "Laboratory"),
                "actions": pathway.get("actions", []),
                "source": pathway.get("source", ""),
            },
            "evidence": r.get("evidence") if include_evidence else None,
        }


def _fact_bundle(r: dict) -> dict:
    """Exactly what the model is allowed to see, and what it is checked against.

    No patient reference, no date, no free-text from the source file beyond the
    test name itself.
    """
    return {
        "id": r["id"],
        "concept_key": r.get("concept_key"),
        "display_name": r.get("display_name") or r.get("test_name", ""),
        "category": r.get("category"),
        "value": r.get("canonical_value") if r.get("canonical_value") is not None
                 else r.get("value"),
        "value_display": r.get("value_display") or "",
        "canonical_unit": r.get("canonical_unit") or r.get("unit") or "",
        "unit": r.get("unit") or "",
        # The result as reported, and - only when a unit conversion was applied -
        # the converted figure that was actually compared to the interval. Sending
        # "7.2" next to "mg/dL" would be a fact the model could not help but
        # misdescribe, so the two are kept explicitly separate.
        "result_text": _result_text(r),
        "converted_text": _converted_text(r),
        "reference_low": r.get("reference_low"),
        "reference_high": r.get("reference_high"),
        "reference_text": r.get("reference_text") or "",
        "status": r.get("status"),
        "direction": r.get("direction"),
        "deviation_index": r.get("deviation_index"),
        "measures": r.get("measures") or "",
        "rationale": ((r.get("evidence") or {}).get("rule") or {}).get("rationale", ""),
    }


def _result_text(r: dict) -> str:
    """The result exactly as the laboratory reported it."""
    return f"{r.get('value_display') or ''} {r.get('unit') or ''}".strip()


def _converted_text(r: dict) -> str:
    """The converted figure that was compared against the interval, if any."""
    if not r.get("unit_converted") or r.get("canonical_value") is None:
        return ""
    return f"{r['canonical_value']:g} {r.get('canonical_unit') or ''}".strip()


def _interval_text(res: dict, lab: dict) -> str:
    low, high = res.get("reference_low"), res.get("reference_high")
    unit = res.get("canonical_unit") or res.get("unit") or ""
    if low is not None and high is not None:
        return f"{low:g}-{high:g} {unit}".strip()
    if high is not None:
        return f"<= {high:g} {unit}".strip()
    if low is not None:
        return f">= {low:g} {unit}".strip()
    if res.get("status") != "Unknown":
        return "qualitative scale (see evidence)"
    return lab.get("reference_range") or "not available"


def _headline(counts: dict, total: int) -> str:
    if counts["Critical"]:
        return (f"{counts['Critical']} critical result"
                f"{'s' if counts['Critical'] > 1 else ''} need immediate review.")
    if counts["Warning"]:
        return (f"{counts['Warning']} result{'s' if counts['Warning'] > 1 else ''} "
                "outside the reference interval need clinician review.")
    if counts["Unknown"]:
        return (f"{counts['Unknown']} result{'s' if counts['Unknown'] > 1 else ''} "
                "could not be classified and need manual review.")
    return f"All {total} results are within their reference intervals."


def _unavailable_result(lab: dict, reason: str) -> dict:
    """Shape an honest failure when the knowledge server cannot be reached."""
    return {
        "test_name": lab["test_name"], "concept_key": None,
        "display_name": lab["test_name"], "category": "Unclassified", "loinc": None,
        "value": None, "value_display": str(lab.get("value", "")),
        "unit": lab.get("unit") or "", "canonical_value": None, "canonical_unit": "",
        "status": "Unknown", "direction": "Unknown", "deviation_index": None,
        "reference_low": None, "reference_high": None,
        "flags": ["needs_human_review", "knowledge_server_unavailable"],
        "measures": "", "direction_meaning": "",
        "care_pathway": {"urgency": "review", "sla": "Manual review",
                         "specialty": "Laboratory",
                         "actions": ["The knowledge server was unreachable; "
                                     "classify this result manually."],
                         "source": "fallback"},
        "evidence": {"steps": [{"step": "classify", "outcome": "mcp_unavailable",
                                "note": reason}]},
        "error": f"Could not classify: {reason}",
    }


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
