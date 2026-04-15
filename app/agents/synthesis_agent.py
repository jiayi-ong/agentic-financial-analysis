"""
Synthesis agent.

Combines all specialist outputs into a coherent, evidence-backed analytical
narrative with a single key hypothesis.  No tools — reasoning only.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from app.agents._helpers import load_prompt
from app.config import settings


def create_synthesis_agent() -> LlmAgent:
    """Construct and return the synthesis LlmAgent."""
    return LlmAgent(
        name="synthesis_agent",
        model=settings.gemini_model,
        description=(
            "Combines specialist outputs into a coherent analytical narrative "
            "with a key hypothesis, retaining all source references."
        ),
        instruction=load_prompt("synthesis_agent"),
        tools=[],
    )
