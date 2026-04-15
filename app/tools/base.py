"""
ToolWrapper — standardises tool call results and propagates errors back to agents.

Every tool function should be wrapped with ``tool_wrapper`` so that:
  - Success  → {"status": "success", "data": <result>}
  - Failure  → {"status": "error",   "error": <message>, "suggestion": <hint>}

The standardised envelope lets agents detect failures and self-correct in
subsequent tool calls without crashing the pipeline.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _make_error_payload(exc: Exception, suggestion: str = "") -> dict[str, Any]:
    return {
        "status": "error",
        "error": f"{type(exc).__name__}: {exc}",
        "suggestion": suggestion or "Check the arguments and retry.",
    }


def _make_success_payload(data: Any) -> dict[str, Any]:
    return {"status": "success", "data": data}


def tool_wrapper(
    suggestion_on_error: str = "Verify the arguments and try again.",
    retries: int = 1,
    wait_min: float = 1.0,
    wait_max: float = 4.0,
) -> Callable[[F], F]:
    """
    Decorator factory for ADK tool functions.

    Parameters
    ----------
    suggestion_on_error:
        Human-readable hint returned to the agent on failure.
    retries:
        Number of automatic retries on transient errors (network, rate-limit).
    wait_min / wait_max:
        Exponential back-off bounds in seconds.
    """

    def decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
                try:
                    async for attempt in AsyncRetrying(
                        stop=stop_after_attempt(retries + 1),
                        wait=wait_exponential(min=wait_min, max=wait_max),
                        reraise=False,
                    ):
                        with attempt:
                            result = await fn(*args, **kwargs)
                            return _make_success_payload(result)
                except RetryError as exc:
                    logger.warning("Tool %s failed after retries: %s", fn.__name__, exc)
                    return _make_error_payload(exc, suggestion_on_error)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Tool %s raised: %s", fn.__name__, exc)
                    return _make_error_payload(exc, suggestion_on_error)
                # Should be unreachable, but satisfy type-checker
                return _make_error_payload(RuntimeError("Unknown error"), suggestion_on_error)

            return async_wrapper  # type: ignore[return-value]

        else:

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
                try:
                    result = fn(*args, **kwargs)
                    return _make_success_payload(result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Tool %s raised: %s", fn.__name__, exc)
                    return _make_error_payload(exc, suggestion_on_error)

            return sync_wrapper  # type: ignore[return-value]

    return decorator
