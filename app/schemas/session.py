"""
Session and streaming event schemas.

SessionEvent objects are JSON-serialised and sent over the WebSocket to the
frontend as the orchestrator progresses through the analysis pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionEvent(BaseModel):
    """
    A single progress event emitted by the orchestrator.

    The frontend routes each event_type to a specific UI handler.
    """

    event_type: Literal[
        "session_ready",
        "validation_error",
        "agent_start",
        "agent_done",
        "tool_call",
        "tool_result",
        "synthesis_start",
        "synthesis_done",
        "critique_start",
        "critique_done",
        "rerun_start",
        "final_output",
        "abstain",
        "error",
    ] = Field(..., description="Discriminator field consumed by the frontend")
    agent_name: str | None = Field(None, description="Agent emitting this event")
    tool_name: str | None = Field(None, description="Tool name (for tool_call/tool_result events)")
    message: str = Field(..., description="Human-readable status message")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured data (e.g. critique issues, final narrative)",
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp of the event",
    )

    def to_ws_message(self) -> str:
        """Serialise to a JSON string for WebSocket transmission."""
        return self.model_dump_json()


class AnalysisSession(BaseModel):
    """
    In-memory state for one analysis run.

    Created when a client sends a ticker; destroyed after the final output
    or WebSocket disconnect.
    """

    session_id: str = Field(..., description="UUID linking this session to its WebSocket")
    ticker: str = Field(..., description="Canonical uppercase ticker (e.g. 'AAPL')")
    user_query: str = Field(..., description="Original free-text query from the user")
    started_at: datetime = Field(default_factory=datetime.utcnow)

    # Mutable state (set by orchestrator as the pipeline progresses)
    specialist_outputs: dict[str, Any] = Field(default_factory=dict)
    synthesis: dict[str, Any] | None = None
    critique_history: list[dict[str, Any]] = Field(default_factory=list)
    final_narrative: str | None = None
    events: list[SessionEvent] = Field(default_factory=list)
    tmp_dir: str | None = Field(
        None, description="Temp directory for matplotlib figures; cleaned up on session close"
    )

    model_config = {"arbitrary_types_allowed": True}
