"""
Text preprocessing utilities for ingested news and SEC filing content.

All functions are deterministic and side-effect-free.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def clean_html_text(raw: str) -> str:
    """
    Remove residual HTML tags, normalise whitespace, and strip boilerplate.

    Parameters
    ----------
    raw:
        Raw string that may contain HTML markup or escape sequences.

    Returns clean, plain-text string.
    """
    # Unescape HTML entities
    import html
    text = html.unescape(raw)
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Normalise unicode (NFKC collapses ligatures, replaces special spaces)
    text = unicodedata.normalize("NFKC", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Remove common boilerplate patterns
    boilerplate_patterns = [
        r"cookie\s+policy",
        r"(all\s+rights\s+reserved)",
        r"terms\s+of\s+use",
        r"privacy\s+policy",
        r"subscribe\s+to\s+our\s+newsletter",
        r"sign\s+up\s+for\s+our\s+newsletter",
        r"javascript\s+must\s+be\s+enabled",
        r"your\s+browser\s+does\s+not\s+support",
    ]
    for pat in boilerplate_patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    return text.strip()


def deduplicate_articles(articles: list[dict]) -> list[dict]:
    """
    Remove near-duplicate articles using Jaccard similarity on word sets.

    Two articles are considered duplicates if their title+body similarity
    exceeds 0.7.  The first occurrence is kept.

    Parameters
    ----------
    articles:
        List of article dicts, each expected to have 'title' and 'body_text' keys.

    Returns deduplicated list preserving original order.
    """
    if not articles:
        return []

    def _fingerprint(article: dict) -> str:
        title = article.get("title", "")
        body = article.get("body_text", "")[:300]
        return f"{title} {body}".lower()

    def _similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    kept: list[dict] = []
    fingerprints: list[str] = []

    for article in articles:
        fp = _fingerprint(article)
        is_dup = any(_similarity(fp, existing) > 0.7 for existing in fingerprints)
        if not is_dup:
            kept.append(article)
            fingerprints.append(fp)

    return kept


def truncate_to_token_limit(text: str, max_tokens: int = 3000) -> str:
    """
    Truncate text to an approximate token limit.

    Uses a rough heuristic of 4 characters per token.  Truncation happens at
    a sentence boundary where possible to avoid cutting mid-sentence.

    Parameters
    ----------
    text:
        Input text.
    max_tokens:
        Maximum approximate token count.

    Returns truncated text with a truncation notice if shortened.
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text

    # Try to truncate at a sentence boundary
    truncated = text[:max_chars]
    last_period = max(truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?"))
    if last_period > max_chars * 0.8:
        truncated = truncated[: last_period + 1]

    return truncated + "\n\n[... content truncated for length ...]"


def extract_financial_numbers(text: str) -> list[dict[str, str]]:
    """
    Extract monetary amounts and percentages mentioned in text.

    Returns a list of {value, unit, context} dicts for downstream analysis.
    """
    patterns = [
        # e.g. "$2.3 billion", "$450 million"
        (r"\$\s*[\d,]+(?:\.\d+)?\s*(?:billion|million|trillion|bn|mn|B|M|T)\b", "USD"),
        # e.g. "15.3%", "revenue growth of 8%"
        (r"\b\d+(?:\.\d+)?\s*%", "PCT"),
        # e.g. "Q3 2024", "fiscal year 2023"
        (r"\b(?:Q[1-4]|FY|fiscal\s+year)\s+\d{4}\b", "PERIOD"),
    ]
    results = []
    for pat, unit in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 40)
            results.append({
                "value": match.group().strip(),
                "unit": unit,
                "context": text[start:end].strip(),
            })
    return results
