"""Request and response models for the public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Status = Literal["Critical", "Warning", "Normal", "Unknown"]


class PatientContext(BaseModel):
    """Non-identifying context that changes which reference interval applies.

    Deliberately minimal: sex and age are the only patient attributes the
    classifier uses, and nothing here is forwarded to the language model.
    """
    sex: Literal["male", "female", "unspecified"] | None = None
    age_years: float | None = Field(None, ge=0, le=130)
    reference: str | None = Field(
        None, max_length=64,
        description="Your own non-identifying accession or case reference. "
                    "Echoed back for correlation; never sent to the AI provider.",
    )


class LabInput(BaseModel):
    test_name: str = Field(..., min_length=1, max_length=200)
    value: str | float | int | None = Field(
        ..., description="Numeric ('12.9'), censored ('<0.01') or qualitative ('Negatif', '1+')."
    )
    unit: str | None = Field(None, max_length=40)
    reference_low: float | None = None
    reference_high: float | None = None
    reference_range: str | None = Field(
        None, max_length=60,
        description="Free-text interval from the report, e.g. '15-150' or '<5'.",
    )
    collected_on: str | None = Field(None, max_length=40)

    @field_validator("test_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("test_name cannot be blank")
        return v


class AnalyzeOptions(BaseModel):
    explain: bool = Field(True, description="Generate AI explanations. Off = rule-based only.")
    include_evidence: bool = Field(True, description="Include the full audit trail per result.")


class AnalyzeRequest(BaseModel):
    labs: list[LabInput] = Field(..., min_length=1, max_length=300)
    patient: PatientContext | None = None
    options: AnalyzeOptions = Field(default_factory=AnalyzeOptions)


class Explanation(BaseModel):
    what_it_measures: str = ""
    why_flagged: str = ""
    clinical_significance: str = ""
    patient_friendly: str = ""
    ai_suggested_next_steps: list[str] = Field(default_factory=list)
    source: Literal["ai", "rule_based"] = "rule_based"
    source_note: str = ""


class VerificationCheck(BaseModel):
    id: str
    name: str
    passed: bool
    detail: str


class Verification(BaseModel):
    passed: bool
    action: str
    summary: str
    checks: list[VerificationCheck] = Field(default_factory=list)


class NextSteps(BaseModel):
    urgency: str
    sla: str
    specialty: str
    actions: list[str] = Field(default_factory=list)
    source: str = ""


class AnalyzedResult(BaseModel):
    id: int
    test_name: str
    display_name: str
    category: str
    loinc: str | None = None
    value: float | None = None
    value_display: str = ""
    unit: str = ""
    canonical_value: float | None = None
    canonical_unit: str = ""
    unit_converted: bool = False
    status: Status
    direction: str = ""
    deviation_index: float | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    reference_text: str = ""
    flags: list[str] = Field(default_factory=list)
    error: str | None = None
    explanation: Explanation
    verification: Verification | None = None
    next_steps: NextSteps
    evidence: dict[str, Any] | None = None


class SeverityGroup(BaseModel):
    status: Status
    count: int
    results: list[AnalyzedResult]


class Summary(BaseModel):
    total: int
    critical: int
    warning: int
    normal: int
    unknown: int
    needs_review: int
    highest_severity: Status
    headline: str


class AIReport(BaseModel):
    """Honest accounting of what the model did on this request."""
    provider: str
    model: str
    used: bool
    detail: str
    latency_ms: int = 0
    explanations_from_cache: int = 0
    verified_ok: int = 0
    replaced_after_failed_check: int = 0


class PipelineStep(BaseModel):
    step: str
    detail: str
    duration_ms: int
    tool_calls: list[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    request_id: str
    generated_at: str
    patient_reference: str | None = None
    summary: Summary
    groups: list[SeverityGroup]
    results: list[AnalyzedResult]
    ai: AIReport
    pipeline: list[PipelineStep]
    knowledge_base_version: str = ""
    disclaimer: str


class EvaluationRow(BaseModel):
    test_name: str
    value: str
    expected: str
    predicted: str
    direction: str
    agree: bool
    note: str = ""


class EvaluationReport(BaseModel):
    """Accuracy of the classifier against a labelled dataset."""
    dataset: str
    rows: int
    agreements: int
    accuracy: float
    by_expected: dict[str, dict[str, int]]
    disagreements: list[EvaluationRow]
    note: str
