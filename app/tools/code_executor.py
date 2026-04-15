"""
Sandboxed Python code execution for dynamic EDA.

Agents can use this tool to:
  - Generate custom data visualisations (matplotlib figures returned as base64 PNG).
  - Perform ad-hoc data wrangling or computations not covered by the pre-written
    financial_metrics tools.

Security model
--------------
Code runs inside a RestrictedPython-compiled environment that:
  • Allows: pandas, numpy, matplotlib, math, json, datetime, statistics, io
  • Blocks: os, sys, subprocess, importlib, open(), exec(), eval(), __import__
  • Captures: stdout (via io.StringIO) and matplotlib figures (as base64 PNG)
  • Times out after EXEC_TIMEOUT_SECONDS

On error the full traceback is returned so the agent can self-correct and retry.

──────────────────────────────────────────────────────────────────────────────
Few-shot examples (also embedded in relevant agent prompts)
──────────────────────────────────────────────────────────────────────────────

Example 1 — simple computation:
    execute_python(\"\"\"
    import math
    result = math.sqrt(2) * 100
    print(f"Result: {result:.2f}")
    \"\"\")
    → {"status":"success","data":{"stdout":"Result: 141.42\\n","figures":[],"error":null}}

Example 2 — plot a time series:
    execute_python(\"\"\"
    import matplotlib.pyplot as plt
    dates = ['2024-01', '2024-02', '2024-03']
    prices = [150.0, 162.5, 175.0]
    plt.figure(figsize=(6,3))
    plt.plot(dates, prices, marker='o')
    plt.title('AAPL Price Trend')
    plt.tight_layout()
    \"\"\")
    → {"status":"success","data":{"stdout":"","figures":["<base64 PNG>"],"error":null}}

Example 3 — blocked import (will return error):
    execute_python("import os; print(os.listdir('/'))")
    → {"status":"success","data":{"stdout":"","figures":[],
       "error":"ImportError: import of os is blocked"}}
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import base64
import io
import logging
import signal
import sys
import textwrap
import traceback
from contextlib import redirect_stdout
from typing import Any

from RestrictedPython import compile_restricted, safe_globals
from RestrictedPython.Guards import guarded_iter_unpack_sequence, safe_builtins

from app.tools.base import tool_wrapper

logger = logging.getLogger(__name__)

EXEC_TIMEOUT_SECONDS = 30

# Modules the agent is allowed to import inside the sandbox
_ALLOWED_MODULES = {
    "pandas", "numpy", "matplotlib", "matplotlib.pyplot",
    "matplotlib.figure", "matplotlib.axes", "matplotlib.ticker",
    "math", "json", "datetime", "statistics", "io", "collections",
    "itertools", "functools", "re", "string",
}


def _make_safe_import(allowed: set[str]) -> Any:
    def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
        top_level = name.split(".")[0]
        if top_level not in allowed:
            raise ImportError(f"import of '{name}' is blocked in the sandbox.")
        return __import__(name, *args, **kwargs)
    return _safe_import


def _build_restricted_globals(fig_collector: list[str]) -> dict[str, Any]:
    """Build the globals dict passed to RestrictedPython exec."""
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend, safe in threads
    import matplotlib.pyplot as plt

    # Capture savefig calls to base64 instead of writing to disk
    _orig_show = plt.show

    def _patched_show(*_: Any, **__: Any) -> None:
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        buf.seek(0)
        fig_collector.append(base64.b64encode(buf.read()).decode())
        buf.close()
        plt.close("all")

    plt.show = _patched_show  # type: ignore[assignment]

    restricted_builtins = dict(safe_builtins)
    restricted_builtins["__import__"] = _make_safe_import(_ALLOWED_MODULES)
    # Allow print
    restricted_builtins["print"] = print

    glb = dict(safe_globals)
    glb["__builtins__"] = restricted_builtins
    glb["_getiter_"] = iter
    glb["_getattr_"] = getattr
    glb["_write_"] = lambda x: x  # allow attribute writes
    glb["_inplacevar_"] = lambda op, x, y: x  # noqa: ARG005 — allow augmented assignment
    glb["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence

    return glb


def _capture_figures(plt_module: Any, fig_collector: list[str]) -> None:
    """Capture any open matplotlib figures that weren't explicitly shown."""
    try:
        figs = [plt_module.figure(n) for n in plt_module.get_fignums()]
        for fig in figs:
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
            buf.seek(0)
            fig_collector.append(base64.b64encode(buf.read()).decode())
            buf.close()
        plt_module.close("all")
    except Exception:  # noqa: BLE001
        pass


@tool_wrapper(
    suggestion_on_error=(
        "Ensure the code uses only allowed modules: pandas, numpy, matplotlib, math, json, "
        "datetime, statistics. Do not use os, sys, subprocess, or open(). "
        "If a NameError occurs, make sure to import the module before using it."
    )
)
def execute_python(code: str) -> dict[str, Any]:
    """
    Execute agent-generated Python code in a restricted sandbox.

    Parameters
    ----------
    code:
        Valid Python source code as a string.  May use: pandas, numpy,
        matplotlib (figures auto-captured), math, json, datetime, statistics.
        Do NOT use: os, sys, subprocess, open, exec, eval, __import__.

    Returns dict with:
      - stdout: captured print output
      - figures: list of base64-encoded PNG images (from plt.show() or open figures)
      - error: error message and traceback if execution failed, else null
    """
    code = textwrap.dedent(code)
    stdout_buf = io.StringIO()
    figures: list[str] = []
    error_msg: str | None = None

    try:
        byte_code = compile_restricted(code, filename="<agent_code>", mode="exec")
    except SyntaxError as exc:
        return {"stdout": "", "figures": [], "error": f"SyntaxError: {exc}"}

    glb = _build_restricted_globals(figures)

    # Timeout via SIGALRM (Unix only; on Windows we rely on the tenacity timeout)
    def _timeout_handler(signum: int, frame: Any) -> None:
        raise TimeoutError(f"Code execution exceeded {EXEC_TIMEOUT_SECONDS}s timeout.")

    use_signal = hasattr(signal, "SIGALRM")
    if use_signal:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(EXEC_TIMEOUT_SECONDS)

    try:
        with redirect_stdout(stdout_buf):
            exec(byte_code, glb)  # noqa: S102
    except TimeoutError as exc:
        error_msg = str(exc)
    except Exception:  # noqa: BLE001
        error_msg = traceback.format_exc()
    finally:
        if use_signal:
            signal.alarm(0)

    # Collect any figures that weren't explicitly shown
    try:
        import matplotlib.pyplot as plt
        _capture_figures(plt, figures)
    except Exception:  # noqa: BLE001
        pass

    return {
        "stdout": stdout_buf.getvalue(),
        "figures": figures,
        "error": error_msg,
    }
