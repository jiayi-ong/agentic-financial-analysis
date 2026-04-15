"""
Stock ticker validation utility.

Two-tier validation:
  1. Fast local lookup: a CSV database of common tickers (loaded at import time).
  2. Live yfinance fallback: for any ticker not in the local DB, a lightweight
     yfinance call verifies the ticker is real and traded on a US exchange.

This design means ALL US-listed stocks are supported, not just the ones in the
local CSV, while keeping common lookups instant.

Async entry-point:
    is_valid, canonical = await validate_ticker_async(user_input)

Synchronous entry-point (local DB only, no fallback):
    is_valid, canonical = validate_ticker(user_input)
"""

from __future__ import annotations

import asyncio
import csv
import difflib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "tickers.csv"
_FUZZY_CUTOFF = 0.6  # Minimum similarity score for a fuzzy name match

# Tickers that look plausible: 1–5 uppercase letters, optional dot + 1–2 letters
# (covers BRK.B, BF.B style tickers)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")

# In-memory lookup tables built at import time
_ticker_to_name: dict[str, str] = {}   # "AAPL" → "Apple Inc."
_name_to_ticker: dict[str, str] = {}   # "apple inc." → "AAPL"
_all_names: list[str] = []             # lowercase company names for fuzzy match


def _load_tickers() -> None:
    """Parse tickers.csv into the in-memory lookup tables."""
    global _ticker_to_name, _name_to_ticker, _all_names

    if not _DATA_PATH.exists():
        logger.warning(
            "Ticker database not found at %s. "
            "All validation will fall through to live yfinance lookup.",
            _DATA_PATH,
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
    Validate a user-supplied string against the local ticker database.

    Returns (True, canonical_ticker) on a local hit, or (False, None) if the
    ticker is not in the local database.  Does NOT perform any network calls.

    Use validate_ticker_async() when you want the live yfinance fallback.
    """
    if not user_input or not user_input.strip():
        return False, None

    stripped = user_input.strip()

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


def _yfinance_validate(ticker: str) -> bool:
    """
    Perform a lightweight yfinance check to verify a ticker is real.

    Uses fast_info (minimal data fetch) to avoid the overhead of a full
    Ticker.info call.  Returns True if the ticker has a recognised exchange.
    Runs synchronously — call via asyncio.to_thread for async contexts.
    """
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        # fast_info.exchange is set for any valid listed security;
        # it is absent or raises for invalid tickers.
        exchange = getattr(fi, "exchange", None)
        return bool(exchange)
    except Exception:  # noqa: BLE001
        return False


async def validate_ticker_async(user_input: str) -> tuple[bool, str | None]:
    """
    Validate a user-supplied string as a stock ticker or company name.

    Two-tier process:
      1. Fast local database lookup (no network, instant).
      2. If not found locally, validate format then call yfinance live.

    Parameters
    ----------
    user_input:
        Raw string from the user (e.g. 'AAPL', 'JPMorgan', 'BRK.B').

    Returns
    -------
    (is_valid, canonical_ticker)
        is_valid=True and canonical_ticker='AAPL' on success.
        is_valid=False and canonical_ticker=None on failure.
    """
    if not user_input or not user_input.strip():
        return False, None

    stripped = user_input.strip()
    upper = stripped.upper()

    # ── Tier 1: local database lookup ────────────────────────────────────────
    is_valid, canonical = validate_ticker(stripped)
    if is_valid and canonical:
        return True, canonical

    # ── Tier 2: format check + live yfinance fallback ─────────────────────────
    # Only bother with network call if input looks like a plausible ticker.
    if not _TICKER_RE.match(upper):
        logger.debug("'%s' failed ticker format check, rejecting without yfinance call.", upper)
        return False, None

    logger.info("'%s' not in local DB — trying live yfinance validation.", upper)
    is_real = await asyncio.to_thread(_yfinance_validate, upper)
    if is_real:
        logger.info("yfinance confirmed '%s' as valid ticker.", upper)
        return True, upper

    return False, None


def get_company_name(ticker: str) -> str | None:
    """Return the company name for a canonical ticker, or None if not found."""
    return _ticker_to_name.get(ticker.upper())


INVALID_TICKER_MESSAGE = (
    "**Invalid ticker:** '{input}' was not recognised as a valid stock ticker or "
    "company name. Please enter a valid stock ticker symbol (e.g. 'AAPL', 'JPM', "
    "'JNJ', 'BRK.B') or a company name. All US-listed stocks are supported."
)
