"""
Analyst Ratings & SEC Filings agent.

Summarises Wall Street analyst consensus and key disclosures from SEC filings
(10-K MD&A, risk factors, 8-K material events).

Tools: get_analyst_recommendations, get_earnings_calendar,
       search_filings, get_filing_content
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.agents._helpers import load_prompt
from app.config import settings
from app.tools.sec_edgar import get_filing_content, search_filings
from app.tools.yahoo_finance import get_analyst_recommendations, get_earnings_calendar


def create_ratings_analyst() -> LlmAgent:
    """Construct and return the ratings analyst LlmAgent."""
    return LlmAgent(
        name="ratings_analyst",
        model=settings.gemini_model,
        description=(
            "Summarises Wall Street analyst consensus ratings and key disclosures "
            "from SEC filings (10-K, 10-Q, 8-K)."
        ),
        instruction=load_prompt("ratings_analyst"),
        tools=[
            FunctionTool(func=get_analyst_recommendations),
            FunctionTool(func=get_earnings_calendar),
            FunctionTool(func=search_filings),
            FunctionTool(func=get_filing_content),
        ],
    )
