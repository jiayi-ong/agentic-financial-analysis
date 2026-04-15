"""
Financial data normalisation utilities.

Converts yfinance DataFrames and raw API responses into JSON-serialisable
dicts that match the schemas expected by the financial analysis tools.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def _is_missing(value: Any) -> bool:
    """Return True for None, NaN, or empty string."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def normalize_yfinance_statement(df: Any) -> dict[str, Any]:
    """
    Convert a yfinance financial statement DataFrame to a nested dict.

    yfinance returns DataFrames with columns = reporting periods and
    rows = line items.  This function transposes to
    {period_label: {line_item: value}} for easy iteration.

    Parameters
    ----------
    df:
        pandas DataFrame returned by yf.Ticker().financials etc.

    Returns dict or empty dict if df is None/empty.
    """
    if df is None:
        return {}

    # Accept DataFrames or pre-converted dicts
    if isinstance(df, dict):
        return df

    try:
        import pandas as pd

        if not isinstance(df, pd.DataFrame) or df.empty:
            return {}

        result: dict[str, Any] = {}
        for col in df.columns:
            period_label = str(col)[:10]  # ISO date prefix
            items: dict[str, Any] = {}
            for idx in df.index:
                val = df.at[idx, col]
                if isinstance(val, float) and math.isnan(val):
                    items[str(idx)] = None
                else:
                    try:
                        items[str(idx)] = float(val)
                    except (TypeError, ValueError):
                        items[str(idx)] = str(val)
            result[period_label] = items
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("normalize_yfinance_statement failed: %s", exc)
        return {}


def validate_financial_data(
    data: dict[str, Any],
    required_fields: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Validate that a financial data dict contains the required fields.

    Parameters
    ----------
    data:
        Dict of financial data to validate.
    required_fields:
        List of top-level keys that must be present and non-missing.
        Defaults to checking that the dict is non-empty.

    Returns (is_valid, message).
    is_valid=False means the agent should retry the data collection call.
    """
    if not data:
        return False, "Data dict is empty. Retry the tool call with corrected arguments."

    if required_fields:
        missing = [f for f in required_fields if _is_missing(data.get(f))]
        if missing:
            return (
                False,
                f"Required fields are missing or null: {missing}. "
                "Retry the tool call or try a different data source.",
            )

    return True, "OK"


def flatten_nested_statement(
    statement: dict[str, Any],
    line_items: list[str],
) -> dict[str, dict[str, float | None]]:
    """
    Flatten a nested statement dict to {line_item: {period: value}}.

    Useful for building time-series analysis of specific line items.

    Parameters
    ----------
    statement:
        Nested dict {period: {line_item: value}}.
    line_items:
        List of line item names to extract.

    Returns {line_item: {period: value}}.
    """
    result: dict[str, dict[str, float | None]] = {item: {} for item in line_items}
    for period, items in statement.items():
        if not isinstance(items, dict):
            continue
        for li in line_items:
            val = items.get(li)
            result[li][period] = None if _is_missing(val) else float(val)  # type: ignore[arg-type]
    return result


def compute_cagr(series: dict[str, float | None]) -> float | None:
    """
    Compute the Compound Annual Growth Rate from a {year_label: value} dict.

    Parameters
    ----------
    series:
        Dict with string year keys (e.g. '2024-09-28') and numeric values.

    Returns CAGR as a decimal or None if insufficient data.
    """
    periods = sorted(k for k, v in series.items() if v is not None)
    if len(periods) < 2:
        return None

    start_val = series[periods[0]]
    end_val = series[periods[-1]]
    n = len(periods) - 1

    if not start_val or start_val <= 0:
        return None

    return (end_val / start_val) ** (1 / n) - 1  # type: ignore[operator]
