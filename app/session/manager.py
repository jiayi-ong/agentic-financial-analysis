"""
In-memory session manager.

One AnalysisSession is created per WebSocket connection and destroyed when
the connection closes or the analysis completes.  No external state store is
needed since Cloud Run is configured with concurrency=1 per container
instance and all data lives in-process.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from typing import Any

from app.schemas.session import AnalysisSession, SessionEvent

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Thread-safe (asyncio-safe) registry of active analysis sessions.

    Usage:
        manager = SessionManager()

        session_id = manager.create("AAPL", "Analyse Apple's financials")
        session = manager.get(session_id)
        manager.add_event(session_id, event)
        manager.close(session_id)
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AnalysisSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, ticker: str, user_query: str) -> AnalysisSession:
        """Create a new session and return it."""
        session_id = str(uuid.uuid4())
        tmp_dir = tempfile.mkdtemp(prefix=f"fin_{session_id[:8]}_")
        session = AnalysisSession(
            session_id=session_id,
            ticker=ticker.upper(),
            user_query=user_query,
            tmp_dir=tmp_dir,
        )
        async with self._lock:
            self._sessions[session_id] = session
        logger.info("Session created: %s for ticker %s", session_id, ticker)
        return session

    def get(self, session_id: str) -> AnalysisSession | None:
        """Return the session or None if it doesn't exist."""
        return self._sessions.get(session_id)

    def add_event(self, session_id: str, event: SessionEvent) -> None:
        """Append an event to the session's event log."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.events.append(event)

    def set_specialist_output(
        self, session_id: str, specialist_name: str, output: Any
    ) -> None:
        """Store a specialist's output in the session."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.specialist_outputs[specialist_name] = output

    def set_synthesis(self, session_id: str, synthesis: Any) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.synthesis = synthesis

    def add_critique(self, session_id: str, critique: Any) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.critique_history.append(critique)

    def set_final_narrative(self, session_id: str, narrative: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.final_narrative = narrative

    async def close(self, session_id: str) -> None:
        """Remove the session and clean up its temp directory."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            return

        if session.tmp_dir:
            try:
                shutil.rmtree(session.tmp_dir, ignore_errors=True)
                logger.debug("Cleaned up tmp dir: %s", session.tmp_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to clean tmp dir %s: %s", session.tmp_dir, exc)

        logger.info("Session closed: %s", session_id)

    @property
    def active_count(self) -> int:
        return len(self._sessions)


# Application-level singleton
session_manager = SessionManager()
