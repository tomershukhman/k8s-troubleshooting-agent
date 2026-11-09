# One image for every service (agent, admin): the `agent` CLI with the eval extra. What each
# container may do comes from what compose mounts into it (SPEC 3.5). Non-root, no secrets.
FROM ghcr.io/astral-sh/uv:0.13.7-python3.14-trixie-slim@sha256:e80c05a310400cfc90af00b5b7271ef9ad24816e3265e1162af8a678e1aad099
# Bytecode compiled at build (the non-root user can't write .pyc files, so each run would
# recompile); no uv cache left in the image; no dev tools.
ENV UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 UV_NO_DEV=1
WORKDIR /app
# Dependencies in their own layer, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra eval --no-install-project
COPY . .
RUN uv sync --locked --extra eval
ARG GIT_COMMIT=""
ENV PATH="/app/.venv/bin:$PATH" AGENT_GIT_COMMIT=$GIT_COMMIT
USER 65532:65532
ENTRYPOINT ["agent"]
