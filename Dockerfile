# syntax=docker/dockerfile:1.7

# ---- builder ---------------------------------------------------------------
# Build the application + locked dependencies into /app/.venv.
FROM python:3.12-slim AS builder

# Pull a pinned uv from the upstream image (zero install overhead).
COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Layer 1: install runtime deps only (cached across source changes).
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: copy source and install the project itself.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# ---- runtime ---------------------------------------------------------------
# Minimal final image: stdlib + the locked venv + source. No uv, no build tools.
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Copy the built venv and source from the builder stage.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./

# Volumes for operator-owned state (NFR-PR1, NFR-S2 mode 0600 verified at startup).
VOLUME ["/app/data", "/app/config"]

# Entrypoint resolves to the uv-installed console script.
ENTRYPOINT ["hardware-hunter"]
