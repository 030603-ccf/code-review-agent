"""Finding — the contract that travels between pipeline stages."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["security", "performance", "readability", "best_practice",
                   "correctness"]
Severity = Literal["critical", "high", "medium", "low"]
Verdict = Literal["confirmed", "rejected", "uncertain"]


class Finding(BaseModel):
    """A single defect / quality issue."""

    id: str
    category: Category
    severity: Severity
    file_path: str
    line_start: int
    line_end: int
    title: str
    description: str
    evidence: str
    suggestion: str
    confidence: float = Field(ge=0.0, le=1.0)

    # Set by the aggregator when evidence was fuzzy-matched back to source.
    evidence_corrected: bool = False

    # Set by second_review when arbitration runs.
    second_verdict: Verdict | None = None
    second_reason: str | None = None


class FindingList(BaseModel):
    """Wrapper the reviewer model outputs; top-level object parses more
    reliably than a bare array for small models."""

    findings: list[Finding]
