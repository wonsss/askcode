# AskCode 슬랙 봇 이미지 — 인바운드 포트 없음(Socket Mode), 비루트 실행
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY eval ./eval
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 askcode && mkdir -p /data && chown askcode /data
USER askcode
ENV ASKCODE_DATA_DIR=/data PATH="/app/.venv/bin:$PATH"

HEALTHCHECK --interval=60s --timeout=10s --retries=3 CMD python -c "import sqlite3,os;sqlite3.connect(os.environ['ASKCODE_DATA_DIR']+'/askcode.sqlite3').execute('select 1')"
CMD ["askcode", "serve"]
