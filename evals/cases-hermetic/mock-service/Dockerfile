# Hermetic-eval mock substrate. Listens on port 8005 inside the
# docker compose network; the `agent-service-eval` sibling routes
# its `DATA_PLANE_URL` here. Built only under `--profile eval`.

FROM python:3.14-slim

# Install uv for fast dep resolution + venv management.
RUN pip install --no-cache-dir uv

# Preserve the repo-relative source-tree layout in-container so
# every path dep in the editable chain resolves cleanly:
#
#   /repo/multi-chain-analysis-agent/evals/cases-hermetic/mock-service/   <- WORKDIR
#   /repo/multi-chain-analysis-agent/agent-service/                       <- mock-service's path dep
#   /repo/second-brain/packages/codex-agent-driver/                        <- agent-service's path dep
#
# uv's path normalization refuses to climb above the project root,
# so the in-container layout has to preserve the same depth as the
# host tree. The literal segment name doesn't matter; the depth does.
WORKDIR /repo/multi-chain-analysis-agent/evals/cases-hermetic/mock-service

COPY agent-service /repo/multi-chain-analysis-agent/agent-service

# codex-agent-driver comes via the named build context (declared in
# `docker-compose.yml::eval-mock::additional_contexts`); identical
# shape to how `agent-service`'s Dockerfile pulls the same sibling.
COPY --from=codex-agent-driver . /repo/second-brain/packages/codex-agent-driver

COPY evals/cases-hermetic/mock-service /repo/multi-chain-analysis-agent/evals/cases-hermetic/mock-service

# Drop any host-side `.venv` that slipped in via COPY (it links to
# a non-existent in-container interpreter, and uv refuses to sync
# over it). `.dockerignore` should already exclude it; the
# `rm -rf` is belt-and-suspenders.
RUN rm -rf .venv && uv sync --no-dev

ENV EVAL_MOCK_PORT=8005

EXPOSE 8005

CMD ["uv", "run", "uvicorn", "eval_mock.main:app", "--host", "0.0.0.0", "--port", "8005"]
