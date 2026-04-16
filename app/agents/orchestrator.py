"""
Financial Analysis Orchestrator.

Controls the full pipeline:
  Classify intent (which specialists are needed)
    → Analyse (selected specialists, parallel)
    → Synthesise
    → Critique
    → [if issues] Targeted re-run of affected specialists
    → Re-synthesise → Re-critique
    → Final output (or Abstain)
    → Append disclaimer

The orchestrator is a plain Python class (not an ADK BaseAgent) so that the
FastAPI handler can call it directly and pass a WebSocket event callback.
Each specialist is an ADK LlmAgent run via an ADK Runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import google.genai as genai
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from app.agents._helpers import (
    parse_critique_output,
    parse_specialist_output,
    parse_synthesis_output,
)
from app.agents.critique_agent import create_critique_agent
from app.agents.financial_analyst import create_financial_analyst
from app.agents.macro_analyst import create_macro_analyst
from app.agents.ratings_analyst import create_ratings_analyst
from app.agents.sentiment_analyst import create_sentiment_analyst
from app.agents.synthesis_agent import create_synthesis_agent
from app.config import settings
from app.schemas.agent_output import SpecialistOutput, SynthesisOutput
from app.schemas.critique import CritiqueOutput
from app.schemas.session import SessionEvent
from app.tools.code_executor import set_figure_collector
from app.utils.disclaimer import DISCLAIMER

logger = logging.getLogger(__name__)

_APP_NAME = "financial_analyst"

# ── JSON coercion ─────────────────────────────────────────────────────────────
# When a specialist agent returns prose instead of the required JSON structure,
# we continue the same ADK session and send this follow-up to force compliance.

_CHART_COERCE_INSTRUCTION = (
    "You have completed your data collection but produced NO charts. "
    "You MUST now call execute_python to generate at least one visualisation using "
    "the real numbers you collected above (e.g. revenue trend, margin trend, or "
    "price history line chart). Use only the actual values from your tool results — "
    "no placeholder data. Call execute_python RIGHT NOW."
)

_TOOL_CAP_INSTRUCTION = (
    "You have reached the maximum number of tool calls allowed for this run. "
    "Do NOT call any more tools. Using only the data you have already collected, "
    "produce your final structured output as a valid JSON object RIGHT NOW."
)

_JSON_COERCE_INSTRUCTION = (
    "Your previous response was not in the required JSON format. "
    "You MUST now output ONLY a valid JSON object — no prose, no markdown fences, "
    "no explanation. Use all the data you collected from your tool calls above.\n\n"
    "The JSON must have exactly these fields:\n"
    "{\n"
    '  "specialist": "<your agent name>",\n'
    '  "claims": ["<concise finding 1>", "<concise finding 2>", ...],\n'
    '  "evidence": [\n'
    '    {"text": "...", "source_url": "...", '
    '"source_type": "news", "filing_identifier": null, '
    '"extraction_timestamp": "<ISO datetime>"}\n'
    "  ],\n"
    '  "confidence": <0.0–1.0>,\n'
    '  "success": true,\n'
    '  "failure_reason": null\n'
    "}\n\n"
    "Start your response with { and end with }. Output ONLY the JSON."
)

# ── Intent router — LLM-based with keyword fallback ──────────────────────────
# Primary path: a lightweight Gemini call with few-shot examples from
# app/prompts/intent_router.md selects the minimum needed specialist set.
# Fallback (on any error): deterministic keyword matching below.

_ROUTER_PROMPT: str = (Path(__file__).parent.parent / "prompts" / "intent_router.md").read_text(encoding="utf-8")

_ALL_SPECIALISTS: tuple[str, ...] = (
    "financial_analyst", "macro_analyst", "sentiment_analyst", "ratings_analyst"
)

# Keyword fallback sets ── only used when the LLM router fails.
_FB_COMPREHENSIVE: frozenset[str] = frozenset({
    "comprehensive", "full", "overview", "tell me about", "everything",
    "report", "all aspects", "deep dive", "deep-dive", "complete", "holistic",
    "financial health", "investment case", "growth outlook",
})
_FB_SPECIALIST: dict[str, frozenset[str]] = {
    "financial_analyst": frozenset({
        "revenue", "earnings", "margin", "dcf", "valuation", "p/e", "pe ratio",
        "cash flow", "cashflow", "balance sheet", "chart", "graph", "plot",
        "price", "metric", "growth", "profit", "loss", "income", "ebitda",
        "return", "ratio", "dividend", "free cash flow", "fcf", "gross margin",
        "operating", "net income", "eps", "shares", "volatility", "trend",
        "technical", "stock data", "price data", "price history",
    }),
    "macro_analyst": frozenset({
        "macro", "economy", "economic", "interest rate", "fed",
        "federal reserve", "inflation", "gdp", "tariff", "trade", "geopolit",
        "supply chain", "regulation", "antitrust", "monetary", "recession",
        "china", "global", "central bank", "rate hike", "rate cut", "policy",
    }),
    "sentiment_analyst": frozenset({
        "news", "sentiment", "market sentiment", "media", "coverage",
        "buzz", "narrative", "press", "article", "headline", "social",
        "public opinion", "investor sentiment", "market mood",
    }),
    "ratings_analyst": frozenset({
        "analyst", "rating", "recommendation", "target price", "price target",
        "upgrade", "downgrade", "sec", "filing", "10-k", "10-q", "10k",
        "10q", "guidance", "management", "outlook", "consensus",
        "wall street", "buy", "sell", "hold",
    }),
}


def _looks_like_json(text: str) -> bool:
    """Return True if *text* appears to be (or contain) a JSON object."""
    s = text.strip()
    return bool(s) and (s.startswith("{") or ("```" in s and "{" in s))




class FinancialOrchestrator:
    """
    Top-level coordinator for the multi-agent financial analysis pipeline.

    Usage:
        orchestrator = FinancialOrchestrator()
        narrative = await orchestrator.run(
            ticker="AAPL",
            user_query="Analyse Apple's financial health and growth outlook",
            session_id="abc123",
            emit=ws_send_event,
        )
    """

    def __init__(self) -> None:
        # Instantiate all agents once at construction time
        self._macro_analyst = create_macro_analyst()
        self._financial_analyst = create_financial_analyst()
        self._sentiment_analyst = create_sentiment_analyst()
        self._ratings_analyst = create_ratings_analyst()
        self._synthesis_agent = create_synthesis_agent()
        self._critique_agent = create_critique_agent()

        self._specialists: dict[str, LlmAgent] = {
            "macro_analyst": self._macro_analyst,
            "financial_analyst": self._financial_analyst,
            "sentiment_analyst": self._sentiment_analyst,
            "ratings_analyst": self._ratings_analyst,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    async def run(
        self,
        ticker: str,
        user_query: str,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
    ) -> str:
        """
        Execute the full analysis pipeline and return the final narrative string.

        Parameters
        ----------
        ticker:
            Validated, canonical uppercase ticker (e.g. 'AAPL').
        user_query:
            The user's free-text query for this session.
        session_id:
            UUID identifying the WebSocket session (used for logging/tracing).
        emit:
            Async callback that accepts a SessionEvent and sends it to the client.
        """
        ticker = ticker.upper()
        _start_time = datetime.utcnow()
        await emit(_event("agent_start", "orchestrator",
                          f"Starting financial analysis for **{ticker}**..."))

        # ── Step 0: Classify user intent ──────────────────────────────────────
        selected_names = await self._classify_intent(user_query, ticker)
        label = ", ".join(selected_names)
        await emit(_event(
            "agent_start", "orchestrator",
            f"Analysis scope: **{label}** — tailored to your request.",
            payload={"selected_specialists": selected_names},
        ))

        # ── Step 1: Run selected specialists in parallel ───────────────────────
        specialist_outputs, all_figures = await self._run_specialists_by_name(
            names=selected_names,
            ticker=ticker,
            user_query=user_query,
            session_id=session_id,
            emit=emit,
            is_rerun=False,
        )

        logger.info(
            "All specialists done. Total figures accumulated: %d", len(all_figures)
        )

        # Report any failures
        failed = [o for o in specialist_outputs.values() if not o.success]
        if failed:
            names = ", ".join(o.specialist for o in failed)
            await emit(_event("agent_done", "orchestrator",
                              f"Some specialists failed and will be omitted: {names}"))

        # Short-circuit: if every selected specialist failed there is nothing to
        # synthesise — skip the LLM pipeline and return a brief canned message.
        if all(not o.success for o in specialist_outputs.values()):
            msg = (
                f"Analysis for **{ticker}** could not be completed — "
                "all specialist agents reported failures for this request. "
                "Please try a different query or check back later."
                f"\n\n---\n{DISCLAIMER}"
            )
            thinking_time_seconds = round(
                (datetime.utcnow() - _start_time).total_seconds(), 1
            )
            await emit(_event(
                "final_output", "orchestrator", msg,
                payload={
                    "narrative": msg,
                    "figures": [],
                    "thinking_time_seconds": thinking_time_seconds,
                },
            ))
            return msg

        # ── Step 2: Synthesise ─────────────────────────────────────────────────
        synthesis = await self._synthesise(
            specialist_outputs=specialist_outputs,
            ticker=ticker,
            session_id=session_id,
            emit=emit,
            selected_specialists=selected_names,
        )

        # ── Step 3: Critique → triage loop ────────────────────────────────────
        final_synthesis = synthesis
        critique: CritiqueOutput | None = None
        for attempt in range(settings.max_critique_retries + 1):
            await emit(_event("critique_start", "critique_agent",
                              f"Running quality critique (attempt {attempt + 1} of "
                              f"{settings.max_critique_retries + 1})..."))

            critique = await self._critique(
                synthesis=final_synthesis,
                session_id=session_id,
                emit=emit,
                user_query=user_query,
            )

            await emit(_event(
                "critique_done", "critique_agent",
                f"Critique complete — severity: **{critique.overall_severity}**. "
                + (f"Issues: {len(critique.issues)}" if critique.issues else "No issues found."),
                payload={"critique": json.loads(critique.model_dump_json())},
            ))

            if critique.passed:
                break  # Synthesis accepted

            if attempt == settings.max_critique_retries:
                # Max retries exhausted — fall through with best result so far.
                # The critique issues will be appended as warnings in the output.
                break

            # ── Targeted re-run ───────────────────────────────────────────────
            affected = [
                name for name in critique.affected_specialists
                if name in self._specialists
            ]
            if not affected:
                break

            await emit(_event("rerun_start", "orchestrator",
                              f"Re-running specialists to address issues: {', '.join(affected)}"))

            rerun_outputs, rerun_figures = await self._run_specialists_by_name(
                names=affected,
                ticker=ticker,
                user_query=user_query,
                session_id=session_id,
                emit=emit,
                is_rerun=True,
            )
            all_figures.extend(rerun_figures)
            # Merge re-run results into the full specialist outputs dict
            specialist_outputs.update(rerun_outputs)

            final_synthesis = await self._synthesise(
                specialist_outputs=specialist_outputs,
                ticker=ticker,
                session_id=session_id,
                emit=emit,
                is_rerun=True,
                selected_specialists=selected_names,
            )

        # ── Step 4: Format and emit final output ──────────────────────────────
        unresolved = critique if (critique and not critique.passed) else None
        logger.info("Formatting final output for %s", ticker)
        try:
            narrative = self._format_final(
                ticker, final_synthesis, specialist_outputs, unresolved
            )
        except Exception:
            logger.exception("_format_final failed — falling back to raw narrative")
            narrative = (final_synthesis.narrative or "") + f"\n\n---\n{DISCLAIMER}"

        thinking_time_seconds = round(
            (datetime.utcnow() - _start_time).total_seconds(), 1
        )
        logger.info("Emitting final_output for %s (%.1fs)", ticker, thinking_time_seconds)
        await emit(_event(
            "final_output", "orchestrator", narrative,
            payload={
                "narrative": narrative,
                "figures": all_figures,
                "thinking_time_seconds": thinking_time_seconds,
            },
        ))
        logger.info("final_output emitted for %s", ticker)
        return narrative

    # ─────────────────────────────────────────────────────────────────────────
    # Intent classification
    # ─────────────────────────────────────────────────────────────────────────

    async def _classify_intent(self, user_query: str, ticker: str) -> list[str]:
        """
        LLM-based intent classifier with keyword fallback.

        Uses a lightweight Gemini call (temperature=0) with few-shot examples
        from app/prompts/intent_router.md to select the minimum specialist set.
        Falls back to keyword matching if the LLM call fails.
        """
        try:
            client = genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
            prompt = f"Query: \"{user_query}\"\nAnswer:"
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=settings.gemini_model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_ROUTER_PROMPT,
                    temperature=0,
                    max_output_tokens=64,
                ),
            )
            raw = (response.text or "").strip()
            # Parse JSON array e.g. ["financial_analyst", "macro_analyst"]
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                valid = [s for s in parsed if s in self._specialists]
                if valid:
                    logger.info("Intent classification (LLM) for '%s': %s", ticker, valid)
                    return [n for n in _ALL_SPECIALISTS if n in valid]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM intent router failed (%s) — falling back to keywords.", exc)

        return self._classify_intent_keywords(user_query, ticker)

    def _classify_intent_keywords(self, user_query: str, ticker: str) -> list[str]:
        """
        Deterministic keyword fallback classifier.  Never fails.

        Selection rules (in order):
        1. Comprehensive / general query → all 4 specialists.
        2. Keyword matching per specialist.
        3. financial_analyst added unless query is exclusively sentiment/ratings.
        4. Nothing matched → all 4 specialists.
        """
        q = user_query.lower()

        if any(kw in q for kw in _FB_COMPREHENSIVE):
            logger.info("Intent classification (keywords) for '%s': all specialists", ticker)
            return list(self._specialists.keys())

        selected: set[str] = {
            name
            for name, keywords in _FB_SPECIALIST.items()
            if any(kw in q for kw in keywords)
        }
        only_non_financial = selected.issubset({"sentiment_analyst", "ratings_analyst"})
        if not only_non_financial:
            selected.add("financial_analyst")
        if not selected:
            selected = set(self._specialists.keys())

        result = [name for name in self._specialists if name in selected]
        logger.info("Intent classification (keywords) for '%s': %s", ticker, result)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Internal pipeline steps
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_specialists_by_name(
        self,
        names: list[str],
        ticker: str,
        user_query: str,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
        is_rerun: bool = False,
    ) -> tuple[dict[str, SpecialistOutput], list[str]]:
        """
        Run a set of specialists concurrently and return their outputs + any figures.

        Parameters
        ----------
        names:
            List of specialist names to run (must be keys in self._specialists).
        is_rerun:
            If True, log messages say "Re-running" instead of "Starting".
        """
        all_figures: list[str] = []

        async def _run_one(name: str) -> tuple[str, SpecialistOutput, list[str]]:
            agent = self._specialists[name]
            verb = "Re-running" if is_rerun else "Starting"
            await emit(_event("agent_start", name,
                              f"[{name}] {verb} analysis for {ticker}..."))
            output, figs, tool_history = await self._run_agent_for_specialist(
                agent=agent,
                user_message=self._specialist_prompt(ticker, user_query),
                run_id=f"{session_id}_{name}_{uuid.uuid4().hex[:6]}",
                ticker=ticker,
                emit=emit,
                emit_label=name,
            )
            status = "done" if output.success else "failed"
            verb_past = "Re-run complete" if is_rerun else f"Analysis {status}"
            await emit(_event(
                "agent_done", name,
                f"[{name}] {verb_past}. Confidence: {output.confidence:.0%}",
                payload={
                    "success": output.success,
                    "confidence": output.confidence,
                    # Suppress claims for failed specialists — failure_reason is
                    # the relevant signal; claims would only duplicate it.
                    "claims": output.claims if output.success else [],
                    "evidence": [
                        json.loads(e.model_dump_json()) for e in output.evidence
                    ],
                    "failure_reason": output.failure_reason,
                    "tool_history": tool_history,
                },
            ))
            logger.info(
                "Specialist '%s': success=%s confidence=%.0f%% claims=%d figures=%d",
                name, output.success, output.confidence * 100, len(output.claims), len(figs),
            )
            for i, claim in enumerate(output.claims, 1):
                logger.info("  [%s] Claim %d: %s", name, i, claim)
            return name, output, figs

        valid_names = [n for n in names if n in self._specialists]
        results = await asyncio.gather(
            *[_run_one(n) for n in valid_names],
            return_exceptions=True,
        )

        outputs: dict[str, SpecialistOutput] = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error("Specialist task raised: %s", result)
            else:
                name, output, figs = result
                outputs[name] = output
                all_figures.extend(figs)

        return outputs, all_figures

    async def _synthesise(
        self,
        specialist_outputs: dict[str, SpecialistOutput],
        ticker: str,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
        selected_specialists: list[str] | None = None,
        is_rerun: bool = False,
    ) -> SynthesisOutput:
        label = "Re-synthesising" if is_rerun else "Synthesising"
        await emit(_event("synthesis_start", "synthesis_agent",
                          f"{label} specialist findings for {ticker}..."))

        context = self._build_synthesis_context(
            specialist_outputs, ticker, selected_specialists or []
        )
        raw_text, _ = await self._run_agent_raw(
            agent=self._synthesis_agent,
            user_message=context,
            run_id=f"{session_id}_synthesis_{uuid.uuid4().hex[:6]}",
            emit=emit,
            agent_label="synthesis_agent",
        )
        synthesis = parse_synthesis_output(raw_text)
        logger.info(
            "Synthesis narrative (%d chars): %.1500s",
            len(synthesis.narrative), synthesis.narrative,
        )

        await emit(_event("synthesis_done", "synthesis_agent",
                          "Synthesis complete.",
                          payload={
                              "key_hypothesis": synthesis.key_hypothesis,
                              "sources": synthesis.sources,
                              "omitted_specialists": synthesis.omitted_specialists,
                          }))
        return synthesis

    async def _critique(
        self,
        synthesis: SynthesisOutput,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
        user_query: str = "",
    ) -> CritiqueOutput:
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        context = (
            f"Today's date (UTC): {today_str}\n"
            "Use this date as the ground truth when evaluating recency and "
            "when deciding whether any date in the synthesis is in the future.\n\n"
        )
        if user_query:
            context += f"Original user query: {user_query}\n\n"
        context += (
            "Please critique the following synthesis:\n\n"
            + synthesis.narrative
            + "\n\nKey hypothesis: "
            + synthesis.key_hypothesis
        )
        raw_text, _ = await self._run_agent_raw(
            agent=self._critique_agent,
            user_message=context,
            run_id=f"{session_id}_critique_{uuid.uuid4().hex[:6]}",
            emit=emit,
            agent_label="critique_agent",
        )
        return parse_critique_output(raw_text)

    # ─────────────────────────────────────────────────────────────────────────
    # ADK runner helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_agent_for_specialist(
        self,
        agent: LlmAgent,
        user_message: str,
        run_id: str,
        ticker: str = "",
        emit: Callable[[SessionEvent], Awaitable[None]] | None = None,
        emit_label: str = "",
    ) -> tuple[SpecialistOutput, list[str], list[dict]]:
        """Run an agent via ADK Runner and parse its output as SpecialistOutput.

        Returns (output, figures, tool_history).  Retries once if the agent
        returns empty text (e.g. due to a transient model non-response).
        """
        raw_text, figures, tool_history = await self._adk_run(
            agent, user_message, run_id, ticker=ticker,
            emit=emit, emit_label=emit_label,
        )
        if not raw_text.strip():
            logger.warning(
                "Specialist '%s' returned empty text — retrying once.", agent.name,
            )
            raw_text, figures, tool_history = await self._adk_run(
                agent, user_message, run_id + "_retry", ticker=ticker,
                emit=emit, emit_label=emit_label,
            )
        return parse_specialist_output(raw_text, agent.name), figures, tool_history

    async def _run_agent_raw(
        self,
        agent: LlmAgent,
        user_message: str,
        run_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
        agent_label: str,
    ) -> tuple[str, list[str]]:
        """Run an agent via ADK Runner and return (raw_text, figures).
        tool_history is discarded — synthesis/critique agents have no tools."""
        raw_text, figures, _tool_history = await self._adk_run(
            agent, user_message, run_id, emit=emit, emit_label=agent_label
        )
        return raw_text, figures

    async def _adk_run(
        self,
        agent: LlmAgent,
        user_message: str,
        run_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]] | None = None,
        emit_label: str = "",
        ticker: str = "",
    ) -> tuple[str, list[str], list[dict]]:
        """
        Run an ADK LlmAgent and return (final_text, figures, tool_history).

        figures is a list of base64-encoded PNG strings captured from any
        execute_python tool calls during this run.

        Creates an isolated InMemorySessionService per run to prevent
        cross-contamination between parallel specialist runs.

        ticker is stored in ADK session state so that any {ticker} template
        references in agent instructions resolve correctly.
        """
        session_service = InMemorySessionService()
        runner = Runner(
            agent=agent,
            app_name=_APP_NAME,
            session_service=session_service,
        )

        # Store ticker in session state so ADK template resolution works for
        # any {ticker} references that might appear in agent instructions.
        session_state = {"ticker": ticker} if ticker else {}
        await session_service.create_session(
            app_name=_APP_NAME,
            user_id="orchestrator",
            session_id=run_id,
            state=session_state,
        )

        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=user_message)],
        )

        # Register a figure collector for this run.
        # execute_python pushes any captured matplotlib PNGs into this list
        # via a ContextVar that propagates through sync and asyncio.to_thread calls.
        figures: list[str] = []
        set_figure_collector(figures)

        text_parts: list[str] = []
        tool_history: list[dict] = []
        _pending_calls: dict[str, dict] = {}
        raw_text = ""
        tool_call_count = 0
        cap_reached = False

        try:
            # ── Main agent run ────────────────────────────────────────────────
            async for event in runner.run_async(
                user_id="orchestrator",
                session_id=run_id,
                new_message=message,
            ):
                # Collect tool history AND emit rich tool_call events
                for part in getattr(getattr(event, "content", None), "parts", []) or []:
                    fc = getattr(part, "function_call", None)
                    if fc:
                        call_id = getattr(fc, "id", None) or getattr(fc, "name", "")
                        _tool_name = getattr(fc, "name", "")
                        _call_args = dict(getattr(fc, "args", {}) or {})
                        entry: dict = {"name": _tool_name, "args": _call_args, "result": None}
                        tool_history.append(entry)
                        if call_id:
                            _pending_calls[call_id] = entry
                        # Server-side log for debugging: tool name + key args
                        if _tool_name == "execute_python":
                            code_snippet = str(_call_args.get("code", ""))[:500]
                            logger.info(
                                "[%s] tool_call execute_python — code (first 500 chars):\n%s",
                                emit_label or agent.name, code_snippet,
                            )
                        else:
                            logger.info(
                                "[%s] tool_call %s — args: %s",
                                emit_label or agent.name, _tool_name,
                                json.dumps(_call_args)[:300],
                            )
                        # Emit every function call with its arguments
                        if emit and emit_label and _tool_name:
                            await emit(_event(
                                "tool_call", emit_label,
                                f"[{emit_label}] Calling tool: {_tool_name}",
                                tool_name=_tool_name,
                                payload={"tool_name": _tool_name, "args": _call_args},
                            ))
                        tool_call_count += 1
                        if tool_call_count >= settings.max_specialist_tool_calls:
                            cap_reached = True
                    fr = getattr(part, "function_response", None)
                    if fr:
                        call_id = getattr(fr, "id", None) or getattr(fr, "name", "")
                        fr_name = getattr(fr, "name", "") or ""
                        matched = _pending_calls.get(call_id)
                        if matched:
                            raw_result = getattr(fr, "response", None)
                            if hasattr(raw_result, "model_dump"):
                                raw_result = raw_result.model_dump()
                            elif not isinstance(
                                raw_result,
                                (dict, list, str, int, float, bool, type(None)),
                            ):
                                raw_result = str(raw_result)
                            matched["result"] = raw_result
                        # ── Inline figure extraction from execute_python responses ──
                        # This is more robust than the finally-block scan because
                        # it fires immediately when ADK emits the response event,
                        # independent of ID matching or ContextVar propagation.
                        if fr_name == "execute_python":
                            fr_response = getattr(fr, "response", None)
                            if fr_response is not None:
                                if hasattr(fr_response, "model_dump"):
                                    fr_response = fr_response.model_dump()
                                if isinstance(fr_response, dict):
                                    inline_figs = (fr_response.get("data") or {}).get("figures") or []
                                    inline_err  = (fr_response.get("data") or {}).get("error")
                                    inline_out  = (fr_response.get("data") or {}).get("stdout", "")
                                    # Log stdout and error for debugging (Issue 3)
                                    if inline_out:
                                        logger.info(
                                            "[%s] execute_python stdout: %s",
                                            emit_label or agent.name, inline_out[:300],
                                        )
                                    if inline_err:
                                        logger.warning(
                                            "[%s] execute_python error:\n%s",
                                            emit_label or agent.name, inline_err[:800],
                                        )
                                    existing_figs = set(figures)
                                    for fig in inline_figs:
                                        if fig not in existing_figs:
                                            figures.append(fig)
                                            existing_figs.add(fig)
                                    if inline_figs:
                                        logger.info(
                                            "[%s] execute_python yielded %d figure(s) (inline capture).",
                                            emit_label or agent.name, len(inline_figs),
                                        )

                # Collect final text response
                if hasattr(event, "is_final_response") and event.is_final_response():
                    content = getattr(event, "content", None)
                    if content:
                        for part in getattr(content, "parts", []):
                            t = getattr(part, "text", None)
                            if t:
                                text_parts.append(t)
                elif hasattr(event, "content") and event.content:
                    content = event.content
                    role = getattr(content, "role", "")
                    if role in ("model", "assistant"):
                        for part in getattr(content, "parts", []):
                            t = getattr(part, "text", None)
                            if t:
                                text_parts.append(t)

                if cap_reached:
                    break  # Stop consuming ADK events; coercion will follow

            raw_text = "".join(text_parts)

            # ── Tool cap coercion ─────────────────────────────────────────────
            # If the cap was reached the agent may not have produced its final
            # text response yet.  Ask it to output now based on collected data.
            if cap_reached:
                logger.warning(
                    "Agent '%s' hit tool-call cap (%d). Sending cap coercion.",
                    agent.name, settings.max_specialist_tool_calls,
                )
                cap_msg = genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=_TOOL_CAP_INSTRUCTION)],
                )
                cap_parts: list[str] = []
                async for event in runner.run_async(
                    user_id="orchestrator", session_id=run_id, new_message=cap_msg,
                ):
                    if hasattr(event, "is_final_response") and event.is_final_response():
                        content = getattr(event, "content", None)
                        if content:
                            for part in getattr(content, "parts", []):
                                t = getattr(part, "text", None)
                                if t:
                                    cap_parts.append(t)
                    elif hasattr(event, "content") and event.content:
                        if getattr(event.content, "role", "") in ("model", "assistant"):
                            for part in getattr(event.content, "parts", []):
                                t = getattr(part, "text", None)
                                if t:
                                    cap_parts.append(t)
                if cap_parts:
                    raw_text = "".join(cap_parts)

            # ── JSON coercion follow-up ───────────────────────────────────────
            # If the agent returned prose or nothing instead of JSON, continue
            # the same session and ask it to reformat.  The model still has all
            # its tool results in context so it can produce a valid JSON output.
            if not _looks_like_json(raw_text):
                logger.info(
                    "Agent %s did not return JSON (len=%d). Sending coercion follow-up.",
                    agent.name, len(raw_text),
                )
                coerce_msg = genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=_JSON_COERCE_INSTRUCTION)],
                )
                coerce_parts: list[str] = []
                async for event in runner.run_async(
                    user_id="orchestrator",
                    session_id=run_id,
                    new_message=coerce_msg,
                ):
                    if hasattr(event, "is_final_response") and event.is_final_response():
                        content = getattr(event, "content", None)
                        if content:
                            for part in getattr(content, "parts", []):
                                t = getattr(part, "text", None)
                                if t:
                                    coerce_parts.append(t)
                    elif hasattr(event, "content") and event.content:
                        content = event.content
                        if getattr(content, "role", "") in ("model", "assistant"):
                            for part in getattr(content, "parts", []):
                                t = getattr(part, "text", None)
                                if t:
                                    coerce_parts.append(t)
                if coerce_parts:
                    coerced = "".join(coerce_parts)
                    # Only adopt the coercion result if it improved the situation
                    if _looks_like_json(coerced) or len(coerced) > len(raw_text):
                        raw_text = coerced

            # ── Chart coercion for financial_analyst ─────────────────────────
            # If the agent collected data but produced no figures, continue the
            # session and ask it to call execute_python now.  The figure
            # collector is still active so any plt.show() calls are captured.
            if agent.name == "financial_analyst" and not figures:
                logger.info(
                    "financial_analyst produced no figures — sending chart coercion."
                )
                # Include the last execute_python error (if any) so the agent
                # has in-context feedback about why the previous attempt failed.
                last_exec_error: str | None = None
                for entry in reversed(tool_history):
                    if entry.get("name") == "execute_python":
                        result = entry.get("result") or {}
                        err = (result.get("data") or {}).get("error")
                        if err:
                            last_exec_error = str(err)[:600]
                        break
                coerce_text = _CHART_COERCE_INSTRUCTION
                if last_exec_error:
                    coerce_text = (
                        f"Your previous execute_python call failed with this error:\n\n"
                        f"```\n{last_exec_error}\n```\n\n"
                        + coerce_text
                        + " Fix the error shown above before retrying — "
                        "e.g. use `pd.to_datetime(...)` instead of `datetime.strptime(...)`."
                    )
                chart_msg = genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=coerce_text)],
                )
                async for event in runner.run_async(
                    user_id="orchestrator",
                    session_id=run_id,
                    new_message=chart_msg,
                ):
                    # Capture any new tool calls for tool_history
                    for part in getattr(getattr(event, "content", None), "parts", []) or []:
                        fc = getattr(part, "function_call", None)
                        if fc:
                            _tool_name = getattr(fc, "name", "")
                            _call_args = dict(getattr(fc, "args", {}) or {})
                            entry = {"name": _tool_name, "args": _call_args, "result": None}
                            tool_history.append(entry)
                            if emit and emit_label and _tool_name:
                                await emit(_event(
                                    "tool_call", emit_label,
                                    f"[{emit_label}] Calling tool: {_tool_name}",
                                    tool_name=_tool_name,
                                    payload={"tool_name": _tool_name, "args": _call_args},
                                ))

        finally:
            # Fallback figure extraction: scan tool_history for execute_python
            # results in case the ContextVar didn't propagate to the tool's
            # execution thread (e.g. when ADK uses run_in_executor without
            # explicit context copy).  De-duplicate against existing figures.
            existing = set(figures)
            for entry in tool_history:
                if entry.get("name") == "execute_python":
                    data = (entry.get("result") or {}).get("data") or {}
                    for fig in (data.get("figures") or []):
                        if fig not in existing:
                            figures.append(fig)
                            existing.add(fig)
            # Always clear the collector so it doesn't leak to subsequent runs.
            # This runs AFTER all coercions so execute_python calls inside any
            # coercion still find a live collector.
            set_figure_collector(None)
            logger.info("Agent '%s' captured %d figure(s).", agent.name, len(figures))

        return raw_text, figures, tool_history

    # ─────────────────────────────────────────────────────────────────────────
    # Prompt / context builders
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _specialist_prompt(ticker: str, user_query: str) -> str:
        return (
            f"Perform your specialist analysis for **{ticker}**.\n\n"
            f"User query: {user_query}\n\n"
            "Focus your analysis on the aspects most relevant to this query. "
            "Use your available tools to collect data, then return your structured "
            "output as a JSON object."
        )

    @staticmethod
    def _build_synthesis_context(
        specialist_outputs: dict[str, SpecialistOutput],
        ticker: str,
        selected_specialists: list[str],
    ) -> str:
        ran = list(specialist_outputs.keys())
        sections = [
            f"Synthesise the following specialist analyses for **{ticker}**.\n",
            f"Specialists that ran: {', '.join(ran) if ran else 'none'}",
            f"Specialists not run (out of scope for this query): "
            f"{', '.join(s for s in selected_specialists if s not in ran) or 'none'}\n",
        ]
        for name, output in specialist_outputs.items():
            sections.append(output.to_context_str())
            sections.append("---")
        sections.append(
            "\nReturn your synthesis as a JSON object matching the SynthesisOutput schema."
        )
        return "\n".join(sections)

    # ─────────────────────────────────────────────────────────────────────────
    # Final output formatting
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _format_final(
        ticker: str,
        synthesis: SynthesisOutput,
        specialist_outputs: dict[str, SpecialistOutput] | None = None,
        unresolved_critique: CritiqueOutput | None = None,
    ) -> str:
        # Compute data gaps deterministically: only specialists that were
        # selected, ran, but returned success=False.  Never list specialists
        # that were intentionally excluded from this query's scope.
        omitted_note = ""
        if specialist_outputs:
            failed_names = [
                name for name, out in specialist_outputs.items() if not out.success
            ]
            if failed_names:
                names = ", ".join(failed_names)
                omitted_note = (
                    f"\n\n### Data Gaps\n"
                    f"The following specialist analyses could not be completed and were "
                    f"excluded from this report: **{names}**."
                )

        quality_warning = ""
        if unresolved_critique and unresolved_critique.issues:
            sev = unresolved_critique.overall_severity.upper()
            issue_lines = "\n".join(
                f"- **[{i.severity.upper()}]** {i.tag}: {i.quote[:120]}"
                + ("…" if len(i.quote) > 120 else "")
                for i in unresolved_critique.issues[:5]
            )
            quality_warning = (
                f"\n\n### ⚠️ Quality Warnings ({sev})\n"
                "This result reached the maximum number of revision attempts. "
                "The following concerns were not fully resolved:\n\n"
                + issue_lines
            )

        return (
            synthesis.narrative
            + omitted_note
            + quality_warning
            + f"\n\n---\n{DISCLAIMER}"
        )



# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _event(
    event_type: str,
    agent_name: str,
    message: str,
    tool_name: str | None = None,
    payload: dict | None = None,
) -> SessionEvent:
    return SessionEvent(
        event_type=event_type,  # type: ignore[arg-type]
        agent_name=agent_name,
        tool_name=tool_name,
        message=message,
        payload=payload or {},
        timestamp=datetime.utcnow(),
    )
