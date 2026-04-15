"""
Data contracts for the critique agent.

The critique agent must return a CritiqueOutput; the orchestrator uses it to
decide whether to accept the synthesis, trigger a partial re-run, or abstain.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CritiqueIssue(BaseModel):
    """A single typed issue found in the synthesis."""

    severity: Literal["low", "medium", "high"] = Field(
        ..., description="Impact level of this issue on the overall conclusion"
    )
    category: Literal[
        "factual_error",
        "insufficient_evidence",
        "outdated_data",
        "logical_inconsistency",
        "missing_source",
    ] = Field(..., description="Nature of the issue")
    quote: str = Field(
        ...,
        description="Verbatim excerpt from the synthesis that exhibits the issue (≤ 300 chars)",
    )
    tag: str = Field(
        ...,
        description="Short label for UI display (e.g. 'unsupported claim', 'stale data')",
    )
    affected_specialist: str = Field(
        ...,
        description="Name of the specialist whose output this issue originates from",
    )
    suggestion: str = Field(
        ...,
        description="Brief suggestion on how to fix this issue in a re-run",
    )


class CritiqueOutput(BaseModel):
    """Full critique result returned by the critique agent."""

    issues: list[CritiqueIssue] = Field(
        default_factory=list,
        description="All issues found; empty list means the synthesis passed",
    )
    overall_severity: Literal["none", "low", "medium", "high"] = Field(
        ...,
        description=(
            "'none' = synthesis accepted; 'low'/'medium' = minor fixes; "
            "'high' = major problems requiring targeted re-run"
        ),
    )
    requires_rerun: bool = Field(
        ...,
        description="True when the orchestrator should re-run affected specialists",
    )
    affected_specialists: list[str] = Field(
        default_factory=list,
        description="Specialists to re-run (populated when requires_rerun=True)",
    )
    critique_summary: str = Field(
        "",
        description="Human-readable paragraph summarising the critique findings",
    )

    @property
    def passed(self) -> bool:
        return self.overall_severity == "none"
