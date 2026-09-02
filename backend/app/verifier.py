"""
Grounding verification for model-authored explanations.

The engine decides severity; the model only writes prose. But prose can still be
wrong - it can invent a threshold, soften a critical result, or drift into
diagnosis. So every explanation is checked against the exact fact bundle it was
generated from *before* it is allowed near a user, and anything that fails is
replaced by the deterministic explanation.

Fail closed: a result the checker cannot vouch for is shown as rule-based text
with a visible badge, never as unverified AI text.
"""

from __future__ import annotations

import re
from typing import Any

# Numbers written into the explanation must be traceable to the fact bundle.
# 1% relative tolerance absorbs rounding ("12.9" vs "12.90").
NUMERIC_TOLERANCE = 0.01

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Phrases that contradict an abnormal status.
_REASSURANCE = [
    r"within (?:the )?normal (?:range|limits)",
    r"within (?:the )?reference (?:range|interval)",
    r"\bis normal\b", r"\bare normal\b", r"\bperfectly normal\b",
    r"no cause for concern", r"nothing to worry about", r"\bdon'?t worry\b",
    r"no action (?:is )?(?:required|needed)",
]

# Phrases that overstate a normal status.
_ALARM = [r"\bcritical\b", r"\burgent(?:ly)?\b", r"\bimmediately\b",
          r"\bemergency\b", r"\blife[- ]threatening\b"]

# Safety rails: definitive diagnosis, prescribing, and discouraging care.
_UNSAFE = [
    (r"\byou have\b(?! (?:a|an) )", "asserts a diagnosis in the second person"),
    (r"\b(?:the )?patient has\b", "asserts a diagnosis"),
    (r"\bdiagnosed with\b", "asserts a diagnosis"),
    (r"\bthis (?:means|confirms) (?:you|the patient) (?:have|has)\b", "asserts a diagnosis"),
    # A dose is a mass or volume standing alone ("40 mg once daily"). The same
    # letters followed by a slash are a concentration - "9 ug/L", "13 g/dL",
    # "186 mg/L" - which is the result's own unit. Matching those rejected the
    # legitimate explanation for almost every chemistry result.
    (r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|ml|units?)\b(?!\s*/)", "contains a dose"),
    (r"\b(?:prescribe|prescription|dosage|titrate)\b", "discusses prescribing"),
    (r"\b(?:do not|don'?t|no need to) (?:see|consult|contact|seek)\b", "discourages seeking care"),
]

_FIELDS = ("what_it_measures", "why_flagged", "clinical_significance", "patient_friendly")


def verify_explanation(explanation: dict, fact: dict) -> dict:
    """Run every grounding check for one explanation.

    Returns a report the UI renders verbatim, so a reviewer can see exactly what
    was checked rather than being asked to trust a green tick.
    """
    text = " ".join(str(explanation.get(f, "")) for f in _FIELDS)
    steps = " ".join(str(s) for s in explanation.get("suggested_next_steps") or [])
    full = f"{text} {steps}".strip()

    checks = [
        _check_numeric_grounding(full, fact),
        _check_status_consistency(full, fact),
        _check_direction_consistency(full, fact),
        _check_safety(full),
        _check_completeness(explanation),
    ]
    passed = all(c["passed"] for c in checks)
    return {
        "passed": passed,
        "checks": checks,
        "action": "accepted" if passed else "replaced_with_rule_based",
        "summary": ("All grounding checks passed" if passed else
                    "; ".join(c["detail"] for c in checks if not c["passed"])),
    }


def _allowed_numbers(fact: dict) -> set[float]:
    """Every number the model was legitimately given, plus those in its own labels.

    Names carry digits that are not claims - "Vitamin B12", "Free T4",
    "mL/min/1.73m2" - so numbers appearing in the test name, the description of
    what it measures, or the engine's own rationale are all fair game.
    """
    allowed: set[float] = set()
    for key in ("value", "canonical_value", "reference_low", "reference_high",
                "deviation_index"):
        v = fact.get(key)
        if isinstance(v, (int, float)):
            allowed.add(float(v))
            allowed.add(round(float(v), 1))
            allowed.add(round(float(v)))
    for key in ("display_name", "measures", "rationale", "reference_text",
                "value_display", "canonical_unit", "unit", "result_text",
                "converted_text"):
        for m in _NUMBER_RE.findall(str(fact.get(key) or "")):
            try:
                allowed.add(float(m))
            except ValueError:
                pass
    return allowed


def _check_numeric_grounding(text: str, fact: dict) -> dict:
    """No number may appear that the model was not given."""
    allowed = _allowed_numbers(fact)
    ungrounded: list[str] = []
    for token in _NUMBER_RE.findall(text):
        try:
            n = float(token)
        except ValueError:
            continue
        if any(abs(n - a) <= max(NUMERIC_TOLERANCE * max(abs(a), 1.0), 1e-9)
               for a in allowed):
            continue
        ungrounded.append(token)

    ok = not ungrounded
    return {
        "id": "numeric_grounding",
        "name": "Every number traces to the fact bundle",
        "passed": ok,
        "detail": ("No ungrounded numbers" if ok else
                   f"Ungrounded number(s) not present in the facts given to the model: "
                   f"{', '.join(sorted(set(ungrounded))[:5])}"),
    }


def _check_status_consistency(text: str, fact: dict) -> dict:
    """The prose must not contradict the severity the engine assigned."""
    status = fact.get("status")
    low = text.lower()
    if status in ("Warning", "Critical"):
        hits = [p for p in _REASSURANCE if re.search(p, low)]
        ok = not hits
        detail = ("Wording matches the assigned severity" if ok else
                  f"Reassuring wording used for a {status} result")
    elif status == "Normal":
        hits = [p for p in _ALARM if re.search(p, low)]
        ok = not hits
        detail = ("Wording matches the assigned severity" if ok else
                  "Alarming wording used for a Normal result")
    else:
        ok, detail = True, "No severity assigned; consistency check not applicable"
    return {"id": "status_consistency",
            "name": "Wording agrees with the assigned severity",
            "passed": ok, "detail": detail}


def _check_direction_consistency(text: str, fact: dict) -> dict:
    """A high result must not be described as low, and vice versa."""
    direction = fact.get("direction")
    low = text.lower()
    contradiction = None
    if direction == "High" and re.search(r"\b(?:below|under|lower than)\s+(?:the\s+)?"
                                         r"(?:reference|normal|lower|expected)", low):
        contradiction = "describes a High result as being below the interval"
    elif direction == "Low" and re.search(r"\b(?:above|over|higher than)\s+(?:the\s+)?"
                                          r"(?:reference|normal|upper|expected)", low):
        contradiction = "describes a Low result as being above the interval"
    ok = contradiction is None
    return {"id": "direction_consistency",
            "name": "Direction of the abnormality is stated correctly",
            "passed": ok,
            "detail": "Direction is consistent" if ok else f"Explanation {contradiction}"}


def _check_safety(text: str) -> dict:
    """No diagnosis, no prescribing, no discouraging care."""
    low = text.lower()
    violations = [why for pattern, why in _UNSAFE if re.search(pattern, low)]
    ok = not violations
    return {"id": "safety_rails",
            "name": "No diagnosis, dosing, or advice against seeking care",
            "passed": ok,
            "detail": ("Within scope for decision support" if ok else
                       f"Out of scope: {'; '.join(sorted(set(violations)))}")}


def _check_completeness(explanation: dict) -> dict:
    """An explanation with empty fields is not an explanation."""
    missing = [f for f in _FIELDS if not str(explanation.get(f, "")).strip()]
    ok = not missing
    return {"id": "completeness",
            "name": "All explanation fields were returned",
            "passed": ok,
            "detail": ("All fields present" if ok else
                       f"Empty field(s): {', '.join(missing)}")}


def rule_based_explanation(result: dict) -> dict:
    """Deterministic explanation, assembled from the knowledge base.

    Used when the model is unavailable, over quota, or its output failed
    verification. It is intentionally plain: everything in it is a restatement
    of a fact the engine already established.
    """
    status = result["status"]
    name = result["display_name"]
    measures = result.get("measures") or f"{name} is a laboratory measurement."
    meaning = (result.get("direction_meaning") or "").strip()
    unit = result.get("canonical_unit") or result.get("unit") or ""
    shown = result.get("value_display") or ""
    interval = _interval_text(result)

    interval_with_unit = f"{interval} {unit}".strip()

    if status == "Normal":
        why = (f"{shown} {unit}".strip() +
               f" falls inside the reference interval {interval_with_unit}, "
               "so it is reported as Normal.")
        significance = "No follow-up is indicated for this result on its own."
        friendly = "This result is in the usual range, so there is nothing to act on here."
    elif status == "Unknown":
        why = result.get("error") or "This result could not be classified automatically."
        significance = ("It has been routed for manual review rather than being given a "
                        "severity it cannot be justified with.")
        friendly = ("We could not check this result automatically, so a person will "
                    "review it.")
    else:
        direction = (result.get("direction") or "").lower()
        why = (f"{shown} {unit}".strip() +
               f" is {direction} against the reference interval {interval_with_unit}, "
               f"which the rule engine classifies as {status}.")
        # The display name keeps its own capitalisation - lowercasing it turns
        # "CRP (C-Reactive Protein)" into something that reads like a typo.
        significance = (f"A {direction} {name} result is consistent with {meaning}."
                        if meaning else
                        f"A {direction} {name} result warrants clinical correlation.")
        friendly = (f"This result is outside the usual range ({interval_with_unit}), "
                    "which is why it has been flagged for review.")

    return {
        "what_it_measures": measures,
        "why_flagged": why,
        "clinical_significance": significance,
        "patient_friendly": friendly,
        "suggested_next_steps": [],
    }


def _interval_text(result: dict) -> str:
    low, high = result.get("reference_low"), result.get("reference_high")
    if low is not None and high is not None:
        return f"{low:g}-{high:g}"
    if high is not None:
        return f"up to {high:g}"
    if low is not None:
        return f"{low:g} and above"
    return "defined by the qualitative scale for this test"
