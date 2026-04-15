"""
Shared utilities for agent construction and output parsing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.schemas.agent_output import SpecialistOutput, SynthesisOutput
from app.schemas.critique import CritiqueOutput

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(agent_name: str) -> str:
    """
    Load the system prompt markdown file for a given agent.

    The file must exist at app/prompts/<agent_name>.md.
    This is called at agent construction time — prompts are loaded once.
    """
    path = _PROMPTS_DIR / f"{agent_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _sanitize_json_strings(text: str) -> str:
    """
    Escape literal control characters (newlines, tabs, carriage returns, etc.)
    that appear inside JSON string values.

    yfinance / SEC filing text embedded in JSON can contain raw ``\\n`` or ``\\t``
    characters which are illegal inside JSON strings and cause ``json.loads`` to
    raise ``JSONDecodeError: Expecting ',' delimiter``.

    This scanner tracks ``in_string`` / ``escaped`` state character by character
    so that it only modifies characters inside string literals, leaving structural
    JSON syntax untouched.
    """
    result: list[str] = []
    in_string = False
    escaped = False

    _ESCAPE_MAP = {
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\b": "\\b",
        "\f": "\\f",
    }

    for ch in text:
        if escaped:
            result.append(ch)
            escaped = False
            continue

        if ch == "\\" and in_string:
            result.append(ch)
            escaped = True
            continue

        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue

        if in_string and ch in _ESCAPE_MAP:
            result.append(_ESCAPE_MAP[ch])
            continue

        # Also escape other raw control characters (U+0000–U+001F) inside strings
        if in_string and ord(ch) < 0x20:
            result.append(f"\\u{ord(ch):04x}")
            continue

        result.append(ch)

    return "".join(result)


def _extract_json_block(text: str) -> str:
    """
    Extract a JSON object from a text that may contain markdown fences or prose.
    Tries ```json ... ``` block first, then the first standalone {...} block.
    """
    import re

    # Try ```json ... ``` fence
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)

    # Find the outermost { ... }
    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return text[start:]


def parse_specialist_output(text: str, specialist_name: str) -> SpecialistOutput:
    """
    Parse an agent's text response into a validated SpecialistOutput.

    Falls back to a failure SpecialistOutput if parsing fails.
    """
    logger.debug(
        "Raw output from '%s' (%d chars): %.2000s",
        specialist_name, len(text), text,
    )
    try:
        raw = _extract_json_block(text)
        raw = _sanitize_json_strings(raw)
        data = json.loads(raw)
        data.setdefault("specialist", specialist_name)
        return SpecialistOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to parse SpecialistOutput for %s: %s\nFull raw text:\n%s",
            specialist_name, exc, text,
        )
        return SpecialistOutput(
            specialist=specialist_name,
            claims=["Output parsing failed — raw response could not be decoded as SpecialistOutput."],
            evidence=[],
            confidence=0.0,
            success=False,
            failure_reason=f"ParseError: {exc}. Raw text (first 200 chars): {text[:200]}",
        )


def parse_synthesis_output(text: str) -> SynthesisOutput:
    """Parse an agent's text response into a validated SynthesisOutput."""
    try:
        raw = _extract_json_block(text)
        raw = _sanitize_json_strings(raw)
        data = json.loads(raw)
        result = SynthesisOutput.model_validate(data)
        logger.debug(
            "Synthesis narrative (%d chars): %.1000s",
            len(result.narrative), result.narrative,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to parse SynthesisOutput: %s\nFull raw text:\n%s", exc, text,
        )
        # Treat the whole text as the narrative as a graceful fallback
        return SynthesisOutput(
            narrative=text,
            key_hypothesis="Unable to extract structured hypothesis.",
            sources=[],
            omitted_specialists=[],
        )


def parse_critique_output(text: str) -> CritiqueOutput:
    """Parse an agent's text response into a validated CritiqueOutput."""
    try:
        raw = _extract_json_block(text)
        data = json.loads(raw)
        return CritiqueOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to parse CritiqueOutput: %s\nFull raw text:\n%s", exc, text,
        )
        # Treat as no-issues (accept synthesis) to avoid infinite loops
        return CritiqueOutput(
            issues=[],
            overall_severity="none",
            requires_rerun=False,
            affected_specialists=[],
            critique_summary=f"Critique parse error: {exc}. Accepting synthesis as-is.",
        )


def extract_final_text(events: list[Any]) -> str:
    """
    Extract the final model response text from a list of ADK events.

    ADK events have a `content` field; we join all text parts from the last
    model-role content block.
    """
    text_parts: list[str] = []
    for event in reversed(events):
        content = getattr(event, "content", None)
        if content is None:
            continue
        role = getattr(content, "role", None)
        if role not in ("model", "assistant"):
            continue
        parts = getattr(content, "parts", [])
        for part in parts:
            t = getattr(part, "text", None)
            if t:
                text_parts.append(t)
        if text_parts:
            break
    return "".join(reversed(text_parts))
