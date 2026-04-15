"""
SEC EDGAR tools (using the public EDGAR REST API — no API key required).

API endpoints used:
  • https://data.sec.gov/submissions/{cik}.json   — company filing index
  • https://efts.sec.gov/LATEST/search-index?q=...  — full-text search
  • https://www.sec.gov/Archives/...               — raw filing documents

SEC policy: callers MUST set a descriptive User-Agent header.
Rate limit: ≤ 10 requests / second.  We enforce a 0.12 s delay.

──────────────────────────────────────────────────────────────────────────────
Few-shot examples (also embedded in prompts/ratings_analyst.md)
──────────────────────────────────────────────────────────────────────────────

Example 1 — find the 3 most recent 10-K filings for Apple:
    search_filings("AAPL", form_type="10-K", limit=3)
    → {"status": "success", "data": [
         {"accession_number": "0000320193-24-000123", "form": "10-K",
          "filed": "2024-11-01", "company": "Apple Inc.", "cik": "0000320193"},
         ...
       ]}

Example 2 — retrieve the MD&A section of a specific filing:
    get_filing_content("0000320193-24-000123", section="mda")
    → {"status": "success", "data": "Management's Discussion and Analysis ...(text)..."}

Example 3 — search 10-Q filings for Microsoft:
    search_filings("MSFT", form_type="10-Q", limit=4)
    → {"status": "success", "data": [...]}

Example 4 — get Risk Factors section:
    get_filing_content("0001652044-24-000022", section="risk_factors")
    → {"status": "success", "data": "Item 1A. Risk Factors ... (text) ..."}
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.tools.base import tool_wrapper

logger = logging.getLogger(__name__)

_EDGAR_BASE = "https://data.sec.gov"
_EFTS_BASE = "https://efts.sec.gov"
_ARCHIVES_BASE = "https://www.sec.gov/Archives"
_SEC_RATE_DELAY = 0.12  # seconds between requests


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept": "application/json"}


async def _get_cik(ticker: str, client: httpx.AsyncClient) -> str:
    """Resolve a ticker symbol to a zero-padded 10-digit CIK."""
    url = f"{_EDGAR_BASE}/submissions/CIK{ticker.upper().zfill(10)}.json"
    # Try ticker→CIK lookup via the EDGAR company tickers JSON
    ticker_url = "https://www.sec.gov/files/company_tickers.json"
    await asyncio.sleep(_SEC_RATE_DELAY)
    resp = await client.get(ticker_url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"CIK not found for ticker '{ticker}'")


@tool_wrapper(
    suggestion_on_error=(
        "form_type must be one of: '10-K', '10-Q', '8-K', 'DEF 14A', 'S-1'. "
        "limit must be between 1 and 20."
    ),
    retries=2,
)
async def search_filings(
    ticker: str,
    form_type: str = "10-K",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Return recent SEC filings for a company.

    Parameters
    ----------
    ticker:
        Uppercase stock ticker symbol (e.g. 'AAPL').
    form_type:
        SEC form type: '10-K', '10-Q', '8-K', 'DEF 14A', 'S-1', etc.
    limit:
        Number of most-recent filings to return (1–20).

    Returns a list of dicts with: accession_number, form, filed, company, cik.
    """
    limit = max(1, min(limit, 20))
    async with httpx.AsyncClient() as client:
        cik = await _get_cik(ticker, client)
        await asyncio.sleep(_SEC_RATE_DELAY)
        url = f"{_EDGAR_BASE}/submissions/CIK{cik}.json"
        resp = await client.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

    company_name = data.get("name", ticker)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    results: list[dict[str, Any]] = []
    for form, date, acc in zip(forms, dates, accessions):
        if form.upper() == form_type.upper():
            results.append(
                {
                    "accession_number": acc,
                    "form": form,
                    "filed": date,
                    "company": company_name,
                    "cik": cik,
                }
            )
            if len(results) >= limit:
                break
    return results


_SECTION_PATTERNS: dict[str, list[str]] = {
    "mda": [
        r"item\s+7\.?\s+management.s\s+discussion",
        r"management.s\s+discussion\s+and\s+analysis",
    ],
    "risk_factors": [
        r"item\s+1a\.?\s+risk\s+factors",
        r"risk\s+factors",
    ],
    "business": [
        r"item\s+1\.?\s+business",
    ],
    "financials": [
        r"item\s+8\.?\s+financial\s+statements",
        r"financial\s+statements\s+and\s+supplementary",
    ],
}

_MAX_SECTION_CHARS = 8_000


@tool_wrapper(
    suggestion_on_error=(
        "accession_number format: '0000320193-24-000123'. "
        "section must be one of: 'mda', 'risk_factors', 'business', 'financials', 'full'."
    ),
    retries=2,
)
async def get_filing_content(
    accession_number: str,
    section: str = "mda",
) -> str:
    """
    Retrieve text content from a specific section of an SEC filing.

    Parameters
    ----------
    accession_number:
        SEC accession number in dotted format (e.g. '0000320193-24-000123').
    section:
        Named section to extract: 'mda', 'risk_factors', 'business',
        'financials', or 'full' (returns first 8000 chars of the document).

    Returns the extracted text (up to ~8 000 characters).
    """
    # Build filing index URL
    acc_clean = accession_number.replace("-", "")
    cik = accession_number.split("-")[0].lstrip("0") or "0"
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{acc_clean}/{accession_number}-index.htm"
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        await asyncio.sleep(_SEC_RATE_DELAY)
        try:
            idx_resp = await client.get(index_url, headers={"User-Agent": settings.sec_user_agent}, timeout=20)
            idx_resp.raise_for_status()
        except httpx.HTTPStatusError:
            # Fallback: try the JSON index
            idx_url_json = (
                f"{_EDGAR_BASE}/submissions/CIK{cik.zfill(10)}.json"
            )
            idx_resp = await client.get(idx_url_json, headers=_headers(), timeout=20)
            idx_resp.raise_for_status()
            return f"[Filing index retrieved; manual review of {accession_number} recommended]"

        # Parse the index page for the primary document
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(idx_resp.text, "lxml")
        doc_link = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.endswith((".htm", ".html")) and "index" not in href.lower():
                doc_link = href
                break

        if not doc_link:
            return "[Could not locate primary document in filing index]"

        if not doc_link.startswith("http"):
            doc_link = "https://www.sec.gov" + doc_link

        await asyncio.sleep(_SEC_RATE_DELAY)
        doc_resp = await client.get(
            doc_link, headers={"User-Agent": settings.sec_user_agent}, timeout=30
        )
        doc_resp.raise_for_status()

        doc_soup = BeautifulSoup(doc_resp.text, "lxml")
        full_text = doc_soup.get_text(separator="\n", strip=True)

    if section == "full":
        return full_text[:_MAX_SECTION_CHARS]

    # Locate the section using regex patterns
    patterns = _SECTION_PATTERNS.get(section.lower())
    if not patterns:
        return full_text[:_MAX_SECTION_CHARS]

    text_lower = full_text.lower()
    start_idx = -1
    for pat in patterns:
        match = re.search(pat, text_lower)
        if match:
            start_idx = match.start()
            break

    if start_idx == -1:
        return full_text[:_MAX_SECTION_CHARS]

    extracted = full_text[start_idx: start_idx + _MAX_SECTION_CHARS]
    return extracted
