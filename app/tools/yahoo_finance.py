"""
Yahoo Finance tools (via yfinance).

All functions are synchronous (yfinance is synchronous) and decorated with
``tool_wrapper`` so that errors are returned as structured dicts rather than
raised — allowing the agent to retry with corrected arguments.

Few-shot usage examples are embedded in the docstrings and appended to agent
system prompts at runtime.

──────────────────────────────────────────────────────────────────────────────
Few-shot examples (also embedded in prompts/financial_analyst.md)
──────────────────────────────────────────────────────────────────────────────

Example 1 — get key company info:
    get_stock_info("AAPL")
    → {"status": "success", "data": {"shortName": "Apple Inc.", "sector": "Technology",
       "marketCap": 3200000000000, "trailingPE": 31.4, ...}}

Example 2 — annual income statement:
    get_financials("MSFT", statement="income", frequency="annual")
    → {"status": "success", "data": {"2024": {"Total Revenue": 245122000000,
       "Net Income": 88136000000, ...}, "2023": {...}, ...}}

Example 3 — 1-year daily price history:
    get_price_history("NVDA", period="1y", interval="1d")
    → {"status": "success", "data": [{"Date": "2024-01-02", "Close": 495.22, "Volume": 43210000}, ...]}

Example 4 — analyst recommendations:
    get_analyst_recommendations("GOOGL")
    → {"status": "success", "data": [{"period": "0m", "strongBuy": 28, "buy": 10,
       "hold": 7, "sell": 1, "strongSell": 0}, ...]}
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import yfinance as yf

from app.tools.base import tool_wrapper

logger = logging.getLogger(__name__)


def _df_to_dict(df: Any) -> dict[str, Any]:
    """Convert a yfinance DataFrame to a JSON-serialisable nested dict."""
    if df is None or (hasattr(df, "empty") and df.empty):
        return {}
    return {
        str(col): {str(idx): _safe_value(val) for idx, val in df[col].items()}
        for col in df.columns
    }


def _safe_value(val: Any) -> Any:
    """Convert numpy / pandas scalars to Python primitives."""
    try:
        import numpy as np

        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.floating,)):
            return None if np.isnan(val) else float(val)
        if isinstance(val, np.bool_):
            return bool(val)
    except ImportError:
        pass
    import math

    if isinstance(val, float) and math.isnan(val):
        return None
    return val


@tool_wrapper(
    suggestion_on_error="Ensure the ticker is a valid NYSE/NASDAQ symbol (e.g. 'AAPL', 'MSFT').",
    retries=2,
)
def get_stock_info(ticker: str) -> dict[str, Any]:
    """
    Return key company information for a given ticker.

    Parameters
    ----------
    ticker:
        Uppercase stock ticker symbol (e.g. 'AAPL').

    Returns dict with fields: shortName, longName, sector, industry, country,
    marketCap, trailingPE, forwardPE, priceToBook, dividendYield,
    fiftyTwoWeekHigh, fiftyTwoWeekLow, longBusinessSummary, website.
    """
    try:
        info = yf.Ticker(ticker.upper()).info
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"yfinance failed to fetch info for {ticker}: {exc}") from exc

    # yfinance can return None or an empty dict on transient Yahoo Finance errors
    if not info:
        raise RuntimeError(
            f"yfinance returned no data for {ticker}. "
            "Yahoo Finance may be temporarily unavailable — please retry."
        )

    keys = [
        "shortName", "longName", "sector", "industry", "country",
        "marketCap", "enterpriseValue", "trailingPE", "forwardPE",
        "priceToBook", "priceToSalesTrailing12Months", "dividendYield",
        "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "currentPrice",
        "longBusinessSummary", "website", "fullTimeEmployees",
        "totalRevenue", "revenueGrowth", "grossMargins", "operatingMargins",
        "profitMargins", "returnOnEquity", "returnOnAssets",
        "debtToEquity", "currentRatio", "quickRatio",
    ]
    return {k: _safe_value(info.get(k)) for k in keys}


@tool_wrapper(
    suggestion_on_error=(
        "Use a standard period string: '1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'. "
        "Use a standard interval: '1m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo'."
    ),
    retries=2,
)
def get_price_history(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
) -> list[dict[str, Any]]:
    """
    Return OHLCV price history for a ticker.

    Parameters
    ----------
    ticker:
        Uppercase stock ticker symbol.
    period:
        Lookback window. Valid values: '1d','5d','1mo','3mo','6mo','1y',
        '2y','5y','10y','ytd','max'.
    interval:
        Bar interval. Valid values: '1m','2m','5m','15m','30m','60m','90m',
        '1h','1d','5d','1wk','1mo','3mo'.

    Returns a list of dicts: [{Date, Open, High, Low, Close, Volume}, ...].
    """
    hist = yf.Ticker(ticker.upper()).history(period=period, interval=interval)
    if hist.empty:
        return []
    # Strip timezone so date strings are clean "YYYY-MM-DD HH:MM:SS" without
    # offset suffixes — prevents "Mixed timezones" errors in agent chart code.
    if getattr(hist.index, "tz", None) is not None:
        hist.index = hist.index.tz_convert("UTC").tz_localize(None)
    hist.index = hist.index.astype(str)
    return [
        {
            "Date": date,
            "Open": _safe_value(row.get("Open")),
            "High": _safe_value(row.get("High")),
            "Low": _safe_value(row.get("Low")),
            "Close": _safe_value(row.get("Close")),
            "Volume": _safe_value(row.get("Volume")),
        }
        for date, row in hist.iterrows()
    ]


@tool_wrapper(
    suggestion_on_error=(
        "statement must be 'income', 'balance', or 'cashflow'. "
        "frequency must be 'annual' or 'quarterly'."
    ),
    retries=2,
)
def get_financials(
    ticker: str,
    statement: Literal["income", "balance", "cashflow"] = "income",
    frequency: Literal["annual", "quarterly"] = "annual",
) -> dict[str, Any]:
    """
    Return a financial statement for a ticker.

    Parameters
    ----------
    ticker:
        Uppercase stock ticker symbol.
    statement:
        Which statement: 'income', 'balance', or 'cashflow'.
    frequency:
        'annual' for yearly data, 'quarterly' for quarterly data.

    Returns a nested dict: {period_label: {line_item: value, ...}, ...}.
    """
    t = yf.Ticker(ticker.upper())
    mapping = {
        ("income", "annual"): lambda: t.financials,
        ("income", "quarterly"): lambda: t.quarterly_financials,
        ("balance", "annual"): lambda: t.balance_sheet,
        ("balance", "quarterly"): lambda: t.quarterly_balance_sheet,
        ("cashflow", "annual"): lambda: t.cashflow,
        ("cashflow", "quarterly"): lambda: t.quarterly_cashflow,
    }
    getter = mapping.get((statement, frequency))
    if getter is None:
        raise ValueError(f"Unknown combination: statement={statement}, frequency={frequency}")
    df = getter()
    return _df_to_dict(df)


@tool_wrapper(
    suggestion_on_error="Ensure the ticker is valid. Recommendations may not exist for all stocks.",
    retries=2,
)
def get_analyst_recommendations(ticker: str) -> list[dict[str, Any]]:
    """
    Return the most recent analyst consensus recommendations.

    Parameters
    ----------
    ticker:
        Uppercase stock ticker symbol.

    Returns a list of dicts with fields: period, strongBuy, buy, hold, sell, strongSell.
    """
    t = yf.Ticker(ticker.upper())
    recs = t.recommendations
    if recs is None or (hasattr(recs, "empty") and recs.empty):
        return []
    recs.index = recs.index.astype(str)
    return recs.reset_index().to_dict(orient="records")


@tool_wrapper(
    suggestion_on_error="Ensure the ticker is valid.",
    retries=2,
)
def get_earnings_calendar(ticker: str) -> list[dict[str, Any]]:
    """
    Return upcoming earnings dates and EPS estimates for a ticker.

    Parameters
    ----------
    ticker:
        Uppercase stock ticker symbol.
    """
    t = yf.Ticker(ticker.upper())
    cal = t.calendar
    if not cal:
        return []
    return [{"key": k, "value": str(v)} for k, v in cal.items()]
