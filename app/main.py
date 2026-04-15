"""
FastAPI application entry point.

Endpoints:
  GET  /         → serves frontend/index.html
  GET  /health   → liveness probe for Cloud Run
  WS   /ws/{session_id}  → WebSocket for real-time agent communication

Static files under /static/ are served from frontend/static/.
"""

from __future__ import annotations

# Load .env into os.environ BEFORE any Google library imports so that
# GOOGLE_APPLICATION_CREDENTIALS and other Google env vars are visible
# to the auth library.  pydantic-settings only populates the Settings
# object; it does NOT call os.environ.update().
from dotenv import load_dotenv
load_dotenv(override=False)  # don't overwrite vars already set in the shell

import datetime
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agents.orchestrator import FinancialOrchestrator
from app.config import settings
from app.schemas.session import SessionEvent
from app.session.manager import session_manager
from app.tracing.tracer import init_tracing
from app.utils.ticker import INVALID_TICKER_MESSAGE, validate_ticker_async

# ── Static paths ──────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent  # repo root
_FRONTEND = _ROOT / "frontend"

# ── Logging setup ─────────────────────────────────────────────────────────────
# Logs go to stdout (captured by Cloud Run / Docker) AND to a local .txt file.
# The file is useful for local development; on Cloud Run the filesystem is
# ephemeral so stdout is the canonical log sink there.
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_file = _LOG_DIR / f"financial_analyst_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

_log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_level = getattr(logging, settings.log_level, logging.INFO)

logging.basicConfig(
    level=_log_level,
    format=_log_fmt,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_log_file), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
logger.info("Log file: %s", _log_file)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    """Startup / shutdown lifecycle hooks."""
    # ── Vertex AI / ADK initialisation ───────────────────────────────────────
    # Tell google-genai to route through Vertex AI (service-account auth) rather
    # than the direct Gemini API (which requires GOOGLE_API_KEY).
    # This must be set before the first ADK Runner is created.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")

    import vertexai
    vertexai.init(
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
    )

    init_tracing(enable_cloud_trace=settings.enable_cloud_trace)
    logger.info(
        "Financial Analyst starting up | model=%s | project=%s | location=%s",
        settings.gemini_model,
        settings.google_cloud_project,
        settings.google_cloud_location,
    )
    yield
    logger.info("Financial Analyst shutting down.")


# ── App factory ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Agentic Financial Analyst",
    description="Multi-agent financial analysis powered by Google ADK and Vertex AI",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,   # disable Swagger UI in production
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static files ──────────────────────────────────────────────────────────────
_STATIC = _FRONTEND / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# Disable browser caching for all /static/* responses so CSS/JS changes are
# always picked up immediately during development.
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):  # type: ignore[override]
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

app.add_middleware(NoCacheStaticMiddleware)


# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        str(_FRONTEND / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/health")
async def health() -> dict:
    """Liveness probe for Cloud Run."""
    return {
        "status": "ok",
        "active_sessions": session_manager.active_count,
        "model": settings.gemini_model,
    }


# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """
    WebSocket endpoint for real-time financial analysis.

    Protocol:
      1. Client connects → server sends `session_ready` event.
      2. Client sends JSON: {"ticker": "AAPL", "query": "..."}
      3. Server streams SessionEvent JSON objects as the pipeline runs.
      4. Server sends `final_output` event when done.
      5. Either side may disconnect; server cleans up the session.
    """
    await websocket.accept()
    logger.info("WebSocket connected: session_id=%s", session_id)

    orchestrator = FinancialOrchestrator()
    session = None

    async def emit(event: SessionEvent) -> None:
        """Send a SessionEvent to the connected client."""
        try:
            await websocket.send_text(event.to_ws_message())
            session_manager.add_event(session_id, event)
        except Exception:  # noqa: BLE001
            pass  # Client may have disconnected

    try:
        # ── Announce readiness ────────────────────────────────────────────────
        await emit(
            SessionEvent(
                event_type="session_ready",
                agent_name="system",
                message="Connected. Please enter a stock ticker to begin analysis.",
            )
        )

        # ── Wait for the ticker message ───────────────────────────────────────
        raw = await websocket.receive_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"ticker": raw.strip(), "query": "Analyse this company."}

        ticker_input: str = str(payload.get("ticker", "")).strip()
        user_query: str = str(
            payload.get("query", f"Perform a comprehensive financial analysis of {ticker_input}.")
        )

        # ── Ticker validation ─────────────────────────────────────────────────
        # validate_ticker_async: fast local lookup first, live yfinance fallback
        # for any ticker not in the local database (covers all US-listed stocks).
        is_valid, canonical_ticker = await validate_ticker_async(ticker_input)
        if not is_valid or not canonical_ticker:
            await emit(
                SessionEvent(
                    event_type="validation_error",
                    agent_name="system",
                    message=INVALID_TICKER_MESSAGE.format(input=ticker_input),
                )
            )
            await websocket.close(code=1000)
            return

        # ── Create session ────────────────────────────────────────────────────
        session = await session_manager.create(canonical_ticker, user_query)
        logger.info(
            "Analysis started: ticker=%s session=%s", canonical_ticker, session.session_id
        )

        # ── Run the analysis pipeline ─────────────────────────────────────────
        await orchestrator.run(
            ticker=canonical_ticker,
            user_query=user_query,
            session_id=session.session_id,
            emit=emit,
        )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: session_id=%s", session_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error in WebSocket handler: %s", exc)
        try:
            await emit(
                SessionEvent(
                    event_type="error",
                    agent_name="system",
                    message=f"An unexpected error occurred: {exc}. Please try again.",
                )
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        if session:
            await session_manager.close(session.session_id)
        logger.info("WebSocket cleanup complete: session_id=%s", session_id)


# ── Dev server entrypoint ─────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        # Only watch app/ — prevents logs/*.txt from triggering reload loops
        reload_dirs=[str(_ROOT / "app")],
        log_level=settings.log_level.lower(),
    )
