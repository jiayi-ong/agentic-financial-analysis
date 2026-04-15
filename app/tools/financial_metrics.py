"""
Pre-written deterministic financial analysis functions exposed as ADK tools.

These functions accept normalised dicts (as returned by yahoo_finance.py) and
produce JSON-serialisable output.  They never call external APIs or LLMs.

Agents should use these tools for standard financial metrics and reserve the
code_executor for custom or one-off computations.

──────────────────────────────────────────────────────────────────────────────
Few-shot examples (also embedded in prompts/financial_analyst.md)
──────────────────────────────────────────────────────────────────────────────

Example 1 — DCF valuation:
    compute_dcf(
        fcf_series=[5.0, 5.5, 6.05, 6.66, 7.32],  # USD billions
        growth_rate=0.10,   # terminal growth (beyond the series)
        wacc=0.09,
        terminal_growth=0.025
    )
    → {"status": "success", "data": {"intrinsic_value_bn": 87.4,
       "terminal_value_bn": 72.3, "pv_fcf_bn": 15.1}}

Example 2 — Valuation multiples (use stock_info from get_stock_info):
    compute_valuation_multiples(stock_info)
    → {"status": "success", "data": {"pe_ratio": 31.4, "ev_ebitda": 24.1,
       "price_to_sales": 8.2, "price_to_book": 47.3}}

Example 3 — Revenue growth YoY (use income statement from get_financials):
    compute_revenue_growth(income_stmt)
    → {"status": "success", "data": {"2024": null, "2023": 0.08, "2022": 0.28}}

Example 4 — DuPont decomposition:
    compute_dupont(income_stmt, balance_sheet)
    → {"status": "success", "data": {"net_profit_margin": 0.253,
       "asset_turnover": 1.12, "equity_multiplier": 5.6, "roe": 1.59}}
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from app.tools.base import tool_wrapper

logger = logging.getLogger(__name__)


def _unwrap_tool_result(v: Any) -> dict[str, Any]:
    """
    Normalise the dict argument that the LLM passes to a compute tool.

    The model sometimes passes:
      - A plain dict (correct)                    → use as-is
      - A tool-wrapper envelope {"status": "success", "data": {...}}
                                                  → return .data
      - A JSON string of either of the above      → parse then apply above
    """
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TypeError(f"Cannot parse argument as JSON: {v[:120]!r}") from exc
    if isinstance(v, dict):
        # Unwrap tool_wrapper envelope transparently
        if v.get("status") in ("success", "error") and "data" in v:
            return v["data"]  # type: ignore[return-value]
        return v
    raise TypeError(f"Expected dict or JSON string, got {type(v).__name__}")


def _first_numeric(d: dict[str, Any], *keys: str) -> float | None:
    """Return the first non-None numeric value found among keys."""
    for k in keys:
        v = d.get(k)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _extract_series(statement: dict[str, Any], line_item: str) -> dict[str, float | None]:
    """
    From a yfinance-style statement dict {period: {line_item: value}},
    extract {period: value} for the given line_item.
    """
    result = {}
    for period, items in statement.items():
        if isinstance(items, dict):
            result[period] = items.get(line_item)
    return result


@tool_wrapper(
    suggestion_on_error=(
        "fcf_series must be a non-empty list of positive floats (in any currency unit). "
        "wacc and terminal_growth must be decimals (e.g. 0.09 for 9%). "
        "terminal_growth must be less than wacc."
    )
)
def compute_dcf(
    fcf_series: list[float],
    growth_rate: float,
    wacc: float,
    terminal_growth: float,
) -> dict[str, Any]:
    """
    Compute a simple multi-stage DCF valuation.

    Parameters
    ----------
    fcf_series:
        List of projected free cash flows (any consistent currency unit, e.g. USD billions).
        The series represents explicit forecast periods.
    growth_rate:
        Annual FCF growth rate applied BEYOND the explicit forecast period
        (before terminal value kicks in).  Decimal (e.g. 0.10 = 10 %).
    wacc:
        Weighted average cost of capital.  Decimal (e.g. 0.09 = 9 %).
    terminal_growth:
        Perpetuity growth rate for terminal value.  Must be < wacc.
        Decimal (e.g. 0.025 = 2.5 %).

    Returns dict with: intrinsic_value (sum of PV), terminal_value, pv_fcf_sum,
    discount_factors, and the unit note.
    """
    if not fcf_series:
        raise ValueError("fcf_series must contain at least one value.")
    if terminal_growth >= wacc:
        raise ValueError("terminal_growth must be less than wacc to avoid infinite terminal value.")

    pv_sum = 0.0
    discount_factors = []
    for t, fcf in enumerate(fcf_series, start=1):
        df = 1 / (1 + wacc) ** t
        pv_sum += fcf * df
        discount_factors.append(round(df, 6))

    last_fcf = fcf_series[-1]
    terminal_fcf = last_fcf * (1 + growth_rate)
    terminal_value = terminal_fcf / (wacc - terminal_growth)
    n = len(fcf_series)
    pv_terminal = terminal_value / (1 + wacc) ** n

    intrinsic = pv_sum + pv_terminal

    return {
        "intrinsic_value": round(intrinsic, 4),
        "pv_fcf_sum": round(pv_sum, 4),
        "terminal_value": round(terminal_value, 4),
        "pv_terminal_value": round(pv_terminal, 4),
        "discount_factors": discount_factors,
        "note": "Values are in the same unit as the input fcf_series.",
    }


@tool_wrapper(
    suggestion_on_error=(
        "Pass the dict returned by get_stock_info() as the stock_info argument."
    )
)
def compute_valuation_multiples(stock_info: dict[str, Any]) -> dict[str, Any]:
    """
    Extract and return key valuation multiples from a stock_info dict.

    Parameters
    ----------
    stock_info:
        Dict returned by get_stock_info() — pass the full tool result or just
        the data payload; both are accepted.

    Returns dict with: pe_ratio, forward_pe, ev_ebitda, price_to_sales,
    price_to_book, dividend_yield, market_cap_bn, enterprise_value_bn.
    """
    stock_info = _unwrap_tool_result(stock_info)

    def _bn(v: float | None) -> float | None:
        return round(v / 1e9, 2) if v is not None else None

    return {
        "pe_ratio": stock_info.get("trailingPE"),
        "forward_pe": stock_info.get("forwardPE"),
        "price_to_sales": stock_info.get("priceToSalesTrailing12Months"),
        "price_to_book": stock_info.get("priceToBook"),
        "dividend_yield_pct": (
            round(stock_info["dividendYield"] * 100, 2)
            if stock_info.get("dividendYield") else None
        ),
        "market_cap_bn": _bn(stock_info.get("marketCap")),
        "enterprise_value_bn": _bn(stock_info.get("enterpriseValue")),
        "gross_margin_pct": (
            round(stock_info["grossMargins"] * 100, 2)
            if stock_info.get("grossMargins") else None
        ),
        "operating_margin_pct": (
            round(stock_info["operatingMargins"] * 100, 2)
            if stock_info.get("operatingMargins") else None
        ),
        "net_profit_margin_pct": (
            round(stock_info["profitMargins"] * 100, 2)
            if stock_info.get("profitMargins") else None
        ),
    }


@tool_wrapper(
    suggestion_on_error=(
        "Pass the dict returned by get_financials(ticker, 'income', 'annual') as income_stmt."
    )
)
def compute_revenue_growth(income_stmt: dict[str, Any]) -> dict[str, float | None]:
    """
    Compute year-over-year revenue growth rates from an annual income statement.

    Parameters
    ----------
    income_stmt:
        Dict returned by get_financials(ticker, 'income', 'annual') — pass the
        full tool result or just the data payload; both are accepted.

    Returns dict {period: yoy_growth_rate | None}; the earliest period has None.
    """
    income_stmt = _unwrap_tool_result(income_stmt)
    revenue = _extract_series(income_stmt, "Total Revenue")
    periods = sorted(revenue.keys(), reverse=True)  # most recent first
    growth: dict[str, float | None] = {}
    for i, period in enumerate(periods):
        if i == len(periods) - 1:
            growth[period] = None
        else:
            curr = revenue[period]
            prev = revenue[periods[i + 1]]
            if curr is not None and prev and prev != 0:
                growth[period] = round((curr - prev) / abs(prev), 4)
            else:
                growth[period] = None
    return growth


@tool_wrapper(
    suggestion_on_error=(
        "Pass dicts from get_financials(ticker, 'income', 'annual') and "
        "get_financials(ticker, 'balance', 'annual') as income_stmt and balance_sheet."
    )
)
def compute_margin_trends(income_stmt: dict[str, Any]) -> dict[str, Any]:
    """
    Compute gross, operating, and net margin trends over time.

    Parameters
    ----------
    income_stmt:
        Dict returned by get_financials(ticker, 'income', 'annual') — pass the
        full tool result or just the data payload; both are accepted.

    Returns dict {period: {gross_margin, operating_margin, net_margin}}.
    """
    income_stmt = _unwrap_tool_result(income_stmt)
    revenue = _extract_series(income_stmt, "Total Revenue")
    gross_profit = _extract_series(income_stmt, "Gross Profit")
    operating_income = _extract_series(income_stmt, "Operating Income")
    net_income = _extract_series(income_stmt, "Net Income")

    result = {}
    for period in sorted(revenue.keys(), reverse=True):
        rev = revenue.get(period)
        if not rev:
            continue
        result[period] = {
            "gross_margin_pct": round(gross_profit.get(period, 0) / rev * 100, 2) if gross_profit.get(period) else None,
            "operating_margin_pct": round(operating_income.get(period, 0) / rev * 100, 2) if operating_income.get(period) else None,
            "net_margin_pct": round(net_income.get(period, 0) / rev * 100, 2) if net_income.get(period) else None,
        }
    return result


@tool_wrapper(
    suggestion_on_error=(
        "Pass dicts from get_financials() for 'income'/'annual' and 'balance'/'annual'."
    )
)
def compute_dupont(
    income_stmt: dict[str, Any],
    balance_sheet: dict[str, Any],
) -> dict[str, Any]:
    """
    DuPont decomposition: ROE = Net Profit Margin × Asset Turnover × Equity Multiplier.

    Pass full tool results or just the data payloads; both are accepted.
    Returns the most recent period's decomposed ROE.
    """
    income_stmt = _unwrap_tool_result(income_stmt)
    balance_sheet = _unwrap_tool_result(balance_sheet)
    periods_i = sorted(income_stmt.keys(), reverse=True)
    periods_b = sorted(balance_sheet.keys(), reverse=True)
    if not periods_i or not periods_b:
        return {}

    latest_i = income_stmt[periods_i[0]]
    latest_b = balance_sheet[periods_b[0]]

    net_income = _first_numeric(latest_i, "Net Income")
    revenue = _first_numeric(latest_i, "Total Revenue")
    total_assets = _first_numeric(latest_b, "Total Assets")
    equity = _first_numeric(latest_b, "Stockholders Equity", "Common Stock Equity")

    npm = (net_income / revenue) if (net_income and revenue) else None
    at = (revenue / total_assets) if (revenue and total_assets) else None
    em = (total_assets / equity) if (total_assets and equity) else None
    roe = (npm * at * em) if (npm is not None and at is not None and em is not None) else None

    return {
        "period": periods_i[0],
        "net_profit_margin": round(npm, 4) if npm is not None else None,
        "asset_turnover": round(at, 4) if at is not None else None,
        "equity_multiplier": round(em, 4) if em is not None else None,
        "roe": round(roe, 4) if roe is not None else None,
    }


@tool_wrapper(
    suggestion_on_error=(
        "Pass dicts from get_financials() for 'balance'/'annual' and 'income'/'annual'."
    )
)
def compute_altman_z(
    balance_sheet: dict[str, Any],
    income_stmt: dict[str, Any],
) -> dict[str, Any]:
    """
    Altman Z-Score for bankruptcy risk (public companies formula).

    Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5

    Interpretation:
      Z > 2.99  → Safe zone
      1.81–2.99 → Grey zone
      Z < 1.81  → Distress zone

    Pass full tool results or just the data payloads; both are accepted.
    Returns score, zone, and component ratios.
    """
    balance_sheet = _unwrap_tool_result(balance_sheet)
    income_stmt = _unwrap_tool_result(income_stmt)
    periods_b = sorted(balance_sheet.keys(), reverse=True)
    periods_i = sorted(income_stmt.keys(), reverse=True)
    if not periods_b or not periods_i:
        return {}

    b = balance_sheet[periods_b[0]]
    inc = income_stmt[periods_i[0]]

    current_assets = _first_numeric(b, "Current Assets")
    current_liabilities = _first_numeric(b, "Current Liabilities")
    total_assets = _first_numeric(b, "Total Assets")
    total_liabilities = _first_numeric(b, "Total Liabilities Net Minority Interest", "Total Liabilities")
    retained_earnings = _first_numeric(b, "Retained Earnings")
    equity_market = _first_numeric(b, "Common Stock Equity", "Stockholders Equity")
    ebit = _first_numeric(inc, "EBIT", "Operating Income")
    revenue = _first_numeric(inc, "Total Revenue")

    if not total_assets:
        return {"error": "Total Assets not found in balance sheet"}

    working_capital = (current_assets - current_liabilities) if (current_assets and current_liabilities) else None

    x1 = (working_capital / total_assets) if working_capital is not None else None
    x2 = (retained_earnings / total_assets) if retained_earnings is not None else None
    x3 = (ebit / total_assets) if ebit is not None else None
    x4 = (equity_market / total_liabilities) if (equity_market and total_liabilities) else None
    x5 = (revenue / total_assets) if revenue is not None else None

    components = {"x1": x1, "x2": x2, "x3": x3, "x4": x4, "x5": x5}
    if any(v is None for v in components.values()):
        missing = [k for k, v in components.items() if v is None]
        return {"error": f"Missing data for components: {missing}", "partial": components}

    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5  # type: ignore[operator]

    if z > 2.99:
        zone = "Safe"
    elif z >= 1.81:
        zone = "Grey"
    else:
        zone = "Distress"

    return {
        "z_score": round(z, 3),
        "zone": zone,
        "components": {k: round(v, 4) for k, v in components.items()},  # type: ignore[arg-type]
    }
