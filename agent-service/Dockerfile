# uv-based Python image for the agent plane.
#
# Python pinned to 3.14 to match the local dev venv. The `openai-codex`
# SDK requires-python is only >=3.10, so this pin is now a parity
# choice, not a hard floor; keep image and host on the same minor.
#
# The codex binary ships inside the `openai-codex` SDK via its
# `openai-codex-cli-bin` dependency (installed into the project venv by
# `uv sync`), so there is no separate codex CLI install / Node stage.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS base

# codex's `--sandbox=read-only` wraps the child process in bubblewrap
# on Linux. The bundled binary can fall back to a vendored bwrap but
# logs noisily when the system package is absent, so install it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/*

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
