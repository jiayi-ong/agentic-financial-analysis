"""
News & Sentiment Analyst agent.

Assesses investor sentiment, market mood, and the news narrative surrounding
the company using recent news articles from Reuters, CNBC, and Yahoo Finance.

Tools: crawl_news, get_stock_info
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types as genai_types

from app.agents._helpers import load_prompt
from app.config import settings
from app.tools.web_crawler import crawl_news
from app.tools.yahoo_finance import get_stock_info


def create_sentiment_analyst() -> LlmAgent:
    """Construct and return the sentiment analyst LlmAgent."""
    return LlmAgent(
        name="sentiment_analyst",
        model=settings.gemini_model,
        description=(
            "Analyses news coverage and investor sentiment from Reuters, CNBC, "
            "and Yahoo Finance News to assess market mood around the target company."
        ),
        instruction=load_prompt("sentiment_analyst"),
        generate_content_config=genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
        tools=[
            FunctionTool(func=crawl_news),
            FunctionTool(func=get_stock_info),
        ],
    )
