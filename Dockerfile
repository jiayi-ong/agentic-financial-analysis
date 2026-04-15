# ── Build stage: install dependencies with uv ──────────────────────────────
FROM python:3.11-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Copy dependency manifests first (layer-cache friendly)
COPY pyproject.toml ./

# Install dependencies into /build/.venv (no project code yet)
RUN uv sync --no-install-project --no-dev

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

# Copy the virtual environment from the builder
COPY --from=builder /build/.venv /app/.venv

# Copy application source
COPY app/        ./app/
COPY frontend/   ./frontend/
COPY data/       ./data/

# Activate virtual env by prepending to PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Cloud Run expects PORT env var; fall back to 8080
ENV PORT=8080

USER app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
