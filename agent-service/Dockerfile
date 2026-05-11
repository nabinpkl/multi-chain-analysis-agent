# Phase 0 walking-skeleton Dockerfile. uv-based Python 3.14 image.
# Phase B optimises this with multistage caching once dep churn slows.
#
# Python pinned to 3.14 because the chunk 3 `codex-agent-driver`
# dep (sibling repo `second-brain/packages/codex-agent-driver`)
# requires-python = ">=3.14". The local dev venv was already on
# 3.14; this image bump brings docker into parity.

# Stage 1: pull the codex CLI from a real Node image so we don't
# carry npm in the final layer. @openai/codex is pinned to the
# version the host dev box is running (kept in lockstep so what
# works locally works in docker). Bump both when bumping codex.
FROM node:22-bookworm-slim AS codex-cli
ARG CODEX_VERSION=0.130.0
RUN npm install -g --no-fund --no-audit @openai/codex@${CODEX_VERSION}

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS base

# Carry the Node runtime + the codex CLI tree forward from the
# build stage. `node` is the interpreter the `codex` shebang line
# needs; `@openai/codex` is the JS package whose `bin/codex.js`
# becomes our `/usr/local/bin/codex` shim. No npm, no
# node_modules anywhere except the codex install tree, so the
# final image is the slim uv base plus ~70 MB of Node + codex
# rather than a full Node image.
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules/@openai/codex \
    /usr/local/lib/node_modules/@openai/codex
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js \
    /usr/local/bin/codex && codex --version

# codex CLI's `--sandbox=read-only` (and `workspace-write`) wraps the
# child process in bubblewrap on Linux. The CLI ships a bundled bwrap
# binary but logs a noisy error and falls back to the system one when
# present, so installing the OS package is cleaner. Required by codex
# >= 0.120 on Linux for any non-`danger-full-access` sandbox mode.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# In-image directory layout mirrors the host source tree so the
# relative `[tool.uv.sources] codex-agent-driver = { path =
# "../../second-brain/packages/codex-agent-driver" }` in
# `agent-service/pyproject.toml` resolves identically in local
# `uv sync` (from the host) and in this docker build.
#
#   /repo/multi-chain-analysis-engine/agent-service/   <- WORKDIR
#   /repo/second-brain/packages/codex-agent-driver/    <- path-dep target
#
# Without this mirroring, uv refuses to normalize a relative path
# that escapes the project root (`/app/../../...` fails with
# "cannot normalize a relative path beyond the base directory").
WORKDIR /repo/multi-chain-analysis-engine/agent-service

# Chunk 3 path dep. The named build context comes from
# `docker-compose.yml`'s `additional_contexts.codex-agent-driver`,
# which points at `../second-brain/packages/codex-agent-driver`
# on the host.
COPY --from=codex-agent-driver . /repo/second-brain/packages/codex-agent-driver

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
