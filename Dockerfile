# ── Build stage: install dependencies with uv ──────────────────────────────
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Use /app workdir to match runtime stage — prevents shebang path mismatches
# when the venv is copied across stages.
WORKDIR /app

COPY pyproject.toml ./

# Extract production deps from pyproject.toml (no lockfile needed) and install.
# Single-line python -c avoids Docker mis-parsing 'import' as a Dockerfile instruction.
RUN python3 -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print('\n'.join(d['project']['dependencies']))" > /tmp/requirements.txt && \
    uv venv .venv && \
    uv pip install --python .venv --no-cache-dir -r /tmp/requirements.txt

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

# Copy the virtual environment (paths match — both stages use /app)
COPY --from=builder /app/.venv /app/.venv

# Copy application source
COPY app/        ./app/
COPY frontend/   ./frontend/
COPY data/       ./data/

# Activate venv by prepending to PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Cloud Run expects PORT env var; fall back to 8080
ENV PORT=8080

# Pre-create the logs dir and give the non-root user ownership of /app
RUN mkdir -p /app/logs && chown -R app:app /app

USER app

EXPOSE 8080

# Use python -m uvicorn — bypasses shebang entirely, uses the venv's Python directly
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
