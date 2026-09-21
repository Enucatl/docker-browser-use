# syntax=docker/dockerfile:1

# ---- Stage 1: builder ----
FROM python:3.14-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src/ src/
COPY config/ config/
COPY alembic.ini alembic.ini
COPY alembic/ alembic/
# uv build needs packaging metadata files declared in pyproject.toml.
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-editable

# ---- Stage 2: runtime ----
FROM python:3.14-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip /root/.cache

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /app app

RUN mkdir -p /var/lib/browser-use/artifacts \
    && chown -R app:app /var/lib/browser-use

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    BROWSER_USE_ROOT=/app \
    BROWSER_USE_CONFIG_DIR=/tmp/browseruse

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/config /app/config
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /app/alembic /app/alembic

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD [".venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD [".venv/bin/python", "-m", "browser_use_agent"]
