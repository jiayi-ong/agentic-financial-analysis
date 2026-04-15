"""
Financial Analysis Orchestrator.

Controls the full pipeline:
  Analyze (4 specialists, parallel)
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
from typing import Any

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
from app.utils.disclaimer import DISCLAIMER

logger = logging.getLogger(__name__)

_APP_NAME = "financial_analyst"


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
        await emit(_event("agent_start", "orchestrator",
                          f"Starting financial analysis for **{ticker}**..."))

        # ── Step 1: Run all 4 specialists in parallel ─────────────────────────
        await emit(_event("agent_start", "orchestrator",
                          "Launching specialist agents in parallel: "
                          "Macro, Financial, Sentiment, Ratings..."))

        specialist_outputs = await self._run_all_specialists(
            ticker=ticker,
            user_query=user_query,
            session_id=session_id,
            emit=emit,
        )

        # Report any failures
        failed = [o for o in specialist_outputs.values() if not o.success]
        if failed:
            names = ", ".join(o.specialist for o in failed)
            await emit(_event("agent_done", "orchestrator",
                              f"Some specialists failed and will be omitted: {names}"))

        # ── Step 2: Synthesise ─────────────────────────────────────────────────
        synthesis = await self._synthesise(
            specialist_outputs=specialist_outputs,
            ticker=ticker,
            session_id=session_id,
            emit=emit,
        )

        # ── Step 3: Critique → triage loop ────────────────────────────────────
        final_synthesis = synthesis
        for attempt in range(settings.max_critique_retries + 1):
            await emit(_event("critique_start", "critique_agent",
                              f"Running quality critique (attempt {attempt + 1} of "
                              f"{settings.max_critique_retries + 1})..."))

            critique = await self._critique(
                synthesis=final_synthesis,
                session_id=session_id,
                emit=emit,
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
                # Max retries exhausted
                if critique.overall_severity == "high":
                    return self._format_abstain(ticker, critique)
                break  # Accept even with medium/low issues after max retries

            # ── Targeted re-run ───────────────────────────────────────────────
            affected = [
                name for name in critique.affected_specialists
                if name in self._specialists
            ]
            if not affected:
                break

            await emit(_event("rerun_start", "orchestrator",
                              f"Re-running specialists to address issues: {', '.join(affected)}"))

            rerun_outputs = await self._run_specialists_by_name(
                names=affected,
                ticker=ticker,
                user_query=user_query,
                session_id=session_id,
                emit=emit,
            )
            # Merge re-run results into the full specialist outputs dict
            specialist_outputs.update(rerun_outputs)

            final_synthesis = await self._synthesise(
                specialist_outputs=specialist_outputs,
                ticker=ticker,
                session_id=session_id,
                emit=emit,
                is_rerun=True,
            )

        # ── Step 4: Format and emit final output ──────────────────────────────
        narrative = self._format_final(ticker, final_synthesis)
        await emit(_event("final_output", "orchestrator", narrative,
                          payload={"narrative": narrative}))
        return narrative

    # ─────────────────────────────────────────────────────────────────────────
    # Internal pipeline steps
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_all_specialists(
        self,
        ticker: str,
        user_query: str,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
    ) -> dict[str, SpecialistOutput]:
        """Run all 4 specialists concurrently via asyncio.gather."""

        async def _run_one(name: str, agent: LlmAgent) -> tuple[str, SpecialistOutput]:
            await emit(_event("agent_start", name, f"[{name}] Starting analysis for {ticker}..."))
            output = await self._run_agent_for_specialist(
                agent=agent,
                user_message=self._specialist_prompt(ticker, user_query),
                run_id=f"{session_id}_{name}_{uuid.uuid4().hex[:6]}",
            )
            status = "done" if output.success else "failed"
            await emit(_event("agent_done", name,
                              f"[{name}] Analysis {status}. "
                              f"Confidence: {output.confidence:.0%}"))
            return name, output

        tasks = [_run_one(name, agent) for name, agent in self._specialists.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        outputs: dict[str, SpecialistOutput] = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error("Specialist task raised: %s", result)
            else:
                name, output = result
                outputs[name] = output

        return outputs

    async def _run_specialists_by_name(
        self,
        names: list[str],
        ticker: str,
        user_query: str,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
    ) -> dict[str, SpecialistOutput]:
        """Re-run a subset of specialists by name."""

        async def _run_one(name: str) -> tuple[str, SpecialistOutput]:
            agent = self._specialists[name]
            await emit(_event("agent_start", name, f"[{name}] Re-running for {ticker}..."))
            output = await self._run_agent_for_specialist(
                agent=agent,
                user_message=self._specialist_prompt(ticker, user_query),
                run_id=f"{session_id}_{name}_rerun_{uuid.uuid4().hex[:6]}",
            )
            await emit(_event("agent_done", name, f"[{name}] Re-run complete."))
            return name, output

        results = await asyncio.gather(
            *[_run_one(n) for n in names if n in self._specialists],
            return_exceptions=True,
        )
        return {
            name: output
            for result in results
            if not isinstance(result, Exception)
            for name, output in [result]
        }

    async def _synthesise(
        self,
        specialist_outputs: dict[str, SpecialistOutput],
        ticker: str,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
        is_rerun: bool = False,
    ) -> SynthesisOutput:
        label = "Re-synthesising" if is_rerun else "Synthesising"
        await emit(_event("synthesis_start", "synthesis_agent",
                          f"{label} specialist findings for {ticker}..."))

        context = self._build_synthesis_context(specialist_outputs, ticker)
        raw_text = await self._run_agent_raw(
            agent=self._synthesis_agent,
            user_message=context,
            run_id=f"{session_id}_synthesis_{uuid.uuid4().hex[:6]}",
            emit=emit,
            agent_label="synthesis_agent",
        )
        synthesis = parse_synthesis_output(raw_text)

        await emit(_event("synthesis_done", "synthesis_agent",
                          "Synthesis complete.",
                          payload={"key_hypothesis": synthesis.key_hypothesis}))
        return synthesis

    async def _critique(
        self,
        synthesis: SynthesisOutput,
        session_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
    ) -> CritiqueOutput:
        context = (
            "Please critique the following synthesis:\n\n"
            + synthesis.narrative
            + "\n\nKey hypothesis: "
            + synthesis.key_hypothesis
        )
        raw_text = await self._run_agent_raw(
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
    ) -> SpecialistOutput:
        """Run an agent via ADK Runner and parse its output as SpecialistOutput."""
        raw_text = await self._adk_run(agent, user_message, run_id)
        return parse_specialist_output(raw_text, agent.name)

    async def _run_agent_raw(
        self,
        agent: LlmAgent,
        user_message: str,
        run_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]],
        agent_label: str,
    ) -> str:
        """Run an agent via ADK Runner and return raw text."""
        return await self._adk_run(agent, user_message, run_id, emit=emit, emit_label=agent_label)

    async def _adk_run(
        self,
        agent: LlmAgent,
        user_message: str,
        run_id: str,
        emit: Callable[[SessionEvent], Awaitable[None]] | None = None,
        emit_label: str = "",
    ) -> str:
        """
        Run an ADK LlmAgent and return its final text response.

        Creates an isolated InMemorySessionService per run to prevent
        cross-contamination between parallel specialist runs.
        """
        session_service = InMemorySessionService()
        runner = Runner(
            agent=agent,
            app_name=_APP_NAME,
            session_service=session_service,
        )

        await session_service.create_session(
            app_name=_APP_NAME,
            user_id="orchestrator",
            session_id=run_id,
        )

        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=user_message)],
        )

        text_parts: list[str] = []
        tool_event_seen: set[str] = set()

        async for event in runner.run_async(
            user_id="orchestrator",
            session_id=run_id,
            new_message=message,
        ):
            # Emit tool-call events to WebSocket
            if emit and emit_label:
                tool_name = _extract_tool_name(event)
                if tool_name and tool_name not in tool_event_seen:
                    tool_event_seen.add(tool_name)
                    await emit(_event(
                        "tool_call", emit_label,
                        f"[{emit_label}] Calling tool: {tool_name}",
                        tool_name=tool_name,
                    ))

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

        return "".join(text_parts)

    # ─────────────────────────────────────────────────────────────────────────
    # Prompt / context builders
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _specialist_prompt(ticker: str, user_query: str) -> str:
        return (
            f"Perform your specialist analysis for the technology company with ticker: **{ticker}**.\n\n"
            f"User query context: {user_query}\n\n"
            "Use your available tools to collect data, then return your structured output as a JSON object."
        )

    @staticmethod
    def _build_synthesis_context(
        specialist_outputs: dict[str, SpecialistOutput],
        ticker: str,
    ) -> str:
        sections = [f"Synthesise the following specialist analyses for **{ticker}**:\n"]
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
    def _format_final(ticker: str, synthesis: SynthesisOutput) -> str:
        omitted_note = ""
        if synthesis.omitted_specialists:
            names = ", ".join(synthesis.omitted_specialists)
            omitted_note = (
                f"\n\n### ⚠️ Data Gaps\n"
                f"The following specialist analyses could not be completed and were "
                f"excluded from this report: **{names}**."
            )

        return (
            synthesis.narrative
            + omitted_note
            + f"\n\n---\n{DISCLAIMER}"
        )

    @staticmethod
    def _format_abstain(ticker: str, critique: CritiqueOutput) -> str:
        issue_list = "\n".join(
            f"- [{issue.severity.upper()}] {issue.tag}: {issue.quote[:120]}..."
            for issue in critique.issues[:5]
        )
        return (
            f"## Analysis: {ticker} — Incomplete\n\n"
            "The system was unable to produce a high-confidence analysis within "
            "the allowed number of attempts. The following issues were identified:\n\n"
            + issue_list
            + "\n\nPlease try again later or consult primary sources directly."
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


def _extract_tool_name(event: Any) -> str | None:
    """Best-effort extraction of tool name from an ADK event."""
    # Check for FunctionCall in parts
    content = getattr(event, "content", None)
    if not content:
        return None
    for part in getattr(content, "parts", []):
        fc = getattr(part, "function_call", None)
        if fc:
            return getattr(fc, "name", None)
    return None
