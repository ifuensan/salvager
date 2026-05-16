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

ARG GIT_SHA=unknown
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    SALVAGER_COMMIT=${GIT_SHA}

# Non-root runtime user. APP_UID/APP_GID default to 1000 (typical homelab UID);
# override at build time with --build-arg APP_UID=$(id -u) APP_GID=$(id -g) to
# keep bind-mounted volumes writable from the host without sudo.
RUN groupadd -g ${APP_GID} salvager \
 && useradd  -u ${APP_UID} -g ${APP_GID} -d /app -s /usr/sbin/nologin -M salvager \
 && mkdir -p /app/data /app/config \
 && chown ${APP_UID}:${APP_GID} /app /app/data /app/config

# Copy the built venv and source from the builder stage (chown inline to keep
# the image small — a separate `chown -R /app` would duplicate the venv layer).
COPY --from=builder --chown=${APP_UID}:${APP_GID} /app/.venv /app/.venv
COPY --from=builder --chown=${APP_UID}:${APP_GID} /app/src /app/src
COPY --chown=${APP_UID}:${APP_GID} pyproject.toml ./

# Volumes for operator-owned state (NFR-PR1, NFR-S2 mode 0600 verified at startup).
VOLUME ["/app/data", "/app/config"]

USER salvager

# Entrypoint resolves to the uv-installed console script.
ENTRYPOINT ["salvager"]
