"""
Financial Analyst agent.

Performs quantitative analysis of financial statements, valuation multiples,
FCF, balance sheet health, and runs the pre-written financial metrics tools.

Tools: get_stock_info, get_price_history, get_financials,
       compute_dcf, compute_valuation_multiples, compute_revenue_growth,
       compute_margin_trends, compute_dupont, compute_altman_z,
       execute_python
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from app.agents._helpers import load_prompt
from app.config import settings
from app.tools.code_executor import execute_python
from app.tools.financial_metrics import (
    compute_altman_z,
    compute_dcf,
    compute_dupont,
    compute_margin_trends,
    compute_revenue_growth,
    compute_valuation_multiples,
)
from app.tools.yahoo_finance import (
    get_analyst_recommendations,
    get_financials,
    get_price_history,
    get_stock_info,
)


def create_financial_analyst() -> LlmAgent:
    """Construct and return the financial analyst LlmAgent."""
    return LlmAgent(
        name="financial_analyst",
        model=settings.gemini_model,
        description=(
            "Analyses financial statements, valuation multiples, DCF intrinsic value, "
            "margin trends, and balance sheet health from Yahoo Finance data."
        ),
        instruction=load_prompt("financial_analyst"),
        # Disable extended thinking so the model follows tool-calling instructions
        # literally rather than reasoning its way to hallucinated answers.
        generate_content_config=genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
        tools=[
            FunctionTool(func=get_stock_info),
            FunctionTool(func=get_price_history),
            FunctionTool(func=get_financials),
            FunctionTool(func=get_analyst_recommendations),
            FunctionTool(func=compute_dcf),
            FunctionTool(func=compute_valuation_multiples),
            FunctionTool(func=compute_revenue_growth),
            FunctionTool(func=compute_margin_trends),
            FunctionTool(func=compute_dupont),
            FunctionTool(func=compute_altman_z),
            FunctionTool(func=execute_python),
        ],
    )
