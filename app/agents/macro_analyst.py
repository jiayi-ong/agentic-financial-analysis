"""
Macro & Geopolitical Analyst agent.

Assesses economy-wide and geopolitical factors affecting the technology sector
and the specific company under analysis.

Tools: crawl_news
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from app.agents._helpers import load_prompt
from app.config import settings
from app.tools.web_crawler import crawl_news


def create_macro_analyst() -> LlmAgent:
    """Construct and return the macro analyst LlmAgent."""
    return LlmAgent(
        name="macro_analyst",
        model=settings.gemini_model,
        description=(
            "Analyses macroeconomic conditions, interest rates, geopolitical risks, "
            "and sector-wide trends relevant to the target company."
        ),
        instruction=load_prompt("macro_analyst"),
        generate_content_config=genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
        tools=[
            FunctionTool(func=crawl_news),
        ],
    )
