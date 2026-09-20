# ---- Stage 1: builder ----
FROM python:3.14-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
ARG VERSION=0.0.0
ENV VERSION=${VERSION}
RUN VERSION="${VERSION:-0.0.0}" \
    python - <<'PY'
from pathlib import Path
import os
import re

path = Path("pyproject.toml")
text = path.read_text()
text = re.sub(
    r'(?m)^version = "[^"]*"$',
    f'version = "{os.environ["VERSION"]}"',
    text,
    count=1,
)
path.write_text(text)
PY
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src/ src/
COPY alembic.ini alembic.ini
COPY alembic/ alembic/
RUN uv sync --frozen --no-dev --no-editable

# ---- Stage 2: runtime ----
FROM python:3.14-slim-bookworm

ARG VERSION=0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3.14/ensurepip /root/.cache

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /app app

ENV PYTHONUNBUFFERED=1 \
    VERSION=${VERSION} \
    PATH="/app/.venv/bin:$PATH" \
    BROWSER_USE_ROOT=/app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /app/alembic /app/alembic

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD [".venv/bin/python", "-m", "browser_use_agent"]
