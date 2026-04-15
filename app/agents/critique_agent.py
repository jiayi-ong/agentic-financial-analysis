"""
Critique agent.

Evaluates the synthesis for factual accuracy, evidence sufficiency, logical
consistency, and source attribution.  Returns a typed CritiqueOutput.
No tools — reasoning only.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from app.agents._helpers import load_prompt
from app.config import settings
from app.schemas.critique import CritiqueOutput


def create_critique_agent() -> LlmAgent:
    """Construct and return the critique LlmAgent."""
    return LlmAgent(
        name="critique_agent",
        model=settings.gemini_model,
        description=(
            "Evaluates the synthesis output for accuracy, evidence quality, "
            "and logical consistency.  Returns a structured CritiqueOutput."
        ),
        instruction=load_prompt("critique_agent"),
        tools=[],
        output_schema=CritiqueOutput,
    )
