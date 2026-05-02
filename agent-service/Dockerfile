# Phase 0 walking-skeleton Dockerfile. uv-based Python 3.12 image.
# Phase B optimises this with multistage caching once dep churn slows.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

WORKDIR /app

# Copy lockfile + manifest first for layer cache.
COPY pyproject.toml ./
COPY uv.lock* ./

# Install deps into a project venv. `--no-install-project` so we only
# resolve third-party deps in this layer (project source copied next).
RUN uv sync --frozen --no-install-project 2>/dev/null || uv sync --no-install-project

# Now copy source and install the project itself.
COPY src/ ./src/
RUN uv sync --no-dev 2>/dev/null || uv sync

EXPOSE 8003

# uvicorn with --proxy-headers so we behave correctly behind any reverse
# proxy. `--no-access-log` because structlog handles request logging
# in main.py once Phase B.1 wires middleware.
CMD ["uv", "run", "uvicorn", "agent_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8003", \
     "--proxy-headers", \
     "--no-access-log"]
