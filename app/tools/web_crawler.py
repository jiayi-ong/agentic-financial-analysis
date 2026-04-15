"""
Web crawler tools for news and market sentiment.

Targets: Reuters, CNBC, Yahoo Finance News.
Uses httpx (async) + BeautifulSoup for HTML parsing.  No Playwright — only
static HTML is fetched.

Policies:
  • Rotates User-Agent strings on each request.
  • Enforces a configurable rate-limit delay between requests.
  • Returns an empty list (not an error) when a source is unreachable so that
    agents can continue with whatever data was collected.

──────────────────────────────────────────────────────────────────────────────
Few-shot examples (also embedded in prompts/sentiment_analyst.md)
──────────────────────────────────────────────────────────────────────────────

Example 1 — fetch 5 latest news articles about Apple:
    crawl_news(query="Apple AAPL earnings revenue", sources=["yahoo_finance"],
               max_articles=5)
    → {"status": "success", "data": [
         {"title": "Apple reports record Q4 earnings",
          "url": "https://finance.yahoo.com/news/apple-q4-earnings-...",
          "published_date": "2024-11-01",
          "body_text": "Apple Inc. on Thursday reported ...",
          "source": "yahoo_finance"},
         ...
       ]}

Example 2 — broad search across Reuters and CNBC:
    crawl_news(query="NVIDIA AI chip demand 2024",
               sources=["reuters", "cnbc"], max_articles=4)
    → {"status": "success", "data": [...]}

Example 3 — single source, macro topic:
    crawl_news(query="Federal Reserve interest rate decision",
               sources=["reuters"], max_articles=3)
    → {"status": "success", "data": [...]}
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.tools.base import tool_wrapper

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_VALID_SOURCES = {"reuters", "cnbc", "yahoo_finance"}


def _random_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _clean_text(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s{2,}", " ", text)
    return text[:3000]  # cap per article


# ── Source-specific parsers ────────────────────────────────────────────────

async def _fetch_reuters(query: str, max_articles: int, client: httpx.AsyncClient) -> list[dict]:
    """
    Reuters blocks unauthenticated crawlers (HTTP 401).
    We use Google News RSS instead, which aggregates Reuters and other
    reputable outlets and is publicly accessible without authentication.
    """
    results = []
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    await asyncio.sleep(settings.crawl_rate_limit_seconds)
    try:
        resp = await client.get(url, headers=_random_headers(), timeout=15, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)  # bytes avoids encoding declaration issues
        channel = root.find("channel")
        if channel is None:
            return results
        for item in channel.findall("item")[:max_articles]:
            title = item.findtext("title", default="Unknown")
            link = item.findtext("link", default="")
            pub_date = item.findtext("pubDate", default="")
            # description may contain HTML — strip it
            raw_desc = item.findtext("description", default="")
            body = BeautifulSoup(raw_desc, "lxml").get_text(separator=" ", strip=True)[:500]
            # Extract source outlet name if present
            source_el = item.find("source")
            outlet = source_el.text if source_el is not None else "google_news"
            results.append({
                "title": title,
                "url": link,
                "published_date": pub_date[:16] if pub_date else "",
                "body_text": body,
                "source": outlet,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google News RSS crawl failed: %s", exc)
    return results


async def _fetch_cnbc(query: str, max_articles: int, client: httpx.AsyncClient) -> list[dict]:
    results = []
    url = f"https://www.cnbc.com/search/?query={quote_plus(query)}&qsearchterm={quote_plus(query)}"
    await asyncio.sleep(settings.crawl_rate_limit_seconds)
    try:
        resp = await client.get(url, headers=_random_headers(), timeout=15, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.select("div.SearchResult-searchResult") or soup.select("li.resultlink")
        for card in cards[:max_articles]:
            title_el = card.find(["h3", "h2", "a"])
            title = title_el.get_text(strip=True) if title_el else "Unknown"
            link_el = card.find("a", href=True)
            link = link_el["href"] if link_el else ""
            date_el = card.find("time") or card.find(class_=re.compile(r"date|time", re.I))
            date_str = date_el.get("datetime", date_el.get_text(strip=True)) if date_el else ""
            body_el = card.find("p")
            body = body_el.get_text(strip=True) if body_el else ""
            results.append(
                {"title": title, "url": link, "published_date": date_str[:10],
                 "body_text": body[:500], "source": "cnbc"}
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CNBC crawl failed: %s", exc)
    return results


async def _fetch_yahoo_finance(query: str, max_articles: int, client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch news from Yahoo Finance via Google News RSS filtered to finance.yahoo.com.
    The old /search/?q=...&news_count=10 URL returns HTTP 404 as of 2025.
    """
    results = []
    # Scope the Google News RSS query to Yahoo Finance articles
    scoped_query = f"{query} site:finance.yahoo.com"
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(scoped_query)}&hl=en-US&gl=US&ceid=US:en"
    )
    await asyncio.sleep(settings.crawl_rate_limit_seconds)
    try:
        resp = await client.get(url, headers=_random_headers(), timeout=15, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            return results
        for item in channel.findall("item")[:max_articles]:
            title = item.findtext("title", default="Unknown")
            link = item.findtext("link", default="")
            pub_date = item.findtext("pubDate", default="")
            raw_desc = item.findtext("description", default="")
            body = BeautifulSoup(raw_desc, "lxml").get_text(separator=" ", strip=True)[:500]
            results.append({
                "title": title,
                "url": link,
                "published_date": pub_date[:16] if pub_date else "",
                "body_text": body,
                "source": "yahoo_finance",
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Yahoo Finance crawl failed: %s", exc)
    return results


_SOURCE_FETCHERS = {
    "reuters": _fetch_reuters,
    "cnbc": _fetch_cnbc,
    "yahoo_finance": _fetch_yahoo_finance,
}


@tool_wrapper(
    suggestion_on_error=(
        "sources must be a list containing one or more of: 'reuters', 'cnbc', 'yahoo_finance'. "
        "max_articles should be between 1 and 10."
    ),
    retries=1,
)
async def crawl_news(
    query: str,
    sources: list[str] | None = None,
    max_articles: int | None = None,
) -> list[dict[str, Any]]:
    """
    Crawl news articles from reputable financial news sources.

    Parameters
    ----------
    query:
        Search query string (e.g. 'Apple AAPL earnings revenue growth 2024').
        Include the company name, ticker, and relevant financial topics.
    sources:
        List of sources to crawl.  Choose from: 'reuters', 'cnbc', 'yahoo_finance'.
        Defaults to all three if not specified.
    max_articles:
        Maximum number of articles to return per source (1–10).
        Defaults to the value in settings (typically 5).

    Returns a list of article dicts with fields:
    title, url, published_date, body_text, source.
    """
    if sources is None:
        sources = list(_VALID_SOURCES)
    if max_articles is None:
        max_articles = settings.crawl_max_articles

    max_articles = max(1, min(max_articles, 10))
    invalid = set(sources) - _VALID_SOURCES
    if invalid:
        raise ValueError(f"Unknown sources: {invalid}. Valid: {_VALID_SOURCES}")

    all_articles: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        tasks = [
            _SOURCE_FETCHERS[src](query, max_articles, client)
            for src in sources
            if src in _SOURCE_FETCHERS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, list):
                all_articles.extend(result)
            else:
                logger.warning("One crawl source returned an exception: %s", result)

    return all_articles
