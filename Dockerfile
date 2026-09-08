# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies are installed before the source is copied, so a code change does not
# invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.14-slim-bookworm AS runtime

RUN groupadd --system --gid 1000 talaia \
    && useradd --system --uid 1000 --gid talaia --create-home talaia

WORKDIR /app

COPY --from=builder --chown=talaia:talaia /app/.venv /app/.venv
COPY --chown=talaia:talaia src/ ./src/
COPY --chown=talaia:talaia migrations/ ./migrations/
COPY --chown=talaia:talaia alembic.ini docker-entrypoint.sh ./
COPY --chown=talaia:talaia config/ ./config/

RUN chmod +x docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TALAIA_CONFIG_PATH=/app/config/monitors.yaml

USER talaia
EXPOSE 9999

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9999/healthz').read()"

ENTRYPOINT ["./docker-entrypoint.sh"]
# Exactly one worker. Each worker would run its own scheduler and duplicate every check.
CMD ["python", "-m", "talaia"]
