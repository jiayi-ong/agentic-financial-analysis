"""
Stock ticker validation utility.

Validates user-supplied ticker symbols or company names against a local CSV
database of NYSE and NASDAQ tickers.  Uses exact match first, then falls back
to case-insensitive fuzzy name matching via difflib.

The CSV is loaded once at module import time (small dataset, fast startup).
"""

from __future__ import annotations

import csv
import difflib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "tickers.csv"
_FUZZY_CUTOFF = 0.6  # Minimum similarity score for a fuzzy name match

# In-memory lookup tables built at import time
_ticker_to_name: dict[str, str] = {}   # "AAPL" → "Apple Inc."
_name_to_ticker: dict[str, str] = {}   # "apple inc." → "AAPL"
_all_names: list[str] = []             # lowercase company names for fuzzy match


def _load_tickers() -> None:
    """Parse tickers.csv into the in-memory lookup tables."""
    global _ticker_to_name, _name_to_ticker, _all_names

    if not _DATA_PATH.exists():
        logger.warning(
            "Ticker database not found at %s. Validation will accept any input.", _DATA_PATH
        )
        return

    with _DATA_PATH.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("Symbol", row.get("ticker", "")).strip().upper()
            name = row.get("Name", row.get("company_name", "")).strip()
            if ticker and name:
                _ticker_to_name[ticker] = name
                _name_to_ticker[name.lower()] = ticker
                _all_names.append(name.lower())

    logger.info("Loaded %d tickers from %s", len(_ticker_to_name), _DATA_PATH)


_load_tickers()


def validate_ticker(user_input: str) -> tuple[bool, str | None]:
    """
    Validate a user-supplied string as a stock ticker or company name.

    Parameters
    ----------
    user_input:
        Raw string from the user (e.g. 'AAPL', 'apple', 'Apple Inc.').

    Returns
    -------
    (is_valid, canonical_ticker)
        is_valid=True and canonical_ticker='AAPL' on success.
        is_valid=False and canonical_ticker=None on failure.

    Notes
    -----
    If the ticker database is unavailable, any non-empty input is accepted
    (to avoid blocking the system during development).
    """
    if not user_input or not user_input.strip():
        return False, None

    stripped = user_input.strip()

    # If database is empty (file missing), accept any input
    if not _ticker_to_name:
        return True, stripped.upper()

    # 1. Exact ticker match (case-insensitive)
    upper = stripped.upper()
    if upper in _ticker_to_name:
        return True, upper

    # 2. Exact company name match (case-insensitive)
    lower = stripped.lower()
    if lower in _name_to_ticker:
        return True, _name_to_ticker[lower]

    # 3. Fuzzy company name match
    matches = difflib.get_close_matches(lower, _all_names, n=1, cutoff=_FUZZY_CUTOFF)
    if matches:
        best = matches[0]
        canonical = _name_to_ticker.get(best)
        if canonical:
            logger.info("Fuzzy match: '%s' → '%s' (%s)", stripped, canonical, best)
            return True, canonical

    return False, None


def get_company_name(ticker: str) -> str | None:
    """Return the company name for a canonical ticker, or None if not found."""
    return _ticker_to_name.get(ticker.upper())


INVALID_TICKER_MESSAGE = (
    "**Invalid ticker:** '{input}' was not recognised as a valid stock ticker or "
    "company name. Please enter a valid NYSE or NASDAQ ticker symbol (e.g. 'AAPL', "
    "'MSFT', 'NVDA') or a company name."
)
