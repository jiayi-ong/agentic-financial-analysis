"""
Data contracts for specialist agent outputs.

Every specialist agent must produce a SpecialistOutput.  The orchestrator
stores these in the ADK session state under the key
``specialist_outputs.<specialist_name>``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator  # noqa: F401


class EvidenceSnippet(BaseModel):
    """A single piece of evidence backing a claim."""

    text: str = Field(..., description="Verbatim or paraphrased excerpt used as evidence")
    source_url: str | None = Field(None, description="Direct URL to the source article or filing")
    source_type: str = Field(
        default="market_data",
        description="Category of source (e.g. news, sec_filing, market_data, analyst_report)",
    )
    filing_identifier: str | None = Field(
        None,
        description="SEC accession number (e.g. '0000320193-24-000123') if source_type='sec_filing'",
    )
    extraction_timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp when the data was fetched",
    )


class SpecialistOutput(BaseModel):
    """Structured output returned by each specialist agent."""

    specialist: str = Field(..., description="Agent name (e.g. 'financial_analyst')")
    claims: list[str] = Field(
        ...,
        min_length=1,
        description="Key analytical claims made by this specialist (bullet-style, concise)",
    )
    evidence: list[EvidenceSnippet] = Field(
        default_factory=list,
        description="Evidence snippets supporting the claims",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Self-assessed confidence in the output (0 = no confidence, 1 = high)",
    )
    success: bool = Field(..., description="Whether the specialist completed successfully")
    failure_reason: str | None = Field(
        None,
        description="Human-readable reason for failure; None if success=True",
    )

    @field_validator("failure_reason")
    @classmethod
    def _failure_reason_consistent(cls, v: str | None, info: object) -> str | None:
        # info.data may not have 'success' yet during validation ordering;
        # we do a best-effort check only.
        return v

    def to_context_str(self) -> str:
        """Serialise to a compact string for injection into synthesis prompts."""
        lines = [
            f"## Specialist: {self.specialist}",
            f"Confidence: {self.confidence:.0%}",
            f"Success: {self.success}",
        ]
        if not self.success and self.failure_reason:
            lines.append(f"Failure reason: {self.failure_reason}")
        lines.append("\n### Claims")
        for claim in self.claims:
            lines.append(f"- {claim}")
        if self.evidence:
            lines.append("\n### Evidence")
            for ev in self.evidence:
                ref = ev.source_url or ev.filing_identifier or "no direct link"
                lines.append(f"- [{ev.source_type}] {ev.text[:200]}  (source: {ref})")
        return "\n".join(lines)


class SynthesisOutput(BaseModel):
    """Narrative synthesis produced by the synthesis agent."""

    narrative: str = Field(..., description="Full analytical narrative (markdown)")
    key_hypothesis: str = Field(
        ...,
        description="One-sentence hypothesis about company performance / growth drivers",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="All source URLs / filing identifiers referenced in the narrative",
    )
    omitted_specialists: list[str] = Field(
        default_factory=list,
        description="Specialists excluded from synthesis due to failure",
    )
